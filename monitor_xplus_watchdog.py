"""Watchdog for monitor_xplus.py.

Runs in the background and restarts the XPLUS monitor if it is not present.
It does not trade and does not call Binance directly.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
MONITOR = ROOT / "monitor_xplus.py"
STATE = ROOT / "state"
LOG = STATE / "xplus_monitor.log"
ERR = STATE / "xplus_monitor.err"
WATCHDOG_LOG = STATE / "xplus_watchdog.log"
CHECK_SECONDS = 60


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    STATE.mkdir(exist_ok=True)
    with WATCHDOG_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"[{now()}] {message}\n")
        fh.flush()


def monitor_processes() -> list[str]:
    command = (
        "Get-CimInstance Win32_Process -Filter \"name = 'python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*monitor_xplus.py*' -and "
        "$_.CommandLine -notlike '*monitor_xplus_watchdog.py*' } | "
        "ForEach-Object { [string]$_.ProcessId }"
    )
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", command],
            cwd=str(ROOT),
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except Exception as exc:
        log(f"PROCESS_CHECK_FAIL {str(exc)[:180]}")
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def start_monitor() -> None:
    STATE.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as out, ERR.open("a", encoding="utf-8") as err:
        subprocess.Popen(
            [str(PYTHON), "-u", str(MONITOR)],
            cwd=str(ROOT),
            stdout=out,
            stderr=err,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
    log("MONITOR_RESTARTED")


def main() -> int:
    log(f"WATCHDOG_START interval={CHECK_SECONDS}s")
    while True:
        pids = monitor_processes()
        if pids:
            log(f"OK monitor_pids={','.join(pids)}")
        else:
            log("MISSING monitor; restarting")
            start_monitor()
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
