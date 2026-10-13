"""Part 2 of docs/evidence/bench_noise/protocol.md: A/A pairs on the twin by each procedure.

Runs on the host (it only talks HTTP to the twin node agent) while production carries the
adopted workload. One round is one pair by each procedure; the order rotates by one position
each round. Every pair is written to <out>/round_NN_<mode>.json as it finishes, so an
interrupted session keeps what it has and can be resumed with the same command.

    python tools/bench/twin_aa.py --rounds 12 --out results/bench_noise/part2
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MODES = ["sequential", "simultaneous", "interleaved"]
ROOT = Path(__file__).resolve().parents[2]


def env(name: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} is not in .env")


def call(method: str, url: str, token: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def pair(base: str, token: str, mode: str, window_s: float, treatment_first: bool, seed: int) -> dict:
    """One A/A pair. Returns the agent's run record with our own timing around it."""
    started = time.time()
    body = {"action": None, "window_s": window_s, "repetitions": 1, "treatment_first": treatment_first, "mode": mode, "seed": seed}
    for attempt in range(40):                                   # the agent runs one thing at a time
        try:
            run_id = call("POST", f"{base}/runs", token, body)["run_id"]
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise
            time.sleep(15)
    else:
        raise SystemExit("the twin node stayed busy")
    while True:
        time.sleep(5)
        state = call("GET", f"{base}/runs/{run_id}", token)
        if state["state"] in ("DONE", "FAILED"):
            return {"mode": mode, "started": started, "finished": time.time(), "treatment_first": treatment_first,
                    "seed": seed, **state}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--first-round", type=int, default=1, help="number of the first round (extra rounds continue the count)")
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--window", type=float, default=55)
    parser.add_argument("--base", default="http://127.0.0.1:8090")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    token = env("TWIN_TOKEN")
    modes = args.modes.split(",")

    for number in range(args.first_round, args.first_round + args.rounds):
        shift = (number - 1) % len(modes)
        for mode in modes[shift:] + modes[:shift]:
            path = out / f"round_{number:02d}_{mode}.json"
            if path.exists():
                continue
            # Which arm starts, or which clone slot the treatment label gets, alternates between rounds.
            record = pair(args.base, token, mode, args.window, treatment_first=number % 2 == 0, seed=number)
            record["round"] = number
            path.write_text(json.dumps(record), encoding="utf-8")
            result = record.get("result") or {}
            errors = sum(sum(arm["errors"].values()) for arm in result.get("arms", {}).values())
            print(f"round {number:2d} {mode:12s} {record['state']:6s} {record['finished'] - record['started']:6.0f} s"
                  f"  transactions {result.get('transactions', '-')}, replay errors {errors}"
                  + (f"  {record.get('error', '')[:120]}" if record["state"] == "FAILED" else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
