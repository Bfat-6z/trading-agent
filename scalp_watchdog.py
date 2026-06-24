"""Keep scalp_autotrader.py running as a supervised paper-mode process.

This watchdog is intentionally conservative: it rejects --live unless the
operator passes --allow-live to the watchdog itself. The default 24/7 mode is
paper trading plus logs, so the system can gather evidence without placing
real futures orders.

Stop cleanly by creating state/STOP_SCALP_WATCHDOG or by running:
    python scalp_watchdog.py --status
then stopping the printed watchdog PID.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from event_store import safe_append_event

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
STOP_FILE = STATE_DIR / "STOP_SCALP_WATCHDOG"
PID_FILE = STATE_DIR / "scalp_watchdog.pid"
CHILD_PID_FILE = STATE_DIR / "scalp_autotrader.pid"
WATCHDOG_LOG = STATE_DIR / "scalp_watchdog.jsonl"
CHILD_OUT = STATE_DIR / "scalp_autotrader.out.log"
CHILD_ERR = STATE_DIR / "scalp_autotrader.err.log"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_jsonl(event: str, payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": utc_now(), "event": event, **payload}
    with WATCHDOG_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
    safe_append_event("scalp_watchdog", event, payload, ts=row["ts"])


def hidden_subprocess_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def default_python() -> str:
    if os.name == "nt":
        venv_pythonw = ROOT / "venv" / "Scripts" / "pythonw.exe"
        if venv_pythonw.exists():
            return str(venv_pythonw)
    venv_python = ROOT / "venv" / "Scripts" / "python.exe"
    return str(venv_python if venv_python.exists() else Path(sys.executable))


def write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="ascii")


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None


def is_pid_running(pid: int | None, expected_script: str | None = None) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt" and not expected_script:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        script_check = ""
        if expected_script:
            escaped = expected_script.replace("'", "''")
            script_check = f"; if ($p.CommandLine -notlike '*{escaped}*') {{ exit 2 }}"
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction Stop; if (-not $p) {{ exit 1 }}{script_check}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        )
        return result.returncode == 0
    except Exception:
        return False


def build_child_cmd(args: argparse.Namespace) -> list[str]:
    child_args = list(args.child_args)
    if "--live" in child_args and not args.allow_live:
        raise SystemExit("Refusing to run --live under watchdog without --allow-live")
    if "--live" not in child_args:
        # Paper mode defaults tuned for evidence collection, not aggressive live trading.
        defaults = [
            "--interval-seconds", str(args.interval_seconds),
            "--margin-usdt", str(args.paper_margin_usdt),
            "--leverage", str(args.paper_leverage),
            "--paper-equity", str(args.paper_equity),
        ]
        if args.paper_trade_through_memory_sleep:
            defaults.append("--paper-trade-through-memory-sleep")
        child_args = defaults + child_args
    return [default_python(), str(ROOT / "scalp_autotrader.py"), *child_args]


def status() -> int:
    watchdog_pid = read_pid(PID_FILE)
    child_pid = read_pid(CHILD_PID_FILE)
    print(f"watchdog_pid={watchdog_pid} running={is_pid_running(watchdog_pid, 'scalp_watchdog.py')}")
    print(f"child_pid={child_pid} running={is_pid_running(child_pid, 'scalp_autotrader.py')}")
    print(f"watchdog_log={WATCHDOG_LOG}")
    print(f"child_out={CHILD_OUT}")
    print(f"child_err={CHILD_ERR}")
    print(f"stop_file={STOP_FILE}")
    return 0


def supervise(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(PID_FILE)
    if existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid, "scalp_watchdog.py"):
        print(f"watchdog already running pid={existing_pid}", flush=True)
        append_jsonl("watchdog_duplicate_exit", {"existing_pid": existing_pid, "pid": os.getpid()})
        return 0
    write_pid(PID_FILE, os.getpid())
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    cmd = build_child_cmd(args)
    append_jsonl("watchdog_start", {"pid": os.getpid(), "cmd": cmd})

    while not STOP_FILE.exists():
        with CHILD_OUT.open("ab") as out_fh, CHILD_ERR.open("ab") as err_fh:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            child = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out_fh, stderr=err_fh, creationflags=creationflags)
            write_pid(CHILD_PID_FILE, child.pid)
            append_jsonl("child_start", {"pid": child.pid})
            while child.poll() is None:
                if STOP_FILE.exists():
                    append_jsonl("child_stop_requested", {"pid": child.pid})
                    child.terminate()
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=15)
                    break
                time.sleep(args.check_seconds)
            append_jsonl("child_exit", {"pid": child.pid, "returncode": child.returncode})
        if not STOP_FILE.exists():
            time.sleep(args.restart_delay_seconds)

    append_jsonl("watchdog_stop", {"pid": os.getpid()})
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Supervise scalp_autotrader.py 24/7")
    parser.add_argument("--status", action="store_true", help="print watchdog/child status and exit")
    parser.add_argument("--allow-live", action="store_true", help="allow forwarded --live child args")
    parser.add_argument("--check-seconds", type=float, default=5.0)
    parser.add_argument("--restart-delay-seconds", type=float, default=10.0)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--paper-margin-usdt", type=float, default=1.0)
    parser.add_argument("--paper-leverage", type=int, default=20)
    parser.add_argument("--paper-equity", type=float, default=100.0)
    parser.add_argument("--paper-trade-through-memory-sleep", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("child_args", nargs=argparse.REMAINDER, help="optional args forwarded after -- to scalp_autotrader.py")
    args = parser.parse_args()
    if args.child_args and args.child_args[0] == "--":
        args.child_args = args.child_args[1:]
    return args


def main() -> int:
    args = parse_args()
    if args.status:
        return status()
    return supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
