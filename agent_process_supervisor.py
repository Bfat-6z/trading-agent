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

from agent_runtime_contract import specs_from_supervisor, validate_agents
from alert_manager import open_incident
from event_store import safe_append_event, safe_upsert_heartbeat

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
PID_FILE = STATE_DIR / "agent_process_supervisor.pid"
LOCK_FILE = STATE_DIR / "agent_process_supervisor.lock"
STOP_FILE = STATE_DIR / "STOP_AGENT_PROCESS_SUPERVISOR"
LOG_FILE = STATE_DIR / "agent_process_supervisor.jsonl"
HEARTBEAT_PATH = STATE_DIR / "agent_process_supervisor_heartbeat.json"
SUPERVISOR_SCRIPT = "agent_process_supervisor.py"
RESTART_STATE_PATH = STATE_DIR / "agent_restart_state.json"
RESTART_WINDOW_SECONDS = 900
RESTART_MAX_PER_WINDOW = 3
RESTART_BASE_BACKOFF_SECONDS = 5
CHILD_ENV_ALLOWLIST = {
    "ALLUSERSPROFILE",
    "APPDATA",
    "ANTHROPIC_API_KEY",
    "BASESCAN_API_KEY",
    "COMSPEC",
    "CRYPTOPANIC_API_KEY",
    "GOPLUS_API_KEY",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "LOG_LEVEL",
    "LUNARCRUSH_API_KEY",
    "MORALIS_API_KEY",
    "NINEROUTER_API_KEY",
    "NINEROUTER_BASE_URL",
    "OPENAI_API_KEY",
    "OS",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "STATE_DIR",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TRADINGAGENTS_DEEP_THINK_LLM",
    "TRADINGAGENTS_JUDGE_LLM",
    "TRADINGAGENTS_LLM_PROVIDER",
    "TRADINGAGENTS_QUICK_THINK_LLM",
    "TRADING_AGENT_DASHBOARD_TOKEN",
    "TRADING_AGENT_MODE",
    "TRADING_AGENT_PAPER_ACCOUNT_USDT",
    "TRADING_AGENT_PAPER_EXPLORATION",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
}
LIVE_ENV_DENYLIST = {
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_FUTURES_API_KEY",
    "BINANCE_FUTURES_API_SECRET",
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "PRIVATE_KEY",
    "WALLET_PRIVATE_KEY",
}


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
        # Bind 0.0.0.0 so the Cloudflare tunnel reaches it; /api/* stays gated by
        # TRADING_AGENT_DASHBOARD_TOKEN (fleet_watchdog sets it from the token
        # file). Without that env the dashboard falls back to local-only.
        AgentSpec("dashboard", "agent_status_dashboard.py", ("--host", "0.0.0.0", "--port", "8090"), STATE_DIR / "agent_status_dashboard.pid", None, None),
        AgentSpec("host_runtime_monitor", "host_runtime_monitor.py", ("--interval-seconds", "300"), STATE_DIR / "host_runtime_monitor.pid", STATE_DIR / "host_runtime_monitor_heartbeat.json", 900),
        AgentSpec("market_observer", "market_observer.py", tuple(), STATE_DIR / "market_observer.pid", STATE_DIR / "market_observer_heartbeat.json", 420),
        AgentSpec("news_observer", "news_observer.py", tuple(), STATE_DIR / "news_observer.pid", STATE_DIR / "news_observer_heartbeat.json", 900),
        # CUT (gpt-5.5 architecture review 2026-07-05): alpha theater — the mission bot,
        # method lab, deep_validation and forward_test have ZERO dependency on it. Kept
        # the file; just no longer supervised. Re-add this line to revive.
        # AgentSpec("dream_cycle", "dream_cycle.py", tuple(), STATE_DIR / "dream_cycle.pid", STATE_DIR / "dream_cycle_heartbeat.json", 2400),
        # CUT (second-brain P4, gpt-5.5 triage): template-string "lessons"/dreams with
        # near-zero signal, off every decision path; lesson mining is now deterministic
        # in brain.mine_lessons() from real trade autopsies. Re-add to revive.
        # AgentSpec("reflection_agent", "reflection_agent.py", ("--interval-hours", "0.5"), STATE_DIR / "reflection_agent.pid", STATE_DIR / "reflection_agent_heartbeat.json", 2400),
        AgentSpec("cognitive_supervisor", "cognitive_supervisor.py", ("--interval-minutes", "20"), STATE_DIR / "cognitive_supervisor.pid", STATE_DIR / "cognitive_supervisor_heartbeat.json", 1500),
        AgentSpec("llm_reasoning_agent", "llm_reasoning_agent.py", ("--interval-minutes", "60"), STATE_DIR / "llm_reasoning_agent.pid", STATE_DIR / "llm_reasoning_agent_heartbeat.json", 900),
        AgentSpec("paper_candidate_feeder", "paper_candidate_feeder.py", ("--interval-seconds", "60"), STATE_DIR / "paper_candidate_feeder.pid", STATE_DIR / "paper_candidate_feeder_heartbeat.json", 180),
        AgentSpec("autonomous_paper_trading_loop", "autonomous_paper_trading_loop.py", ("--interval-seconds", "60"), STATE_DIR / "autonomous_paper_trading_loop.pid", STATE_DIR / "autonomous_paper_trading_loop_heartbeat.json", 180),
        AgentSpec("paper_execution_lifecycle_loop", "paper_execution_lifecycle_loop.py", ("--interval-seconds", "30"), STATE_DIR / "paper_execution_lifecycle_loop.pid", STATE_DIR / "paper_execution_lifecycle_loop_heartbeat.json", 120),
        AgentSpec("microstructure_observer_loop", "microstructure_observer_loop.py", ("--interval-seconds", "60"), STATE_DIR / "microstructure_observer_loop.pid", STATE_DIR / "microstructure_observer_loop_heartbeat.json", 180),
        AgentSpec("microstructure_flow_factory", "microstructure_flow_factory.py", ("--interval-seconds", "60"), STATE_DIR / "microstructure_flow_factory.pid", STATE_DIR / "microstructure_flow_factory_heartbeat.json", 180),
        AgentSpec("whale_flow_observer", "whale_flow_observer.py", ("--interval-seconds", "180"), STATE_DIR / "whale_flow_observer.pid", STATE_DIR / "whale_flow_observer_heartbeat.json", 600),
        AgentSpec("forward_test_harness", "forward_test_harness.py", ("--interval-seconds", "900"), STATE_DIR / "forward_test" / "forward_test_harness.pid", STATE_DIR / "forward_test" / "forward_test_harness_heartbeat.json", 2700),
        AgentSpec("forward_strategy_paper", "forward_strategy_paper.py", ("--interval-seconds", "1800"), STATE_DIR / "forward_strategy" / "forward_strategy_paper.pid", STATE_DIR / "forward_strategy" / "forward_strategy_paper_heartbeat.json", 4200),
        AgentSpec("llm_trader", "llm_trader.py", ("--interval-seconds", "90"), STATE_DIR / "llm_trader" / "llm_trader.pid", STATE_DIR / "llm_trader" / "llm_trader_heartbeat.json", 1200),
        AgentSpec("manual_trader", "manual_trader.py", ("--interval-seconds", "60"), STATE_DIR / "manual_trader" / "manual_trader.pid", STATE_DIR / "manual_trader" / "manual_trader_heartbeat.json", 600),
        # Method Lab (24/7 research->backtest->curate; rounds are heavy, 3h apart)
        AgentSpec("method_lab_runner", "method_lab_runner.py", ("--interval", "10800"), STATE_DIR / "method_lab_runner.pid", STATE_DIR / "method_lab_heartbeat.json", 14400),
        # Signal follower (paper-trades Telegram alerts, measures per-channel win rate)
        AgentSpec("signal_follower", "signal_follower.py", ("--interval", "300"), STATE_DIR / "signal_follower.pid", STATE_DIR / "signal_follower_heartbeat.json", 1800),
        # Forward test (paper shadow-ledger for below-bar candidate methods on fresh
        # LIVE bars; owner 'cam forward-test um_reclaim_06'; promotes if edge persists)
        AgentSpec("forward_test", "forward_test.py", ("--interval", "300"), STATE_DIR / "forward_test.pid", STATE_DIR / "forward_test_heartbeat.json", 1200),
        # Lane farm (owner: '10 kênh trade, mỗi kênh 100u, rút tổng hợp bài học') —
        # 10 parallel paper experiment lanes incl. a random-entry control; feeds
        # trade_autopsy/lesson mining; paper-only, mission untouched.
        AgentSpec("lane_farm", "lane_farm.py", ("--interval", "300"), STATE_DIR / "lane_farm.pid", STATE_DIR / "lane_farm_heartbeat.json", 1200),
        AgentSpec("lane_farm_1h", "lane_farm.py", ("--interval", "600", "--tf", "1h"), STATE_DIR / "lane_farm_1h.pid", STATE_DIR / "lane_farm_1h_heartbeat.json", 2400),

        # Method matrix (owner /goal: 'áp nhiều pp lên setup, cái nào winrate cao nhất')
        # — decision-support only, backtests all ~118 method defs -> live signal matrix.
        # Places no orders; the ARMED gate still rules execution. Stats cached 3h.
        AgentSpec("method_matrix", "method_matrix.py", ("--interval", "600"), STATE_DIR / "method_matrix.pid", STATE_DIR / "method_matrix_heartbeat.json", 5400),
        # Lane promotion (owner: 'dồn pp winrate cao nhất về line main'): funnels lane
        # methods that are OOS-significant (Šidák-corrected over ~100 lanes) into the
        # mission armed set. Paper-only; hand-armed methods always kept. Runs every 30m.
        AgentSpec("lane_promotion", "lane_promotion.py", ("--interval", "1800"), STATE_DIR / "lane_promotion.pid", STATE_DIR / "lane_promotion_heartbeat.json", 5400),
        AgentSpec("counterfactual_replay_agent", "counterfactual_replay_agent.py", ("--interval-seconds", "300"), STATE_DIR / "counterfactual_replay_agent.pid", STATE_DIR / "counterfactual_replay_agent_heartbeat.json", 900),
        AgentSpec("learning_exam_benchmark", "learning_exam_benchmark.py", ("--interval-seconds", "3600"), STATE_DIR / "learning_exam_benchmark.pid", STATE_DIR / "learning_exam_benchmark_heartbeat.json", 4500),
        AgentSpec("test_result_memory_agent", "test_result_memory_agent.py", ("--interval-seconds", "1800"), STATE_DIR / "test_result_memory_agent.pid", STATE_DIR / "test_result_memory_agent_heartbeat.json", 2700),
        AgentSpec("shadow_trade_evaluator_loop", "shadow_trade_evaluator_loop.py", ("--interval-seconds", "600", "--max-age-hours", "24", "--max-trades", "100"), STATE_DIR / "shadow_trade_evaluator_loop.pid", STATE_DIR / "shadow_trade_evaluator_loop_heartbeat.json", 1800),
        AgentSpec("promotion_evaluator_loop", "promotion_evaluator_loop.py", ("--interval-seconds", "300"), STATE_DIR / "promotion_evaluator_loop.pid", STATE_DIR / "promotion_evaluator_loop_heartbeat.json", 600),
        # CUT (gpt-5.5 review 2026-07-05): no direct alpha value, 0 mission deps. Re-add to revive.
        # AgentSpec("self_model", "self_model.py", ("--interval-minutes", "10"), STATE_DIR / "self_model.pid", STATE_DIR / "self_model_heartbeat.json", 900),
        # CUT (second-brain P4): its durable-memory write role is superseded by the
        # deterministic brain.db registry + mechanical lessons; its evidence sources
        # were largely the already-cut theater agents. The data_trust gating CODE
        # stays in the repo for reuse. Re-add to revive.
        # AgentSpec("memory_consolidation_agent", "memory_consolidation_agent.py", ("--interval-seconds", "1800"), STATE_DIR / "memory_consolidation_agent.pid", STATE_DIR / "memory_consolidation_agent_heartbeat.json", 2700),
        # CUT (gpt-5.5 review 2026-07-05): 'complexity factory' + ran with --apply (autonomous
        # self-modification) for no measured edge; 0 mission deps. Re-add to revive.
        # AgentSpec("skill_forge_agent", "skill_forge_agent.py", ("--interval-seconds", "1800", "--apply"), STATE_DIR / "skill_forge_agent.pid", STATE_DIR / "skill_forge_agent_heartbeat.json", 2700),
        # CUT (post-ship sweep 2026-07-06): its food chain is gone — it consumed
        # reflection_agent's profile.json (cut) and fed self_model (cut). Theater tier.
        # AgentSpec("self_improvement_agent", "self_improvement_agent.py", ("--interval-hours", "6"), STATE_DIR / "self_improvement_agent.pid", STATE_DIR / "self_improvement_agent_heartbeat.json", 28800),
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


def scrub_child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    source = dict(env or os.environ)
    clean = {key: value for key, value in source.items() if key.upper() in CHILD_ENV_ALLOWLIST and key.upper() not in LIVE_ENV_DENYLIST}
    clean["PYTHONUNBUFFERED"] = "1"   # crash tracebacks must hit the log before a kill
    clean["TRADING_AGENT_LIVE_ORDERS"] = "false"
    clean["TRADING_AGENT_CHILD_ENV_SCRUBBED"] = "1"
    return clean


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
    # numeric epoch timestamps (seconds or ms) — some agents write int ts; without
    # this they parse as None -> "permanently stale" -> kill-loop -> quarantine.
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        try:
            num = float(value)
            if num > 1e12:
                num /= 1000.0
            return datetime.fromtimestamp(num, tz=timezone.utc)
        except Exception:
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

def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def heartbeat_age_seconds(path: Path | None) -> float | None:
    if not path:
        return None
    ts = parse_ts(read_json(path).get("ts"))
    if not ts:
        return None
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())


# bughunt 2026-07-08 (R6 #1): a freshly-spawned agent has NO heartbeat yet (age is None). The old
# `stale = age is None or ...` killed it on the very next cycle before it could prove liveness ->
# relaunch -> 3x -> 6h quarantine = the "mission stuck for hours". Track spawn time and give a
# startup grace during which "no heartbeat yet" is NOT stale.
STARTUP_GRACE_SECONDS = 180.0
_started_at: dict[str, float] = {}


def stale(spec: AgentSpec) -> bool:
    if spec.max_heartbeat_age_seconds is None:
        return False
    age = heartbeat_age_seconds(spec.heartbeat_file)
    if age is None:
        started = _started_at.get(spec.name)
        if started is not None and (time.time() - started) < STARTUP_GRACE_SECONDS:
            return False                        # still inside its startup grace -> not stale
        return True
    return age > spec.max_heartbeat_age_seconds

def restart_gate(agent: str, *, now: datetime | None = None, state_path: Path = RESTART_STATE_PATH) -> dict:
    current = now or datetime.now(timezone.utc)
    state = read_json(state_path)
    agents = state.get("agents") if isinstance(state.get("agents"), dict) else {}
    row = agents.get(agent) if isinstance(agents.get(agent), dict) else {}
    if row.get("state") == "quarantined":
        # TTL: auto-release after 6h — quarantine protects against crash-loops, it
        # must not be a permanent death sentence (a heartbeat-format bug killed two
        # healthy agents for good before this).
        qat = parse_ts(row.get("quarantined_at"))
        if qat and (current - qat).total_seconds() > 6 * 3600:
            row = {"attempts": [], "state": "active", "released_from_quarantine_at": current.isoformat(timespec="seconds")}
            agents[agent] = row
            state["agents"] = agents
            write_json(state_path, state)
        else:
            return {
                "allowed": False,
                "reason": "restart_quarantined",
                "original_reason": row.get("reason") or "restart_circuit_breaker",
                "restart_count_window": int(row.get("restart_count_window") or 0),
                "backoff_seconds": None,
                "quarantined_at": row.get("quarantined_at"),
            }
    attempts = []
    for value in row.get("attempts", []) if isinstance(row.get("attempts"), list) else []:
        parsed = parse_ts(value)
        if parsed and (current - parsed).total_seconds() <= RESTART_WINDOW_SECONDS:
            attempts.append(parsed.isoformat(timespec="seconds"))
    if len(attempts) >= RESTART_MAX_PER_WINDOW:
        row.update(
            {
                "state": "quarantined",
                "quarantined_at": current.isoformat(timespec="seconds"),
                "reason": "restart_circuit_breaker",
                "restart_count_window": len(attempts),
            }
        )
        agents[agent] = row
        state["agents"] = agents
        state["updated_at"] = current.isoformat(timespec="seconds")
        write_json(state_path, state)
        return {"allowed": False, "reason": "restart_circuit_breaker", "restart_count_window": len(attempts), "backoff_seconds": None}
    backoff_seconds = min(300, RESTART_BASE_BACKOFF_SECONDS * (2 ** max(0, len(attempts) - 1)))
    if attempts:
        last = parse_ts(attempts[-1])
        elapsed = (current - last).total_seconds() if last else backoff_seconds
        if elapsed < backoff_seconds:
            return {
                "allowed": False,
                "reason": "restart_backoff_active",
                "restart_count_window": len(attempts),
                "backoff_seconds": backoff_seconds,
                "retry_after_seconds": round(backoff_seconds - elapsed, 3),
            }
    return {"allowed": True, "reason": "ok", "restart_count_window": len(attempts), "backoff_seconds": backoff_seconds}

def record_restart_attempt(agent: str, *, now: datetime | None = None, state_path: Path = RESTART_STATE_PATH) -> dict:
    current = now or datetime.now(timezone.utc)
    state = read_json(state_path)
    agents = state.get("agents") if isinstance(state.get("agents"), dict) else {}
    row = agents.get(agent) if isinstance(agents.get(agent), dict) else {}
    attempts = []
    for value in row.get("attempts", []) if isinstance(row.get("attempts"), list) else []:
        parsed = parse_ts(value)
        if parsed and (current - parsed).total_seconds() <= RESTART_WINDOW_SECONDS:
            attempts.append(parsed.isoformat(timespec="seconds"))
    attempts.append(current.isoformat(timespec="seconds"))
    row.update({"state": row.get("state") if row.get("state") == "quarantined" else "active", "attempts": attempts, "restart_count_window": len(attempts), "last_restart_at": attempts[-1]})
    agents[agent] = row
    state["schema_version"] = 1
    state["updated_at"] = attempts[-1]
    state["window_seconds"] = RESTART_WINDOW_SECONDS
    state["max_per_window"] = RESTART_MAX_PER_WINDOW
    state["agents"] = agents
    write_json(state_path, state)
    return row

def open_restart_incident(spec: AgentSpec, gate: dict) -> None:
    try:
        open_incident(
            "Sev2",
            "agent restart circuit breaker",
            {"agent": spec.name, "script": spec.script, **gate},
            source="agent_process_supervisor",
            owner="operator",
            runbook_id="runbook_restart_circuit_breaker",
            dedupe_key=f"restart_circuit:{spec.name}",
            action_required="quarantine_and_review_before_restart",
        )
    except Exception as exc:
        append_jsonl("incident_emit_error", {"agent": spec.name, "error": str(exc)[:200]})


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
    # bughunt R6 #4: --restart-clean must ALSO clear what previously made it a no-op — the restart/
    # quarantine state (else a quarantined agent stays quarantined up to 6h), stale heartbeat files
    # (else the just-relaunched agent is instantly judged stale -> killed, R6 #1), and the agents'
    # own child loop.locks (the fresh-mtime lock that self-blocked llm_trader).
    _extra_cleared: list[str] = []
    for p in [RESTART_STATE_PATH, *(sp.heartbeat_file for sp in specs() if sp.heartbeat_file),
              STATE_DIR / "llm_trader" / "loop.lock", STATE_DIR / "lane_farm.lock",
              STATE_DIR / "lane_farm_1h.lock", STATE_DIR / "manual_trader" / "loop.lock"]:
        try:
            if p and Path(p).exists():
                Path(p).unlink()
                _extra_cleared.append(str(p))
        except Exception as exc:
            append_jsonl("runtime_cleanup_extra_error", {"path": str(p), "error": str(exc)[:120]})
    _started_at.clear()                          # forget spawn times so the grace re-arms cleanly
    return {"stopped": stopped, "pid_files_removed": [str(path) for path in supervised_pid_files()],
            "lock_files_removed": [str(path) for path in supervisor_lock_files()], "extra_cleared": _extra_cleared}

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
        for pid in duplicates:
            stop_pid(pid, SUPERVISOR_SCRIPT)
        append_jsonl("supervisor_duplicate_detected", {"pids": duplicates, "owner_pid": current, "action": "stopped"})


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
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out_fh, stderr=err_fh, creationflags=creationflags, env=scrub_child_env())
    _started_at[spec.name] = time.time()        # bughunt R6 #1: stamp spawn time for the startup grace
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
    restart_state_path = spec.pid_file.parent / "agent_restart_state.json"
    gate = restart_gate(spec.name, state_path=restart_state_path)
    if not gate.get("allowed"):
        if gate.get("reason") == "restart_circuit_breaker":
            append_jsonl("agent_restart_quarantined", {"agent": spec.name, **gate})
            open_restart_incident(spec, gate)
            action = "quarantined"
        elif gate.get("reason") == "restart_quarantined":
            append_jsonl("agent_restart_quarantined", {"agent": spec.name, **gate})
            action = "quarantined"
        else:
            append_jsonl("agent_restart_deferred", {"agent": spec.name, **gate})
            action = "restart_deferred"
        return {"agent": spec.name, "pid": pid, "running": bool(running), "stale": is_stale, "action": action, "restart_gate": gate}
    try:
        new_pid = start_agent(spec)
    except Exception as exc:
        attempt = record_restart_attempt(spec.name, state_path=restart_state_path)
        failure_gate = restart_gate(spec.name, state_path=restart_state_path)
        append_jsonl("agent_start_failed", {"agent": spec.name, "error": str(exc)[:240], "restart_count_window": attempt.get("restart_count_window"), "restart_gate": failure_gate})
        if failure_gate.get("reason") == "restart_circuit_breaker":
            open_restart_incident(spec, failure_gate)
        return {"agent": spec.name, "pid": pid, "running": False, "stale": is_stale, "action": "start_failed", "restart_gate": failure_gate, "error": str(exc)[:240]}
    record_restart_attempt(spec.name, state_path=restart_state_path)
    return {"agent": spec.name, "pid": new_pid, "running": True, "stale": is_stale, "action": "restarted" if running else "started"}


def write_heartbeat(rows: list[dict]) -> None:
    row = {"ts": utc_now(), "pid": os.getpid(), "status": "ok", "agents": rows}
    HEARTBEAT_PATH.write_text(json.dumps(row, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    safe_upsert_heartbeat("agent_process_supervisor", "ok", row, ts=row["ts"])


def run_once() -> list[dict]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    current_specs = specs()
    rows = [ensure_agent(spec) for spec in current_specs]
    validate_agents(specs_from_supervisor(current_specs), output_path=STATE_DIR / "agent_registry.json")
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
