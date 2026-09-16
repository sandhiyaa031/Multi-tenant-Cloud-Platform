import asyncio
import time
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from app.middleware.admission_policy import controller

class AdaptiveConcurrencyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        is_interactive = "/api/investigate" in path
        is_analytical = "/api/aggregate" in path

        if not is_interactive and not is_analytical:
            return await call_next(request)

        start_time = time.perf_counter()
        queue_time_ms = 0.0

        if is_interactive:
            admitted = await controller.attempt_admit_interactive()
            if not admitted:
                return Response(content="Capacity exceeded limit.", status_code=429)

            try:
                response = await call_next(request)
            finally:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                controller.recent_latencies.append(elapsed_ms)
                await controller.release_interactive()
                # Persist to research schema (fire-and-forget)
                asyncio.create_task(
                    controller.persist_observation("interactive", elapsed_ms, 0.0)
                )

        elif is_analytical:
            timeout_sec = 10.0
            wait_start = time.perf_counter()
            admitted = False

            while (time.perf_counter() - wait_start) < timeout_sec:
                admitted = await controller.attempt_admit_analytical()
                if admitted:
                    break
                await asyncio.sleep(0.1)

            queue_time_ms = (time.perf_counter() - wait_start) * 1000

            if not admitted:
                return Response(
                    content="Gateway Timeout: Concurrency Queue saturated by Controller.",
                    status_code=504
                )

            try:
                response = await call_next(request)
            finally:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                await controller.release_analytical()
                asyncio.create_task(
                    controller.persist_observation("analytical", elapsed_ms, queue_time_ms)
                )

        return response


async def telemetry_loop():
    import numpy as np
    while True:
        if controller.recent_latencies:
            latencies = list(controller.recent_latencies)
            p95 = np.percentile(latencies, 95)
            was_paused = controller.is_paused
            controller.update_p95_latency(p95)
            # Detect state transitions and persist them
            if not was_paused and controller.is_paused:
                asyncio.create_task(controller.persist_decision("THROTTLE", f"p95={p95:.1f}ms"))
            elif was_paused and not controller.is_paused:
                asyncio.create_task(controller.persist_decision("RESUME", f"p95={p95:.1f}ms"))
        else:
            controller.update_p95_latency(0.0)

        await asyncio.sleep(1.0)
