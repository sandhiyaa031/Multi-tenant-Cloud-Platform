"""Requests A/A runs from the twin node agent and stores each result. Runs on the host, standard library only.

usage: python tools/aa/aa_run.py <n_runs> <warm_s> <out_dir>
Reads TWIN_TOKEN and TWIN_WINDOW_S from .env. Calls only the twin agent on 127.0.0.1:8090.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
n_runs, warm_s, out = int(sys.argv[1]), float(sys.argv[2]), Path(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)
env = dict(line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
           if "=" in line and not line.lstrip().startswith("#"))
TOKEN, WINDOW = env["TWIN_TOKEN"].strip(), float(env.get("TWIN_WINDOW_S", "120"))
BASE = "http://127.0.0.1:8090"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_average() -> list[float] | None:
    return list(os.getloadavg()) if hasattr(os, "getloadavg") else None


def log(text: str) -> None:
    with open(out / "progress.log", "a", encoding="utf-8") as f:
        f.write(f"[{now()}] {text}\n")


def call(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


log(f"START n_runs={n_runs} warm_s={warm_s} window_s={WINDOW} load_average={load_average()}")
time.sleep(warm_s)
for i in range(1, n_runs + 1):
    record = {"run": i, "treatment_first": i % 2 == 0, "requested": now(), "load_average_start": load_average()}
    started = time.time()
    try:
        run_id = call("POST", "/runs", {"action": None, "window_s": WINDOW, "repetitions": 1,
                                        "treatment_first": i % 2 == 0})["run_id"]
        state = {"state": "RUNNING"}
        while time.time() - started < 1500:
            time.sleep(5)
            state = call("GET", f"/runs/{run_id}")
            if state["state"] in ("DONE", "FAILED"):
                break
        record.update(state=state["state"], error=state.get("error"), result=state.get("result"))
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        record.update(state="ERROR", error=f"{type(exc).__name__}: {exc}")
    record.update(finished=now(), seconds=round(time.time() - started, 1), load_average_end=load_average())
    (out / f"run_{i:02d}.json").write_text(json.dumps(record), encoding="utf-8")
    result = record.get("result") or {}
    errors = sum(sum(a["errors"].values()) for a in result.get("arms", {}).values()) if result else None
    log(f"run {i:02d} {record['state']} {record['seconds']}s txns={result.get('transactions')} errors={errors}"
        f" load_average={record['load_average_end']} {record.get('error') or ''}")
log("END")
