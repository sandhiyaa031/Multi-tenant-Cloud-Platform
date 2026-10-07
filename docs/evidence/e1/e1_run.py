"""E1 orchestrator (runs on the host). Requests A/A runs from the twin node agent and stores each result.

usage: python results/e1/e1_run.py <n_runs> <warm_s> [out_dir]
Reads TWIN_TOKEN and TWIN_WINDOW_S from .env. Calls only the twin agent (127.0.0.1:8090). Changes nothing else.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
n_runs, warm_s = int(sys.argv[1]), float(sys.argv[2])
OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(__file__).parent
OUT.mkdir(parents=True, exist_ok=True)
env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
           if "=" in l and not l.lstrip().startswith("#"))
TOKEN, WINDOW = env["TWIN_TOKEN"].strip(), float(env.get("TWIN_WINDOW_S", "120"))
BASE = "http://127.0.0.1:8090"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(text: str) -> None:
    with open(OUT / "progress.log", "a", encoding="utf-8") as f:
        f.write(f"[{now()}] {text}\n")


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def power() -> str:
    cmd = ("Add-Type -AssemblyName System.Windows.Forms; $p=[System.Windows.Forms.SystemInformation]::PowerStatus;"
           "$k=Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Power\\User\\PowerSchemes';"
           "'{0}|{1:N0}|{2}' -f $p.PowerLineStatus, ($p.BatteryLifePercent*100), $k.ActiveOverlayAcPowerScheme")
    try:
        return subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except Exception as exc:
        return f"unknown:{exc}"


log(f"START n_runs={n_runs} warm_s={warm_s} window_s={WINDOW} power={power()}")
time.sleep(warm_s)
for i in range(1, n_runs + 1):
    record = {"run": i, "treatment_first": i % 2 == 0, "requested": now(), "power_start": power()}
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
    record.update(finished=now(), seconds=round(time.time() - started, 1), power_end=power())
    (OUT / f"run_{i:02d}.json").write_text(json.dumps(record), encoding="utf-8")
    r = record.get("result") or {}
    errs = sum(sum(a["errors"].values()) for a in r.get("arms", {}).values()) if r else None
    log(f"run {i:02d} {record['state']} {record['seconds']}s txns={r.get('transactions')} window={r.get('window_s')}"
        f" errors={errs} power={record['power_start']} -> {record['power_end']} {record.get('error') or ''}")
log("END")
