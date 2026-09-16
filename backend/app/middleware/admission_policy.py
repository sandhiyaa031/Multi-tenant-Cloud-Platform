import asyncio
import time
from typing import Callable, Awaitable
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

class AdmissionController:
    def __init__(
        self,
        p_total: int = 50,
        r_interactive: int = 15,
        l_slo: float = 100.0,
        l_resume: float = 70.0,
        n_resume_windows: int = 3
    ):
        self.P_total = p_total
        self.R = r_interactive
        self.L_slo = l_slo
        self.L_resume = l_resume
        self.N_resume_windows = n_resume_windows

        self.I_active = 0
        self.A_active = 0

        self.L_current_p95 = 0.0
        self.is_paused = False
        self.consecutive_healthy_windows = 0
        self.recent_latencies = []

        self._lock = asyncio.Lock()

        # Active workload run UUID — set at scenario start
        self.current_run_id: str | None = None

    def update_p95_latency(self, latency_ms: float):
        self.L_current_p95 = latency_ms
        if not self.is_paused:
            if self.L_current_p95 > self.L_slo:
                self.is_paused = True
                self.consecutive_healthy_windows = 0
                print(f"[THROTTLING] p95={self.L_current_p95:.1f}ms > SLO={self.L_slo}ms → HALTING analytical.")
        else:
            if self.L_current_p95 < self.L_resume:
                self.consecutive_healthy_windows += 1
                if self.consecutive_healthy_windows >= self.N_resume_windows:
                    self.is_paused = False
                    self.consecutive_healthy_windows = 0
                    print(f"[RECOVERED] Latency stable. RESUMING analytical traffic.")
            else:
                self.consecutive_healthy_windows = 0
        self.recent_latencies.clear()

    async def attempt_admit_interactive(self) -> bool:
        async with self._lock:
            if self.I_active + self.A_active < self.P_total:
                self.I_active += 1
                return True
            return False

    async def attempt_admit_analytical(self) -> bool:
        async with self._lock:
            if self.is_paused:
                return False
            if self.A_active < (self.P_total - self.R):
                self.A_active += 1
                return True
            return False

    async def release_interactive(self):
        async with self._lock:
            self.I_active = max(0, self.I_active - 1)

    async def release_analytical(self):
        async with self._lock:
            self.A_active = max(0, self.A_active - 1)

    # ── Research Persistence ──────────────────────────────────────────────────

    async def persist_observation(self, query_type: str, latency_ms: float, queue_time_ms: float):
        """
        Persists a single query observation to research.query_observations.
        Called as a fire-and-forget asyncio.Task from the middleware.
        """
        try:
            from app.db import pool
            async with pool.connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO research.query_observations
                        (run_id, query_type, latency_ms, queue_time_ms)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (self.current_run_id, query_type, latency_ms, queue_time_ms)
                )
                await conn.commit()
        except Exception as e:
            # Non-fatal — research persistence should never crash the API
            print(f"[WARN] Could not persist observation: {e}")

    async def persist_decision(self, action: str, trigger_metric: str):
        """
        Persists a controller THROTTLE/RESUME decision to research.controller_decisions.
        """
        try:
            from app.db import pool
            async with pool.connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO research.controller_decisions
                        (run_id, action_taken, trigger_metric)
                    VALUES (%s, %s, %s);
                    """,
                    (self.current_run_id, action, trigger_metric)
                )
                await conn.commit()
        except Exception as e:
            print(f"[WARN] Could not persist controller decision: {e}")


# Global singleton
controller = AdmissionController()
