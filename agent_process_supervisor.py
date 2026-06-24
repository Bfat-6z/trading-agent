"""Supervise long-running trading-agent helper processes.

This is a local process watchdog. It starts read-only/learning services when
they are missing or stale. It does not place trades and it does not touch API
keys. `scalp_autotrader.py` remains supervised by `scalp_watchdog.py`.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from event_store import safe_append_event, safe_upsert_heartbeat

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
PID_FILE = STATE_DIR / "agent_process_supervisor.pid"
LOCK_FILE = STATE_DIR / "agent_process_supervisor.lock"
STOP_FILE = STATE_DIR / "STOP_AGENT_PROCESS_SUPERVISOR"
LOG_FILE = STATE_DIR / "agent_process_supervisor.jsonl"
HEARTBEAT_PATH = STATE_DIR / "agent_process_supervisor_heartbeat.json"
SUPERVISOR_SCRIPT = "agent_process_supervisor.py"


@dataclass(frozen=True)
class AgentSpec:
    name: str
    script: str
    args: tuple[str, ...]
    pid_file: Path
    heartbeat_file: Path | None
    max_heartbeat_age_seconds: float | None


def specs() -> list[AgentSpec]:
    return [
        AgentSpec("dashboard", "agent_status_dashboard.py", ("--host", "127.0.0.1", "--port", "8090"), STATE_DIR / "agent_status_dashboard.pid", None, None),
        AgentSpec("market_observer", "market_observer.py", tuple(), STATE_DIR / "market_observer.pid", STATE_DIR / "market_observer_heartbeat.json", 420),
        AgentSpec("news_observer", "news_observer.py", tuple(), STATE_DIR / "news_observer.pid", STATE_DIR / "news_observer_heartbeat.json", 900),
        AgentSpec("dream_cycle", "dream_cycle.py", tuple(), STATE_DIR / "dream_cycle.pid", STATE_DIR / "dream_cycle_heartbeat.json", 2400),
        AgentSpec("reflection_agent", "reflection_agent.py", ("--interval-hours", "0.5"), STATE_DIR / "reflection_agent.pid", STATE_DIR / "reflection_agent_heartbeat.json", 2400),
        AgentSpec("cognitive_supervisor", "cognitive_supervisor.py", ("--interval-minutes", "20"), STATE_DIR / "cognitive_supervisor.pid", STATE_DIR / "cognitive_supervisor_heartbeat.json", 1500),
        AgentSpec("llm_reasoning_agent", "llm_reasoning_agent.py", ("--interval-minutes", "60"), STATE_DIR / "llm_reasoning_agent.pid", STATE_DIR / "llm_reasoning_agent_heartbeat.json", 900),
        AgentSpec("paper_candidate_feeder", "paper_candidate_feeder.py", ("--interval-seconds", "60"), STATE_DIR / "paper_candidate_feeder.pid", STATE_DIR / "paper_candidate_feeder_heartbeat.json", 180),
        AgentSpec("autonomous_paper_trading_loop", "autonomous_paper_trading_loop.py", ("--interval-seconds", "60"), STATE_DIR / "autonomous_paper_trading_loop.pid", STATE_DIR / "autonomous_paper_trading_loop_heartbeat.json", 180),
        AgentSpec("paper_execution_lifecycle_loop", "paper_execution_lifecycle_loop.py", ("--interval-seconds", "30"), STATE_DIR / "paper_execution_lifecycle_loop.pid", STATE_DIR / "paper_execution_lifecycle_loop_heartbeat.json", 120),
        AgentSpec("microstructure_observer_loop", "microstructure_observer_loop.py", ("--interval-seconds", "60"), STATE_DIR / "microstructure_observer_loop.pid", STATE_DIR / "microstructure_observer_loop_heartbeat.json", 180),
        AgentSpec("counterfactual_replay_agent", "counterfactual_replay_agent.py", ("--interval-seconds", "300"), STATE_DIR / "counterfactual_replay_agent.pid", STATE_DIR / "counterfactual_replay_agent_heartbeat.json", 900),
        AgentSpec("shadow_trade_evaluator_loop", "shadow_trade_evaluator_loop.py", ("--interval-seconds", "600", "--max-age-hours", "24", "--max-trades", "100"), STATE_DIR / "shadow_trade_evaluator_loop.pid", STATE_DIR / "shadow_trade_evaluator_loop_heartbeat.json", 1800),
        AgentSpec("promotion_evaluator_loop", "promotion_evaluator_loop.py", ("--interval-seconds", "300"), STATE_DIR / "promotion_evaluator_loop.pid", STATE_DIR / "promotion_evaluator_loop_heartbeat.json", 600),
        AgentSpec("self_model", "self_model.py", ("--interval-minutes", "10"), STATE_DIR / "self_model.pid", STATE_DIR / "self_model_heartbeat.json", 900),
        AgentSpec("self_improvement_agent", "self_improvement_agent.py", ("--interval-hours", "6"), STATE_DIR / "self_improvement_agent.pid", STATE_DIR / "self_improvement_agent_heartbeat.json", 28800),
        AgentSpec("daily_exam_agent", "daily_exam_agent.py", ("--check-seconds", "300"), STATE_DIR / "daily_exam_agent.pid", STATE_DIR / "daily_exam_agent_heartbeat.json", 900),
    ]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def append_jsonl(event: str, payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": utc_now(), "event": event, **payload}
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
    safe_append_event("agent_process_supervisor", event, payload, ts=row["ts"])


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None


def write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="ascii")


def is_pid_running(pid: int | None, expected_script: str | None = None) -> bool:
    if not pid:
        return False
    if os.name != "nt":
        proc = Path(f"/proc/{pid}")
        if not proc.exists():
            return False
        if expected_script:
            try:
                return expected_script in (proc / "cmdline").read_text(errors="ignore")
            except Exception:
                return True
        return True
    try:
        if not expected_script:
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
            timeout=5,
            **hidden_subprocess_kwargs(),
        )
        return result.returncode == 0
    except Exception:
        return False

def running_script_processes(expected_script: str) -> list[tuple[int, str]]:
    if os.name != "nt":
        result: list[tuple[int, str]] = []
        for proc in Path("/proc").iterdir() if Path("/proc").exists() else []:
            if not proc.name.isdigit():
                continue
            try:
                cmdline = (proc / "cmdline").read_text(errors="ignore").replace("\x00", " ")
            except Exception:
                continue
            if expected_script in cmdline and str(ROOT) in cmdline:
                result.append((int(proc.name), cmdline))
        return sorted({pid: cmdline for pid, cmdline in result}.items())
    try:
        escaped_script = expected_script.replace("'", "''")
        escaped_root = str(ROOT).replace("'", "''")
        ps = (
            f"$script='{escaped_script}'; $root='{escaped_root}'; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -like '*python*' -and $_.CommandLine -like \"*$root*\" -and $_.CommandLine -like \"*$script*\" } | "
            "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        result = subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], capture_output=True, text=True, timeout=8, **hidden_subprocess_kwargs())
        if result.returncode != 0:
            return []
        raw = result.stdout.strip()
        if not raw:
            return []
        payload = json.loads(raw)
        items = payload if isinstance(payload, list) else [payload]
        rows: list[tuple[int, int | None, str]] = []
        for item in items:
            try:
                if not isinstance(item, dict):
                    continue
                rows.append((int(item.get("ProcessId")), int(item.get("ParentProcessId")), str(item.get("CommandLine") or "").strip()))
            except Exception:
                continue
        return collapse_launcher_processes(rows)
    except Exception:
        return []

def collapse_launcher_processes(rows: list[tuple[int, int | None, str]]) -> list[tuple[int, str]]:
    """Collapse Windows venv launcher + real interpreter into one process.

    Windows venvs can leave a `venv\\Scripts\\python.exe` parent plus a base
    Python child with the same command line. Counting both makes one script look
    like two agents. Prefer the child because it owns os.getpid()/heartbeats.
    """
    parent_child_keys = {(parent_pid, cmdline) for _, parent_pid, cmdline in rows if parent_pid is not None}
    collapsed = [(pid, cmdline) for pid, _, cmdline in rows if (pid, cmdline) not in parent_child_keys]
    return sorted({pid: cmdline for pid, cmdline in collapsed}.items())

def running_script_pids(expected_script: str) -> list[int]:
    return [pid for pid, _ in running_script_processes(expected_script)]

def supervisor_loop_pids() -> list[int]:
    ignored_flags = ("--status", "--cleanup-only", "--once")
    current = os.getpid()
    return [
        pid
        for pid, cmdline in running_script_processes(SUPERVISOR_SCRIPT)
        if pid != current and not any(flag in cmdline for flag in ignored_flags)
    ]


def parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def heartbeat_age_seconds(path: Path | None) -> float | None:
    if not path:
        return None
    ts = parse_ts(read_json(path).get("ts"))
    if not ts:
        return None
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())


def stale(spec: AgentSpec) -> bool:
    if spec.max_heartbeat_age_seconds is None:
        return False
    age = heartbeat_age_seconds(spec.heartbeat_file)
    return age is None or age > spec.max_heartbeat_age_seconds


def stop_pid(pid: int | None, expected_script: str) -> None:
    if not is_pid_running(pid, expected_script):
        return
    try:
        subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", f"Stop-Process -Id {pid} -Force"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, **hidden_subprocess_kwargs())
    except Exception:
        pass

def supervised_pid_files() -> list[Path]:
    return [PID_FILE, *(spec.pid_file for spec in specs())]

def supervisor_lock_files() -> list[Path]:
    return [LOCK_FILE]

def unlink_pid_files(paths: Iterable[Path] | None = None) -> None:
    for path in paths or supervised_pid_files():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            append_jsonl("pid_file_unlink_error", {"path": str(path), "error": str(exc)[:200]})

def cleanup_runtime(exclude_current_supervisor: bool = True) -> dict:
    """Stop supervised helper processes and clear PID files.

    This is intentionally scoped to scripts in this repository. It is used for a
    clean local restart after duplicate supervisors have already been spawned.
    """
    current = os.getpid()
    stopped: dict[str, list[int]] = {}
    scripts = [SUPERVISOR_SCRIPT, *(spec.script for spec in specs())]
    for script in scripts:
        for pid in running_script_pids(script):
            if exclude_current_supervisor and script == SUPERVISOR_SCRIPT and pid == current:
                continue
            stop_pid(pid, script)
            stopped.setdefault(script, []).append(pid)
            append_jsonl("runtime_cleanup_stop", {"script": script, "pid": pid, "kept_pid": current if script == SUPERVISOR_SCRIPT else None})
    unlink_pid_files()
    unlink_pid_files(supervisor_lock_files())
    return {"stopped": stopped, "pid_files_removed": [str(path) for path in supervised_pid_files()], "lock_files_removed": [str(path) for path in supervisor_lock_files()]}

def acquire_supervisor_lock() -> bool:
    current = os.getpid()
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(current).encode("ascii"))
            finally:
                os.close(fd)
            append_jsonl("supervisor_lock_acquired", {"pid": current})
            return True
        except FileExistsError:
            owner = read_pid(LOCK_FILE)
            if owner and owner != current and is_pid_running(owner, SUPERVISOR_SCRIPT):
                append_jsonl("supervisor_lock_busy", {"pid": current, "owner_pid": owner})
                return False
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                append_jsonl("supervisor_lock_unlink_error", {"pid": current, "owner_pid": owner, "error": str(exc)[:200]})
                return False
    return False

def release_supervisor_lock() -> None:
    if read_pid(LOCK_FILE) != os.getpid():
        return
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        append_jsonl("supervisor_lock_release_error", {"pid": os.getpid(), "error": str(exc)[:200]})

def dedupe_agent_processes(spec: AgentSpec, preferred_pid: int | None) -> int | None:
    pids = running_script_pids(spec.script)
    if not pids:
        return None
    keep = preferred_pid if preferred_pid in pids else pids[0]
    for pid in pids:
        if pid != keep:
            stop_pid(pid, spec.script)
            append_jsonl("agent_duplicate_stop", {"agent": spec.name, "pid": pid, "kept_pid": keep})
    if keep != preferred_pid:
        write_pid(spec.pid_file, keep)
    return keep

def stop_other_supervisors() -> None:
    current = os.getpid()
    if read_pid(PID_FILE) != current:
        return
    duplicates = [pid for pid in supervisor_loop_pids() if pid != current]
    if duplicates:
        append_jsonl("supervisor_duplicate_detected", {"pids": duplicates, "owner_pid": current, "action": "cleanup_required"})


def start_agent(spec: AgentSpec) -> int:
    out_path = STATE_DIR / f"{spec.name}.supervisor.out.log"
    err_path = STATE_DIR / f"{spec.name}.supervisor.err.log"
    cmd = [default_python(), str(ROOT / spec.script), *spec.args]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    if spec.heartbeat_file is not None:
        try:
            spec.pid_file.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
    with out_path.open("ab") as out_fh, err_path.open("ab") as err_fh:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out_fh, stderr=err_fh, creationflags=creationflags)
    if spec.heartbeat_file is None:
        write_pid(spec.pid_file, proc.pid)
    append_jsonl("agent_start", {"agent": spec.name, "pid": proc.pid, "cmd": cmd})
    return proc.pid


def ensure_agent(spec: AgentSpec) -> dict:
    pid = read_pid(spec.pid_file)
    deduped_pid = dedupe_agent_processes(spec, pid)
    if deduped_pid:
        pid = deduped_pid
    running = is_pid_running(pid, spec.script)
    is_stale = stale(spec)
    if running and not is_stale:
        return {"agent": spec.name, "pid": pid, "running": True, "stale": False, "action": "ok"}
    if running and is_stale:
        stop_pid(pid, spec.script)
    new_pid = start_agent(spec)
    return {"agent": spec.name, "pid": new_pid, "running": True, "stale": is_stale, "action": "restarted" if running else "started"}


def write_heartbeat(rows: list[dict]) -> None:
    row = {"ts": utc_now(), "pid": os.getpid(), "status": "ok", "agents": rows}
    HEARTBEAT_PATH.write_text(json.dumps(row, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    safe_upsert_heartbeat("agent_process_supervisor", "ok", row, ts=row["ts"])


def run_once() -> list[dict]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rows = [ensure_agent(spec) for spec in specs()]
    write_heartbeat(rows)
    return rows


def status() -> int:
    rows = []
    for spec in specs():
        pid = read_pid(spec.pid_file)
        matching_pids = running_script_pids(spec.script)
        rows.append(
            {
                "agent": spec.name,
                "pid": pid,
                "matching_pids": matching_pids,
                "duplicate_count": max(0, len(matching_pids) - 1),
                "running": is_pid_running(pid, spec.script),
                "heartbeat_age_seconds": heartbeat_age_seconds(spec.heartbeat_file),
                "stale": stale(spec),
            }
        )
    supervisor_pids = supervisor_loop_pids()
    print(
        json.dumps(
            {
                "supervisor_pid": read_pid(PID_FILE),
                "supervisor_pids": supervisor_pids,
                "supervisor_duplicate_count": max(0, len(supervisor_pids) - 1),
                "agents": rows,
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def run_loop(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not acquire_supervisor_lock():
        owner = read_pid(LOCK_FILE)
        print(f"agent process supervisor already running pid={owner}", flush=True)
        return 0
    existing_pid = read_pid(PID_FILE)
    append_jsonl("supervisor_loop_enter", {"pid": os.getpid(), "existing_pid": existing_pid, "once": bool(args.once)})
    if existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid, "agent_process_supervisor.py"):
        print(f"agent process supervisor already running pid={existing_pid}", flush=True)
        append_jsonl("supervisor_loop_existing_exit", {"pid": os.getpid(), "existing_pid": existing_pid})
        release_supervisor_lock()
        return 0
    write_pid(PID_FILE, os.getpid())
    append_jsonl("supervisor_loop_claimed", {"pid": os.getpid()})
    stop_other_supervisors()
    append_jsonl("supervisor_loop_after_stop_others", {"pid": os.getpid(), "supervisor_pids": supervisor_loop_pids()})
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        try:
            stop_other_supervisors()
            rows = run_once()
            restarted = [row["agent"] for row in rows if row.get("action") != "ok"]
            print(f"agent_supervisor ok restarted={','.join(restarted) if restarted else 'none'}", flush=True)
        except Exception as exc:
            append_jsonl("supervisor_error", {"error": str(exc)[:300]})
        if args.once:
            break
        time.sleep(args.check_seconds)
    append_jsonl("supervisor_stop", {"pid": os.getpid()})
    release_supervisor_lock()
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Supervise trading-agent background helper processes")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--restart-clean", action="store_true", help="stop duplicate supervised helpers, clear PID files, then run supervisor")
    parser.add_argument("--cleanup-only", action="store_true", help="stop supervised helpers and clear PID files, then exit")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check-seconds", type=float, default=60.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.check_seconds <= 0:
        parser.error("--check-seconds must be positive")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        return status()
    if args.restart_clean or args.cleanup_only:
        summary = cleanup_runtime(exclude_current_supervisor=True)
        print(json.dumps({"cleanup": summary}, ensure_ascii=True, sort_keys=True), flush=True)
        if args.cleanup_only:
            return 0
    return run_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
