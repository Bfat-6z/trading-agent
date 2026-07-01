"""One-command DEMO launcher (paper-only, live trading stays disabled).

Boots the trading agent in a clean, presentable state for a live demo:
  1. Backs up the current paper account, then resets to a fresh $100 sim account
     so risk breakers/drawdown don't block demo trades (real history preserved in
     paper_trades.jsonl + the backup file).
  2. Clears the host sleep/resume pause so the paper brain is allowed to act.
  3. Enables paper-exploration so under-sampled (no-evidence-yet) setups can open
     demo paper trades. Proven-negative setups stay blocked (risk gates intact).
  4. Starts the dashboard on http://127.0.0.1:8090 and the agent fleet via the
     process supervisor.

SAFETY: never sets ALLOW_LIVE_ORDERS. The fail-closed live guard blocks every
real order regardless. This is a simulation demo only.

Usage:  venv\\Scripts\\python.exe start_demo.py
Stop:   venv\\Scripts\\python.exe start_demo.py --stop
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
PY = sys.executable
ACCOUNT = STATE / "paper_account.json"


def _reset_account_to_100() -> None:
    import paper_portfolio_manager as ppm
    if ACCOUNT.exists():
        shutil.copy(ACCOUNT, STATE / "paper_account_predemo_backup.json")
    fresh = ppm.default_account(100)
    ACCOUNT.write_text(json.dumps(fresh, indent=1, default=str), encoding="utf-8")
    print(f"[demo] paper account reset to $100 (prev backed up)")


def _clear_sleep_pause() -> None:
    try:
        import host_runtime_monitor as h
        h.acknowledge_sleep_resume_replay(actor="start_demo")
        print("[demo] sleep/resume pause cleared")
    except Exception as exc:
        print(f"[demo] pause clear skipped: {exc}")


def start() -> int:
    env = dict(os.environ)
    env["TRADING_AGENT_PAPER_EXPLORATION"] = "1"   # allow under-sampled setups to open demo trades
    env["INGEST_DECISION_CANDLES"] = "1"           # real 5m candles into decisions
    env.pop("ALLOW_LIVE_ORDERS", None)             # SAFETY: never allow live orders
    # A strong token lets the dashboard be viewed through a Cloudflare tunnel.
    # Open the tunnel URL as  https://<tunnel>/#token=<TOKEN>  and the page stores
    # it. Bind 0.0.0.0 so the tunnel reaches it; the token gates /api/*.
    import secrets
    # Strong token: needs >=24 chars, >=10 distinct, >=3 char classes
    # (lower/upper/digit/symbol). Build one deterministically-random per run.
    token = os.environ.get("TRADING_AGENT_DASHBOARD_TOKEN") or ("Demo-" + secrets.token_urlsafe(24))
    env["TRADING_AGENT_DASHBOARD_TOKEN"] = token
    _reset_account_to_100()
    _clear_sleep_pause()

    # Dashboard bound for tunnel access, token-gated.
    dash = subprocess.Popen([PY, str(ROOT / "agent_status_dashboard.py"), "--host", "0.0.0.0", "--port", "8090",
                             "--token-env", "TRADING_AGENT_DASHBOARD_TOKEN"],
                            cwd=str(ROOT), env=env,
                            stdout=open(STATE / "demo_dashboard.out.log", "w"), stderr=subprocess.STDOUT)
    (STATE / "demo_dashboard_token.txt").write_text(token, encoding="utf-8")
    print(f"[demo] dashboard starting (pid {dash.pid}) -> http://127.0.0.1:8090")
    print(f"[demo] dashboard token: {token}")
    print(f"[demo] remote view: append  #token={token}  to the tunnel URL")

    # Agent fleet via supervisor loop
    sup = subprocess.Popen([PY, str(ROOT / "agent_process_supervisor.py")],
                           cwd=str(ROOT), env=env,
                           stdout=open(STATE / "demo_supervisor.out.log", "w"), stderr=subprocess.STDOUT)
    print(f"[demo] agent supervisor starting (pid {sup.pid})")

    (STATE / "demo_pids.json").write_text(json.dumps({"dashboard": dash.pid, "supervisor": sup.pid}), encoding="utf-8")
    time.sleep(6)
    print("\n[demo] READY.")
    print("[demo] Dashboard : http://127.0.0.1:8090")
    print("[demo] Live orders: DISABLED (paper/simulation only)")
    print("[demo] Stop with  : python start_demo.py --stop")
    return 0


def stop() -> int:
    pid_file = STATE / "demo_pids.json"
    if not pid_file.exists():
        print("[demo] no demo_pids.json; nothing to stop")
        return 0
    pids = json.loads(pid_file.read_text())
    for name, pid in pids.items():
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
            print(f"[demo] stopped {name} (pid {pid})")
        except Exception as exc:
            print(f"[demo] stop {name} failed: {exc}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()
    raise SystemExit(stop() if args.stop else start())
