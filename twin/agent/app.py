"""Twin node agent: the experimentation plane's only entry point.

The control plane sends it a typed action and a window length; it clones,
applies, replays and reports measurements. It makes no decision: approving or
rejecting belongs to the verification engine in the control plane.
"""
import hmac
import logging
import os
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent import pg, runner, whatif

log = logging.getLogger("dbpilot.twin")
logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TWIN_TOKEN"]

runs: dict[str, dict] = {}
busy = threading.Lock()  # one run at a time: runs share the node's CPU and disk


RETENTION_MIN = float(os.environ.get("LOG_RETENTION_MIN", "0"))


def prune_capture(directory: str, minutes: float) -> int:
    """Deletes captured statement files last written more than `minutes` ago. The
    twin only ever replays the most recent window, and at full load the capture
    grows by tens of megabytes a minute."""
    cutoff, removed = time.time() - minutes * 60, 0
    for path in Path(directory).glob("pg-*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass  # rotated or removed underneath us; the next pass sees the truth
    return removed


def _pruner() -> None:
    while True:
        time.sleep(60)
        prune_capture(runner.LOG_DIR, RETENTION_MIN)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pg.ensure_source()
    if RETENTION_MIN > 0:
        threading.Thread(target=_pruner, daemon=True).start()
    yield
    pg.stop(pg.WORK)
    pg.stop(pg.SOURCE)


app = FastAPI(title="DBPilot twin node agent", lifespan=lifespan)


def authorised(authorization: str = Header(default="")) -> None:
    if not hmac.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(401, "invalid token")


class RunIn(BaseModel):
    action: dict | None = None
    window_s: float = Field(default=120, ge=10, le=1800)
    repetitions: int = Field(default=1, ge=1, le=5)
    treatment_first: bool = False


class WhatIfIn(BaseModel):
    action: dict
    queries: list[str] = Field(max_length=200)


@app.get("/status", dependencies=[Depends(authorised)])
def status():
    return {"source": pg.source_status(), "busy": busy.locked()}


def _execute(run_id: str, body: RunIn) -> None:
    try:
        runs[run_id].update(state="DONE", result=runner.run(body.action, body.window_s, body.repetitions, body.treatment_first))
    except Exception as exc:  # reported to the caller, who decides what a failed run means
        log.error("run %s failed:\n%s", run_id, traceback.format_exc())
        runs[run_id].update(state="FAILED", error=f"{type(exc).__name__}: {exc}")
        pg.stop(pg.WORK)
    finally:
        busy.release()


@app.post("/runs", status_code=202, dependencies=[Depends(authorised)])
def start_run(body: RunIn):
    if not busy.acquire(blocking=False):
        raise HTTPException(409, "a twin run is already in progress")
    run_id = uuid.uuid4().hex
    runs[run_id] = {"state": "RUNNING"}
    threading.Thread(target=_execute, args=(run_id, body), daemon=True).start()
    return {"run_id": run_id}


@app.get("/runs/{run_id}", dependencies=[Depends(authorised)])
def get_run(run_id: str):
    if run_id not in runs:
        raise HTTPException(404, "unknown run")
    return runs[run_id]


class ExplainIn(BaseModel):
    query: str = Field(max_length=20000)


@app.post("/explain", dependencies=[Depends(authorised)])
def explain(body: ExplainIn):
    return whatif.explain(body.query)


@app.post("/whatif", dependencies=[Depends(authorised)])
def what_if(body: WhatIfIn):
    return whatif.index_whatif(body.action, body.queries)
