"""Single-page status dashboard for the trading agent.

This intentionally uses only the Python standard library so it can run even
when FastAPI or frontend tooling is slow/unavailable. It is read-only: no live
orders, no API-key access, no mutation of trading state.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import mimetypes
import os
import socketserver
import subprocess
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

from decision_explainer import explain_decision
from agent_work_queue import queue_summary
from learning_dashboard_data import load_phase_b_learning
from market_learner import valid_paper_close, valid_paper_open

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"

HEARTBEAT_FILES = {
    "agent_process_supervisor": STATE_DIR / "agent_process_supervisor_heartbeat.json",
    "market_observer": STATE_DIR / "market_observer_heartbeat.json",
    "news_observer": STATE_DIR / "news_observer_heartbeat.json",
    "reflection_agent": STATE_DIR / "reflection_agent_heartbeat.json",
    "dream_cycle": STATE_DIR / "dream_cycle_heartbeat.json",
    "cognitive_supervisor": STATE_DIR / "cognitive_supervisor_heartbeat.json",
    "llm_reasoning_agent": STATE_DIR / "llm_reasoning_agent_heartbeat.json",
    "self_improvement_agent": STATE_DIR / "self_improvement_agent_heartbeat.json",
    "daily_exam_agent": STATE_DIR / "daily_exam_agent_heartbeat.json",
    "paper_candidate_feeder": STATE_DIR / "paper_candidate_feeder_heartbeat.json",
    "autonomous_paper_trading_loop": STATE_DIR / "autonomous_paper_trading_loop_heartbeat.json",
    "paper_execution_lifecycle_loop": STATE_DIR / "paper_execution_lifecycle_loop_heartbeat.json",
    "microstructure_observer_loop": STATE_DIR / "microstructure_observer_loop_heartbeat.json",
    "counterfactual_replay_agent": STATE_DIR / "counterfactual_replay_agent_heartbeat.json",
    "promotion_evaluator_loop": STATE_DIR / "promotion_evaluator_loop_heartbeat.json",
    "self_model": STATE_DIR / "self_model_heartbeat.json",
}

HEARTBEAT_FRESH_LIMITS = {
    "agent_process_supervisor": 180,
    "market_observer": 420,
    "news_observer": 900,
    "dream_cycle": 2400,
    "reflection_agent": 2400,
    "cognitive_supervisor": 1500,
    "llm_reasoning_agent": 900,
    "paper_candidate_feeder": 180,
    "autonomous_paper_trading_loop": 180,
    "paper_execution_lifecycle_loop": 120,
    "microstructure_observer_loop": 180,
    "counterfactual_replay_agent": 900,
    "promotion_evaluator_loop": 600,
    "self_model": 900,
    "self_improvement_agent": 28800,
    "daily_exam_agent": 900,
}

LOG_FILES = {
    "scalp_autotrader": STATE_DIR / "scalp_autotrader.jsonl",
    "scalp_watchdog": STATE_DIR / "scalp_watchdog.jsonl",
    "autotrader_err": STATE_DIR / "scalp_autotrader.err.log",
    "autotrader_out": STATE_DIR / "scalp_autotrader.out.log",
    "market_updates": STATE_DIR / "market_updates.jsonl",
    "news_events": MEMORY_DIR / "news_events.jsonl",
    "paper_lifecycle": MEMORY_DIR / "paper_trades.jsonl",
}

LIVE_MONITORS = {
    "unified_monitor": {
        "script": "unified_monitor.py",
        "role": "external_live_sl_tp_monitor",
        "agent_controls": False,
        "can_submit_reduce_only_orders": True,
    }
}

def hidden_subprocess_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


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


def age_seconds(value: object) -> float | None:
    parsed = parse_ts(value)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def human_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def read_jsonl_tail(path: Path, max_lines: int = 300) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]:
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
        except Exception:
            continue
    return rows


def tail_text(path: Path, lines: int = 120) -> str:
    if not path.exists():
        return "(file not found)"
    data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-max(1, lines):])


def file_mtime_age(path: Path) -> float | None:
    if not path.exists():
        return None
    return max(0.0, time.time() - path.stat().st_mtime)


def pid_running(pid: object) -> bool | None:
    try:
        int_pid = int(pid)
    except Exception:
        return None
    if int_pid <= 0:
        return False
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int_pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    return Path(f"/proc/{int_pid}").exists()

def collapse_process_rows(rows: list[dict]) -> list[dict]:
    parent_child_keys = {
        (int(row.get("ParentProcessId")), str(row.get("CommandLine") or "").strip())
        for row in rows
        if row.get("ParentProcessId") is not None
    }
    collapsed = []
    seen: set[int] = set()
    for row in rows:
        try:
            pid = int(row.get("ProcessId"))
        except Exception:
            continue
        cmdline = str(row.get("CommandLine") or "").strip()
        if (pid, cmdline) in parent_child_keys:
            continue
        if pid in seen:
            continue
        seen.add(pid)
        collapsed.append({"pid": pid, "parent_pid": row.get("ParentProcessId"), "command": cmdline, "executable": row.get("ExecutablePath")})
    return sorted(collapsed, key=lambda row: row["pid"])

def script_processes(script: str) -> list[dict]:
    if os.name != "nt":
        rows = []
        for proc in Path("/proc").iterdir() if Path("/proc").exists() else []:
            if not proc.name.isdigit():
                continue
            try:
                cmdline = (proc / "cmdline").read_text(errors="ignore").replace("\x00", " ")
            except Exception:
                continue
            if script in cmdline and str(ROOT) in cmdline:
                rows.append({"ProcessId": int(proc.name), "ParentProcessId": None, "CommandLine": cmdline, "ExecutablePath": None})
        return collapse_process_rows(rows)
    try:
        escaped_script = script.replace("'", "''")
        escaped_root = str(ROOT).replace("'", "''")
        ps = (
            f"$script='{escaped_script}'; $root='{escaped_root}'; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -like '*python*' -and $_.CommandLine -like \"*$root*\" -and $_.CommandLine -like \"*$script*\" } | "
            "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
        )
        result = subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], capture_output=True, text=True, timeout=6, **hidden_subprocess_kwargs())
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        items = payload if isinstance(payload, list) else [payload]
        return collapse_process_rows([item for item in items if isinstance(item, dict)])
    except Exception:
        return []

def live_monitor_status() -> list[dict]:
    rows = []
    for name, config in LIVE_MONITORS.items():
        processes = script_processes(str(config["script"]))
        state = "missing" if not processes else "ok" if len(processes) == 1 else "duplicate"
        rows.append(
            {
                "name": name,
                "script": config["script"],
                "state": state,
                "running": bool(processes),
                "process_count": len(processes),
                "pids": [row["pid"] for row in processes],
                "role": config["role"],
                "agent_controls": bool(config["agent_controls"]),
                "can_submit_reduce_only_orders": bool(config["can_submit_reduce_only_orders"]),
                "processes": processes,
            }
        )
    return rows


def heartbeat_status(name: str, path: Path) -> dict:
    payload = read_json(path)
    ts = payload.get("ts")
    age = age_seconds(ts)
    heartbeat_pid = payload.get("pid")
    pid = heartbeat_pid
    running = pid_running(pid)
    pid_file_pid = read_pid_file(STATE_DIR / f"{name}.pid")
    if running is False and pid_file_pid and pid_file_pid != heartbeat_pid and pid_running(pid_file_pid):
        pid = pid_file_pid
        running = True
    fresh_limit = HEARTBEAT_FRESH_LIMITS.get(name, 120)
    if not payload:
        state = "missing"
    elif running is False:
        state = "dead"
    elif age is None:
        state = "unknown"
    elif age <= fresh_limit:
        state = "ok"
    else:
        state = "stale"
    return {
        "name": name,
        "state": state,
        "path": str(path),
        "ts": ts,
        "age_seconds": age,
        "age": human_age(age),
        "pid": pid,
        "heartbeat_pid": heartbeat_pid,
        "running": running,
        "status": payload.get("status"),
        "payload": payload,
    }


def summarize_paper(rows: list[dict]) -> dict:
    closes = [row for row in rows if valid_paper_close(row)]
    opens = [row for row in rows if valid_paper_open(row)]
    latest_open = opens[-1] if opens else None
    latest_close = closes[-1] if closes else None
    latest_open_ts = parse_ts((latest_open or {}).get("ts"))
    latest_close_ts = parse_ts((latest_close or {}).get("ts"))
    inferred_open = bool(latest_open_ts and (not latest_close_ts or latest_open_ts > latest_close_ts))
    wins = sum(1 for row in closes if safe_float(row.get("net")) > 0)
    losses = sum(1 for row in closes if safe_float(row.get("net")) < 0)
    net = sum(safe_float(row.get("net")) for row in closes)
    trades = len(closes)
    return {
        "opens": len(opens),
        "closes": trades,
        "wins": wins,
        "losses": losses,
        "net": round(net, 8),
        "win_rate": round(wins / trades, 4) if trades else 0.0,
        "risk_blocks": sum(1 for row in rows if row.get("event") in {"risk_block", "memory_bias_filter"}),
        "latest_open": latest_open,
        "latest_close": latest_close,
        "inferred_position_open": inferred_open,
        "latest_events": rows[-20:],
    }

def summarize_paper_account(account: dict) -> dict:
    open_positions = account.get("open_positions") if isinstance(account.get("open_positions"), list) else []
    open_margin = sum(safe_float(pos.get("margin")) for pos in open_positions)
    open_notional = sum(safe_float(pos.get("notional")) for pos in open_positions)
    return {
        "open_positions": open_positions,
        "open_position_count": len(open_positions),
        "open_margin": round(open_margin, 8),
        "open_notional": round(open_notional, 8),
    }

def trade_r_multiple(row: dict) -> float | None:
    entry = safe_float(row.get("entry"))
    sl = safe_float(row.get("sl"))
    qty = safe_float(row.get("qty"))
    net = safe_float(row.get("net"))
    side = str(row.get("side") or "").upper()
    if entry <= 0 or sl <= 0 or qty <= 0:
        return None
    if side == "LONG":
        risk_amount = max(0.0, (entry - sl) * qty)
    elif side == "SHORT":
        risk_amount = max(0.0, (sl - entry) * qty)
    else:
        return None
    if risk_amount <= 0:
        return None
    return net / risk_amount

def update_trade_bucket(store: dict[str, dict], key: object, row: dict, net: float) -> None:
    bucket_key = str(key or "unknown")
    bucket = store.setdefault(
        bucket_key,
        {"key": bucket_key, "trades": 0, "wins": 0, "losses": 0, "net": 0.0, "win_sum": 0.0, "loss_sum": 0.0, "notional_sum": 0.0, "fees": 0.0, "last_ts": ""},
    )
    bucket["trades"] += 1
    bucket["net"] += net
    bucket["notional_sum"] += safe_float(row.get("notional"))
    bucket["fees"] += safe_float(row.get("fee") or row.get("fees"))
    bucket["last_ts"] = str(row.get("close_ts") or row.get("ts") or bucket.get("last_ts") or "")
    if net > 0:
        bucket["wins"] += 1
        bucket["win_sum"] += net
    elif net < 0:
        bucket["losses"] += 1
        bucket["loss_sum"] += net

def finalize_trade_buckets(store: dict[str, dict], limit: int = 10) -> list[dict]:
    rows: list[dict] = []
    for bucket in store.values():
        trades = int(bucket.get("trades") or 0)
        wins = int(bucket.get("wins") or 0)
        losses = int(bucket.get("losses") or 0)
        win_sum = safe_float(bucket.get("win_sum"))
        loss_sum = safe_float(bucket.get("loss_sum"))
        rows.append(
            {
                "key": bucket.get("key"),
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / trades, 4) if trades else 0.0,
                "net": round(safe_float(bucket.get("net")), 8),
                "expectancy": round(safe_float(bucket.get("net")) / trades, 8) if trades else 0.0,
                "profit_factor": round(win_sum / abs(loss_sum), 4) if loss_sum < 0 else (999.0 if win_sum > 0 else 0.0),
                "avg_notional": round(safe_float(bucket.get("notional_sum")) / trades, 8) if trades else 0.0,
                "fees": round(safe_float(bucket.get("fees")), 8),
                "last_ts": bucket.get("last_ts") or "",
            }
        )
    rows.sort(key=lambda row: (row["trades"], abs(row["net"])), reverse=True)
    return rows[:limit]

def rolling_trade_window(closes: list[dict], size: int) -> dict:
    rows = closes[-size:]
    count = len(rows)
    net = sum(safe_float(row.get("net")) for row in rows)
    wins = sum(1 for row in rows if safe_float(row.get("net")) > 0)
    r_values = [safe_float(row.get("r_multiple")) for row in rows if row.get("r_multiple") is not None]
    return {
        "size": size,
        "count": count,
        "net": round(net, 8),
        "win_rate": round(wins / count, 4) if count else 0.0,
        "expectancy": round(net / count, 8) if count else 0.0,
        "avg_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
    }

def pnl_bucket_label(net: float) -> str:
    if net <= -1.0:
        return "<= -1.00"
    if net <= -0.5:
        return "-1.00..-0.50"
    if net <= -0.1:
        return "-0.50..-0.10"
    if net < 0:
        return "-0.10..0"
    if net == 0:
        return "0"
    if net < 0.1:
        return "0..0.10"
    if net < 0.5:
        return "0.10..0.50"
    if net < 1.0:
        return "0.50..1.00"
    return ">= 1.00"

def summarize_paper_report(rows: list[dict], account: dict, _closes_override: list[dict] | None = None, _include_historical: bool = True) -> dict:
    seen: set[tuple[str, str]] = set()
    closes: list[dict] = []
    if _closes_override is not None:
        closes = list(_closes_override)
    else:
        for row in rows:
            if not valid_paper_close(row):
                continue
            trade_id = str(row.get("trade_id") or row.get("paper_trade_id") or row.get("close_id") or "")
            close_ts = str(row.get("close_ts") or row.get("ts") or "")
            key = (trade_id, close_ts)
            if key in seen:
                continue
            seen.add(key)
            r_multiple = trade_r_multiple(row)
            enriched = {**row}
            if r_multiple is not None:
                enriched["r_multiple"] = round(r_multiple, 4)
            closes.append(enriched)
    closes.sort(key=lambda row: parse_ts(row.get("close_ts") or row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))

    all_closes = list(closes)
    reset_ts = parse_ts(account.get("created_at"))
    if reset_ts:
        closes = [
            row
            for row in closes
            if (parse_ts(row.get("close_ts") or row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)) >= reset_ts
        ]

    starting_equity = safe_float(account.get("starting_equity"), 100.0) or 100.0
    equity = starting_equity
    peak = equity
    max_drawdown = 0.0
    win_sum = 0.0
    loss_sum = 0.0
    wins = 0
    losses = 0
    current_streak = 0
    current_streak_side = "flat"
    max_win_streak = 0
    max_loss_streak = 0
    total_fees = 0.0
    total_notional = 0.0
    r_values: list[float] = []
    by_symbol: dict[str, dict] = {}
    by_setup: dict[str, dict] = {}
    by_side: dict[str, dict] = {}
    by_reason: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    pnl_buckets: dict[str, dict] = {}
    curve = [{"index": 0, "ts": account.get("created_at"), "equity": round(equity, 8), "net": 0.0, "label": "start"}]

    for index, row in enumerate(closes, start=1):
        net = safe_float(row.get("net"))
        total_fees += safe_float(row.get("fee") or row.get("fees"))
        total_notional += safe_float(row.get("notional"))
        if row.get("r_multiple") is not None:
            r_values.append(safe_float(row.get("r_multiple")))
        equity += net
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if net > 0:
            wins += 1
            win_sum += net
            current_streak = current_streak + 1 if current_streak_side == "win" else 1
            current_streak_side = "win"
            max_win_streak = max(max_win_streak, current_streak)
        elif net < 0:
            losses += 1
            loss_sum += net
            current_streak = current_streak + 1 if current_streak_side == "loss" else 1
            current_streak_side = "loss"
            max_loss_streak = max(max_loss_streak, current_streak)
        update_trade_bucket(by_symbol, row.get("symbol"), row, net)
        update_trade_bucket(by_setup, row.get("setup_id"), row, net)
        update_trade_bucket(by_side, row.get("side"), row, net)
        update_trade_bucket(by_reason, row.get("reason"), row, net)
        close_dt = parse_ts(row.get("close_ts") or row.get("ts"))
        day_key = close_dt.date().isoformat() if close_dt else "unknown"
        update_trade_bucket(by_day, day_key, row, net)
        hist_key = pnl_bucket_label(net)
        hist = pnl_buckets.setdefault(hist_key, {"bucket": hist_key, "count": 0, "net": 0.0})
        hist["count"] += 1
        hist["net"] += net
        curve.append(
            {
                "index": index,
                "ts": row.get("close_ts") or row.get("ts"),
                "equity": round(equity, 8),
                "net": round(net, 8),
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "reason": row.get("reason"),
                "setup_id": row.get("setup_id"),
            }
        )

    closed = len(closes)
    net_total = equity - starting_equity
    recent = closes[-10:]
    recent_net = sum(safe_float(row.get("net")) for row in recent)
    recent_wins = sum(1 for row in recent if safe_float(row.get("net")) > 0)
    expectancy = net_total / closed if closed else 0.0
    recent_expectancy = recent_net / len(recent) if recent else 0.0
    profit_factor = round(win_sum / abs(loss_sum), 4) if loss_sum < 0 else (999.0 if win_sum > 0 else 0.0)
    equity_values = [safe_float(row.get("equity")) for row in curve]
    equity_high = max(equity_values) if equity_values else starting_equity
    equity_low = min(equity_values) if equity_values else starting_equity
    current_drawdown = max(0.0, equity_high - equity)
    open_summary = summarize_paper_account(account)
    best_trade = max(closes, key=lambda row: safe_float(row.get("net")), default={})
    worst_trade = min(closes, key=lambda row: safe_float(row.get("net")), default={})
    bucket_order = ["<= -1.00", "-1.00..-0.50", "-0.50..-0.10", "-0.10..0", "0", "0..0.10", "0.10..0.50", "0.50..1.00", ">= 1.00"]
    histogram = [{"bucket": key, "count": int(pnl_buckets.get(key, {}).get("count") or 0), "net": round(safe_float(pnl_buckets.get(key, {}).get("net")), 8)} for key in bucket_order]
    progress_state = "chưa đủ mẫu"
    if closed >= 10:
        if recent_expectancy > expectancy and recent_net > 0:
            progress_state = "đang tiến bộ"
        elif recent_expectancy < expectancy and recent_net < 0:
            progress_state = "đang xấu đi"
        else:
            progress_state = "đi ngang"
    historical = None
    if _include_historical and reset_ts and len(all_closes) != len(closes):
        historical = summarize_paper_report(rows, {**account, "created_at": None}, _closes_override=all_closes, _include_historical=False)

    account_closed_trades = int(safe_float(account.get("closed_trades") or account.get("trades")))
    account_realized_pnl = safe_float(account.get("realized_pnl"))
    reset_window_net = net_total
    account_alignment = {
        "account_created_at": account.get("created_at"),
        "account_closed_trades": account_closed_trades,
        "validated_current_closed_trades": closed,
        "historical_closed_trades": len(all_closes),
        "closed_trade_count_delta": account_closed_trades - closed,
        "account_realized_pnl": round(account_realized_pnl, 8),
        "validated_current_net": round(reset_window_net, 8),
        "realized_pnl_delta": round(account_realized_pnl - reset_window_net, 8),
        "is_current_reset_window": bool(reset_ts),
    }

    return {
        "starting_equity": round(starting_equity, 8),
        "current_equity": round(safe_float(account.get("equity"), equity), 8),
        "account_created_at": account.get("created_at"),
        "window": "current_reset" if reset_ts else "all_time",
        "historical": historical,
        "account_alignment": account_alignment,
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / closed, 4) if closed else 0.0,
        "net": round(net_total, 8),
        "return_pct": round(net_total / starting_equity, 4) if starting_equity else 0.0,
        "expectancy": round(expectancy, 8),
        "recent_10_net": round(recent_net, 8),
        "recent_10_win_rate": round(recent_wins / len(recent), 4) if recent else 0.0,
        "recent_10_expectancy": round(recent_expectancy, 8),
        "profit_factor": profit_factor,
        "max_drawdown": round(max_drawdown, 8),
        "max_drawdown_pct": round(max_drawdown / starting_equity, 4) if starting_equity else 0.0,
        "current_drawdown": round(current_drawdown, 8),
        "current_drawdown_pct": round(current_drawdown / equity_high, 4) if equity_high else 0.0,
        "equity_high": round(equity_high, 8),
        "equity_low": round(equity_low, 8),
        "avg_win": round(win_sum / wins, 8) if wins else 0.0,
        "avg_loss": round(loss_sum / losses, 8) if losses else 0.0,
        "payoff_ratio": round((win_sum / wins) / abs(loss_sum / losses), 4) if wins and losses and loss_sum < 0 else 0.0,
        "avg_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
        "total_fees": round(total_fees, 8),
        "fee_drag_pct": round(total_fees / abs(net_total), 4) if net_total else 0.0,
        "avg_notional": round(total_notional / closed, 8) if closed else 0.0,
        "current_streak": current_streak,
        "current_streak_side": current_streak_side,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "open_position_count": open_summary["open_position_count"],
        "open_margin": open_summary["open_margin"],
        "open_notional": open_summary["open_notional"],
        "margin_usage_pct": round(open_summary["open_margin"] / safe_float(account.get("equity"), starting_equity), 4) if safe_float(account.get("equity"), starting_equity) else 0.0,
        "notional_exposure_pct": round(open_summary["open_notional"] / safe_float(account.get("equity"), starting_equity), 4) if safe_float(account.get("equity"), starting_equity) else 0.0,
        "rolling": {"5": rolling_trade_window(closes, 5), "10": rolling_trade_window(closes, 10), "20": rolling_trade_window(closes, 20)},
        "breakdown": {
            "by_symbol": finalize_trade_buckets(by_symbol),
            "by_setup": finalize_trade_buckets(by_setup),
            "by_side": finalize_trade_buckets(by_side),
            "by_reason": finalize_trade_buckets(by_reason),
            "by_day": finalize_trade_buckets(by_day, limit=14),
        },
        "pnl_histogram": histogram,
        "progress_state": progress_state,
        "curve": curve[-160:],
        "recent_closes": closes[-40:],
    }

def read_pid_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None

def process_status() -> dict:
    watchdog_pid = read_pid_file(STATE_DIR / "scalp_watchdog.pid")
    child_pid = read_pid_file(STATE_DIR / "scalp_autotrader.pid")
    return {
        "watchdog_pid": watchdog_pid,
        "watchdog_running": pid_running(watchdog_pid),
        "child_pid": child_pid,
        "child_running": pid_running(child_pid),
        "stop_file_exists": (STATE_DIR / "STOP_SCALP_WATCHDOG").exists(),
    }

def paper_runtime_status(heartbeats: list[dict]) -> dict:
    lookup = {row.get("name"): row for row in heartbeats if isinstance(row, dict)}
    tracked = [
        lookup.get("paper_candidate_feeder"),
        lookup.get("autonomous_paper_trading_loop"),
        lookup.get("paper_execution_lifecycle_loop"),
        lookup.get("counterfactual_replay_agent"),
        lookup.get("promotion_evaluator_loop"),
    ]
    tracked = [row for row in tracked if row]
    running = [row for row in tracked if row.get("running")]
    healthy = [row for row in tracked if row.get("state") == "ok"]
    if not tracked:
        state = "unknown"
    elif len(healthy) == len(tracked):
        state = "running"
    elif running:
        state = "degraded"
    else:
        state = "stopped"
    return {
        "state": state,
        "running": bool(running),
        "healthy_count": len(healthy),
        "tracked_count": len(tracked),
        "tracked": [row.get("name") for row in tracked],
    }

def compact_market_latest(payload: dict) -> dict:
    return {
        "ts": payload.get("ts") or payload.get("updated_at"),
        "executor": payload.get("executor") if isinstance(payload.get("executor"), dict) else {},
        "majors": (payload.get("majors") if isinstance(payload.get("majors"), list) else [])[:12],
        "hot": (payload.get("hot") if isinstance(payload.get("hot"), list) else [])[:16],
        "funding_extremes": (payload.get("funding_extremes") if isinstance(payload.get("funding_extremes"), list) else [])[:12],
    }

def compact_cognitive(payload: dict) -> dict:
    focus = payload.get("focus") if isinstance(payload.get("focus"), dict) else {}
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    belief_summary = payload.get("belief_summary") if isinstance(payload.get("belief_summary"), dict) else {}
    return {
        "ts": payload.get("ts"),
        "focus": focus,
        "decision": decision,
        "contradictions": (payload.get("contradictions") if isinstance(payload.get("contradictions"), list) else [])[:8],
        "hypotheses_to_test": (payload.get("hypotheses_to_test") if isinstance(payload.get("hypotheses_to_test"), list) else [])[:8],
        "belief_count": int(belief_summary.get("belief_count", 0) or 0),
        "top_beliefs": (belief_summary.get("top_beliefs") if isinstance(belief_summary.get("top_beliefs"), list) else [])[:6],
    }

def compact_reasoning(payload: dict) -> dict:
    return {
        "ts": payload.get("ts"),
        "decision": payload.get("decision") if isinstance(payload.get("decision"), dict) else {},
        "focus": payload.get("focus") if isinstance(payload.get("focus"), dict) else {},
        "contradictions": (payload.get("contradictions") if isinstance(payload.get("contradictions"), list) else [])[:8],
        "beliefs_used": (payload.get("beliefs_used") if isinstance(payload.get("beliefs_used"), list) else [])[:8],
    }


def compact_beliefs(ledger: dict) -> dict:
    beliefs = ledger.get("beliefs") if isinstance(ledger.get("beliefs"), dict) else {}
    top = sorted(beliefs.values(), key=lambda row: safe_float(row.get("confidence")), reverse=True)[:8]
    return {
        "count": len(beliefs),
        "top": [
            {
                "id": row.get("belief_id"),
                "statement": row.get("statement"),
                "confidence": safe_float(row.get("confidence")),
                "status": row.get("status"),
                "topic": row.get("topic"),
                "scope": row.get("scope"),
            }
            for row in top
        ],
    }


def compact_setups(library: dict) -> dict:
    skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
    rows = []
    for setup_id, row in sorted(skills.items()):
        stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
        rows.append(
            {
                "setup_id": setup_id,
                "enabled": bool(row.get("enabled", True)),
                "trades": int(stats.get("trades", 0) or 0),
                "win_rate": safe_float(stats.get("win_rate")),
                "expectancy": safe_float(stats.get("expectancy")),
                "net": safe_float(stats.get("net")),
            }
        )
    rows.sort(key=lambda item: (item["trades"], item["expectancy"]), reverse=True)
    return {"count": len(rows), "rows": rows}


def compact_news(news: dict) -> dict:
    return {
        "ts": news.get("ts"),
        "event_count": int(news.get("event_count", 0) or 0),
        "macro_risk_score": safe_float(news.get("macro_risk_score")),
        "crypto_regulatory_risk": safe_float(news.get("crypto_regulatory_risk")),
        "catalyst_score": safe_float(news.get("catalyst_score")),
        "headline_chaos": safe_float(news.get("headline_chaos")),
        "freshness_score": safe_float(news.get("freshness_score")),
        "source_quality_score": safe_float(news.get("source_quality_score")),
        "top_events": (news.get("top_events") if isinstance(news.get("top_events"), list) else [])[:12],
        "source_health": (news.get("source_health") if isinstance(news.get("source_health"), list) else [])[:12],
        "symbol_impacts": news.get("symbol_impacts") if isinstance(news.get("symbol_impacts"), dict) else {},
        "risk_contract": news.get("risk_contract") or "tighten_only",
        "can_place_orders": bool(news.get("can_place_orders")),
        "can_loosen_risk": bool(news.get("can_loosen_risk")),
    }

def compact_shadow_performance(performance: dict) -> dict:
    overall = performance.get("overall") if isinstance(performance.get("overall"), dict) else {}
    data_quality = performance.get("data_quality") if isinstance(performance.get("data_quality"), dict) else {}
    segments = performance.get("segments") if isinstance(performance.get("segments"), dict) else {}
    top_segments = []
    worst_segments = []
    for group, rows in segments.items():
        if not isinstance(rows, list):
            continue
        for row in rows[:8]:
            if isinstance(row, dict):
                item = {"group": group, **row}
                top_segments.append(item)
                worst_segments.append(item)
    top_segments.sort(key=lambda row: (safe_float(row.get("expectancy")), safe_float(row.get("net"))), reverse=True)
    worst_segments.sort(key=lambda row: (safe_float(row.get("expectancy")), safe_float(row.get("net"))))
    closed = int(overall.get("closed", 0) or 0)
    return {
        "updated_at": performance.get("updated_at"),
        "schema_version": int(performance.get("schema_version", 0) or 0),
        "run_id": performance.get("run_id"),
        "assumption_hash": performance.get("assumption_hash") or "none",
        "metric_mode": performance.get("metric_mode") or "closed_only",
        "trades": int(overall.get("trades", 0) or 0),
        "closed": closed,
        "wins": int(overall.get("wins", 0) or 0),
        "losses": int(overall.get("losses", 0) or 0),
        "win_rate": safe_float(overall.get("win_rate")),
        "net": safe_float(overall.get("net")),
        "expectancy": safe_float(overall.get("expectancy")),
        "profit_factor": safe_float(overall.get("profit_factor")),
        "max_drawdown": safe_float(overall.get("max_drawdown")),
        "under_sampled": closed < 50,
        "data_quality": {
            "confidence": data_quality.get("confidence") or overall.get("confidence") or "low",
            "unresolved_count": int(data_quality.get("unresolved_count", overall.get("unresolved_count", 0)) or 0),
            "ambiguous_count": int(data_quality.get("ambiguous_count", overall.get("ambiguous_count", 0)) or 0),
            "skipped_count": int(data_quality.get("skipped_count", overall.get("skipped_count", 0)) or 0),
            "api_error_count": int(data_quality.get("api_error_count", overall.get("api_error_count", 0)) or 0),
            "mixed_assumptions": bool(data_quality.get("mixed_assumptions")),
        },
        "top_segments": top_segments[:12],
        "worst_segments": worst_segments[:12],
        "kill_candidates": (performance.get("kill_candidates") if isinstance(performance.get("kill_candidates"), list) else [])[:12],
        "promotion_candidates": (performance.get("promotion_candidates") if isinstance(performance.get("promotion_candidates"), list) else [])[:12],
    }

def compact_self_improvement(payload: dict) -> dict:
    scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    return {
        "ts": payload.get("ts"),
        "overall_learning_score": safe_float(payload.get("overall_learning_score")),
        "readiness": payload.get("readiness") or "unknown",
        "blindspots": (payload.get("blindspots") if isinstance(payload.get("blindspots"), list) else [])[:10],
        "learning_curriculum": (payload.get("learning_curriculum") if isinstance(payload.get("learning_curriculum"), list) else [])[:10],
        "guardrail_proposal": payload.get("guardrail_proposal") if isinstance(payload.get("guardrail_proposal"), dict) else {},
        "score_snapshot": {key: safe_float(value.get("score") if isinstance(value, dict) else 0) for key, value in scores.items()},
    }

def compact_daily_exam(payload: dict) -> dict:
    rubric = payload.get("rubric") if isinstance(payload.get("rubric"), dict) else {}
    scores = rubric.get("scores") if isinstance(rubric.get("scores"), dict) else {}
    grade = payload.get("grade") if isinstance(payload.get("grade"), dict) else {}
    return {
        "ts": payload.get("ts"),
        "local_date": payload.get("local_date"),
        "exam_id": payload.get("exam_id"),
        "exam_type": payload.get("exam_type") or "unknown",
        "quality_score": safe_float(payload.get("quality_score")),
        "quality_grade": payload.get("quality_grade") or "F",
        "exam_score": safe_float(payload.get("exam_score")),
        "passed": bool(payload.get("passed")),
        "task": payload.get("task") if isinstance(payload.get("task"), dict) else {},
        "answer": payload.get("answer") if isinstance(payload.get("answer"), dict) else {},
        "learning_targets": (payload.get("learning_targets") if isinstance(payload.get("learning_targets"), list) else [])[:8],
        "checks": (grade.get("checks") if isinstance(grade.get("checks"), list) else [])[:8],
        "score_snapshot": {key: safe_float(value.get("score") if isinstance(value, dict) else 0) for key, value in scores.items()},
        "contract": payload.get("contract") if isinstance(payload.get("contract"), dict) else {"paper_only": True},
    }

def compact_llm_reasoning(payload: dict) -> dict:
    reasoning = payload.get("reasoning") if isinstance(payload.get("reasoning"), dict) else {}
    provider = payload.get("provider") if isinstance(payload.get("provider"), dict) else reasoning.get("provider") if isinstance(reasoning.get("provider"), dict) else {}
    risk = reasoning.get("risk_proposal") if isinstance(reasoning.get("risk_proposal"), dict) else {}
    return {
        "ts": payload.get("ts"),
        "status": payload.get("status") or "unknown",
        "provider": provider.get("provider") or "unknown",
        "deep_model": provider.get("deep_model") or "unknown",
        "quick_model": provider.get("quick_model") or "unknown",
        "summary": reasoning.get("summary") or "không có",
        "market_read": reasoning.get("market_read") or "không có",
        "critical_blindspots": (reasoning.get("critical_blindspots") if isinstance(reasoning.get("critical_blindspots"), list) else [])[:8],
        "hypotheses": (reasoning.get("hypotheses") if isinstance(reasoning.get("hypotheses"), list) else [])[:8],
        "experiments": (reasoning.get("paper_shadow_experiments") if isinstance(reasoning.get("paper_shadow_experiments"), list) else [])[:8],
        "curriculum": (reasoning.get("curriculum") if isinstance(reasoning.get("curriculum"), list) else [])[:8],
        "risk_proposal": risk,
        "can_place_live_orders": bool(risk.get("can_place_live_orders")),
        "can_loosen_risk": bool(risk.get("can_loosen_risk")),
        "error": payload.get("error") or reasoning.get("error"),
    }

def compact_promotion(payload: dict) -> dict:
    req = payload.get("requirements") if isinstance(payload.get("requirements"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    failures = payload.get("failures") if isinstance(payload.get("failures"), list) else []
    rows = []
    for key, target in req.items():
        value = metrics.get(key)
        try:
            passed = float(value or 0) >= float(target)
        except Exception:
            passed = bool(value)
        rows.append({"metric": key, "value": value, "target": target, "passed": passed})
    return {"evaluated_at": payload.get("evaluated_at"), "state": payload.get("state") or "paper_learning", "passed": bool(payload.get("passed")), "failures": failures, "rows": rows, "metrics": metrics, "requirements": req, "can_place_live_orders": bool(payload.get("can_place_live_orders"))}

def compact_ops() -> dict:
    return {
        "promotion": compact_promotion(read_json(MEMORY_DIR / "promotion_board_latest.json")),
        "alerts": read_json(STATE_DIR / "alerts_latest.json"),
        "host_runtime": read_json(STATE_DIR / "host_runtime_latest.json"),
        "model_usage": read_json(MEMORY_DIR / "model_usage_latest.json"),
        "skill_forge": read_json(MEMORY_DIR / "skill_forge_latest.json"),
        "skill_patch_integration": read_json(MEMORY_DIR / "skill_patch_integration_latest.json"),
        "dont_do": read_json(MEMORY_DIR / "dont_do_memory.json"),
        "paper_loop": read_json(MEMORY_DIR / "autonomous_paper_trading_loop_latest.json"),
        "paper_execution_lifecycle": read_json(MEMORY_DIR / "paper_execution_lifecycle_latest.json"),
        "microstructure_loop": read_json(MEMORY_DIR / "microstructure_observer_loop_latest.json"),
        "security_import_guard": read_json(MEMORY_DIR / "security_import_guard_latest.json"),
        "queue": queue_summary(),
    }

def load_dashboard_status() -> dict:
    bias = read_json(MEMORY_DIR / "execution_bias.json")
    market_model = read_json(MEMORY_DIR / "market_model.json")
    market_latest = read_json(STATE_DIR / "market_updates_latest.json")
    dream_latest = read_json(MEMORY_DIR / "dream_cycle_latest.json")
    belief_ledger = read_json(MEMORY_DIR / "belief_ledger.json")
    setup_library = read_json(MEMORY_DIR / "setup_skills.json")
    live_readiness = read_json(MEMORY_DIR / "live_readiness_latest.json")
    news_latest = read_json(MEMORY_DIR / "news_latest.json")
    shadow_performance = read_json(MEMORY_DIR / "shadow_performance_latest.json")
    self_improvement = read_json(MEMORY_DIR / "self_improvement_latest.json")
    daily_exam = read_json(MEMORY_DIR / "daily_exam_latest.json")
    llm_reasoning = read_json(MEMORY_DIR / "llm_reasoning_latest.json")
    cognitive_state = read_json(MEMORY_DIR / "cognitive_state_latest.json")
    curiosity_focus = read_json(MEMORY_DIR / "curiosity_focus_latest.json")
    reasoning_trace = read_json(MEMORY_DIR / "reasoning_trace_latest.json")
    paper_account = read_json(STATE_DIR / "paper_account.json")
    scalp_rows = read_jsonl_tail(LOG_FILES["scalp_autotrader"], 500)
    lifecycle_rows = read_jsonl_tail(LOG_FILES.get("paper_lifecycle", MEMORY_DIR / "paper_trades.jsonl"), 500)
    paper_rows = [*scalp_rows, *lifecycle_rows]
    heartbeats = [heartbeat_status(name, path) for name, path in HEARTBEAT_FILES.items()]
    log_health = [
        {"name": name, "path": str(path), "exists": path.exists(), "age_seconds": file_mtime_age(path), "age": human_age(file_mtime_age(path))}
        for name, path in LOG_FILES.items()
    ]
    market_state = market_model.get("last_market_state") if isinstance(market_model.get("last_market_state"), dict) else {}
    dream_cycle = dream_latest.get("cycle") if isinstance(dream_latest.get("cycle"), dict) else {}
    bias_sleep_age = None
    sleep_until = parse_ts(bias.get("sleep_until")) if bias else None
    if sleep_until:
        bias_sleep_age = (sleep_until - datetime.now(timezone.utc)).total_seconds()
    paper_account_summary = summarize_paper_account(paper_account)
    paper_report = summarize_paper_report(paper_rows, paper_account)
    process = process_status()
    process["paper_runtime"] = paper_runtime_status(heartbeats)
    return {
        "now": utc_now(),
        "overview": {
            "risk_posture": bias.get("risk_posture", "unknown"),
            "allow_new_entries": bias.get("allow_new_entries"),
            "min_signal_score": bias.get("min_signal_score"),
            "sleep_until": bias.get("sleep_until"),
            "sleep_remaining_seconds": bias_sleep_age,
            "sleep_remaining": human_age(bias_sleep_age if bias_sleep_age and bias_sleep_age > 0 else 0),
            "regime": (bias.get("market_learning") or {}).get("regime") or market_state.get("primary_regime"),
            "tags": (bias.get("market_learning") or {}).get("tags") or market_state.get("tags", []),
            "hot": market_latest.get("hot", [{}])[0].get("symbol") if isinstance(market_latest.get("hot"), list) and market_latest.get("hot") else None,
            "live_mode": live_readiness.get("mode") or live_readiness.get("status") or "paper",
        },
        "bias": bias,
        "market_state": market_state,
        "dream": {
            "ts": dream_latest.get("ts"),
            "applied_bias": dream_latest.get("applied_bias"),
            "high_risk_count": (dream_latest.get("bias_patch") or {}).get("high_risk_count"),
            "paper_candidates": (dream_latest.get("bias_patch") or {}).get("paper_candidates", []),
            "blocks": dream_cycle.get("blocks", [])[:12],
        },
        "paper": {**summarize_paper(paper_rows), "account": paper_account, "account_summary": paper_account_summary},
        "paper_report": paper_report,
        "process": process,
        "market_latest": compact_market_latest(market_latest),
        "beliefs": compact_beliefs(belief_ledger),
        "setups": compact_setups(setup_library),
        "news": compact_news(news_latest),
        "shadow_performance": compact_shadow_performance(shadow_performance),
        "self_improvement": compact_self_improvement(self_improvement),
        "daily_exam": compact_daily_exam(daily_exam),
        "llm_reasoning": compact_llm_reasoning(llm_reasoning),
        "ops": compact_ops(),
        "cognitive": compact_cognitive(cognitive_state),
        "curiosity": curiosity_focus,
        "reasoning": compact_reasoning(reasoning_trace),
        "phase_b_learning": load_phase_b_learning(),
        "live_readiness": live_readiness,
        "heartbeats": heartbeats,
        "live_monitors": live_monitor_status(),
        "logs": log_health,
    }


HTML = r"""
<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Bảng điều khiển Trading Agent</title>
<style>
:root{
  --bg:#0c100f; --bg2:#101615; --surface:#141a19; --surface2:#19211f; --surface3:#202b28;
  --line:rgba(189,210,203,.13); --line2:rgba(189,210,203,.22); --text:#eef5f2; --muted:#94a39e; --soft:#c9d7d2;
  --accent:#63c7a5; --amber:#d4aa54; --red:#e17369; --green:#78d49a; --blue:#8fbac6;
  --shadow:0 20px 60px rgba(0,0,0,.28); --shadow2:0 8px 22px rgba(0,0,0,.18); --r:8px;
}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;min-height:100dvh;background:linear-gradient(180deg,#0d1110 0%,#0a0d0c 48%,#0f1312 100%);color:var(--text);font:14px/1.5 "Aptos","Segoe UI",system-ui,-apple-system,BlinkMacSystemFont,sans-serif;letter-spacing:0}
body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.32;background-image:linear-gradient(rgba(255,255,255,.028) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.022) 1px,transparent 1px);background-size:48px 48px;mask-image:linear-gradient(180deg,rgba(0,0,0,.9),transparent 78%)}
button{font:inherit}.mono,.metric,.value,.table .num{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;font-variant-numeric:tabular-nums}
.app{min-height:100dvh;display:grid;grid-template-rows:auto 1fr}.topbar{position:sticky;top:0;z-index:20;background:rgba(12,16,15,.86);backdrop-filter:blur(18px);border-bottom:1px solid var(--line)}
.topinner{max-width:1680px;width:100%;min-width:0;margin:0 auto;padding:14px 22px;display:grid;grid-template-columns:minmax(260px,1fr) minmax(0,560px);gap:20px;align-items:center}.brand,.statusline{min-width:0}.brand h1{font-size:18px;font-weight:760;margin:0}.brand p{margin:3px 0 0;color:var(--muted);font-size:12.5px;overflow-wrap:anywhere}.statusline{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}.pill{border:1px solid var(--line2);background:rgba(20,26,25,.82);border-radius:999px;padding:5px 10px;color:var(--soft);font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;white-space:nowrap}.pill.ok{border-color:rgba(120,212,154,.42);color:var(--green);background:rgba(34,60,43,.34)}.pill.warn{border-color:rgba(212,170,84,.52);color:var(--amber);background:rgba(69,54,27,.28)}.pill.bad{border-color:rgba(225,115,105,.54);color:var(--red);background:rgba(71,34,31,.3)}
.layout{max-width:1680px;width:100%;min-width:0;margin:0 auto;padding:16px 22px 28px;background:transparent;display:grid;grid-template-columns:238px minmax(0,1fr);gap:14px;align-items:start}.rail{position:sticky;top:86px;z-index:12;min-width:0;min-height:calc(100dvh - 108px);display:grid;grid-template-rows:auto 1fr auto;background:rgba(17,23,21,.84);border:1px solid var(--line);border-radius:var(--r);padding:10px;box-shadow:var(--shadow2);backdrop-filter:blur(14px)}
.railhead{padding:8px 9px 12px;border-bottom:1px solid var(--line);margin-bottom:8px}.railhead span{display:block;color:var(--muted);font-size:12px}.railhead b{display:block;color:var(--text);font-size:15px;margin-top:2px}.nav{display:grid;grid-template-columns:1fr;align-content:start;gap:6px;min-width:0}.nav button{min-width:0;min-height:42px;border:1px solid transparent;border-radius:7px;background:transparent;color:var(--soft);text-align:left;padding:0 12px;cursor:pointer;transition:background .18s,border-color .18s,color .18s,transform .18s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.nav button:hover{background:rgba(255,255,255,.045);border-color:var(--line)}.nav button.active{background:linear-gradient(90deg,rgba(99,199,165,.2),rgba(99,199,165,.08));border-color:rgba(99,199,165,.36);color:var(--text);box-shadow:inset 3px 0 0 var(--accent)}.nav button:active{transform:translateY(1px)}.railfoot{border-top:1px solid var(--line);padding:10px 9px 4px;color:var(--muted);font-size:12px;min-width:0}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;background:var(--muted);box-shadow:0 0 0 3px rgba(255,255,255,.035)}.dot.ok{background:var(--green)}.dot.warn{background:var(--amber)}.dot.bad{background:var(--red)}
.main{min-width:0}.hero{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;align-items:end;min-width:0;background:linear-gradient(180deg,rgba(25,33,31,.9),rgba(18,24,23,.88));border:1px solid var(--line);border-radius:var(--r);padding:18px 18px 16px;box-shadow:var(--shadow);overflow:hidden}.hero::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(180deg,var(--accent),rgba(99,199,165,.1))}.hero h2{font-size:26px;line-height:1.12;margin:0;font-weight:780;text-wrap:balance;overflow-wrap:anywhere}.hero .sub{max-width:96ch;color:var(--muted);margin-top:7px;font-size:13px;overflow-wrap:anywhere}.actions{display:flex;gap:8px}.btn{border:1px solid var(--line2);background:rgba(25,33,31,.94);color:var(--text);border-radius:7px;padding:9px 12px;cursor:pointer;transition:background .18s,border-color .18s,transform .18s,box-shadow .18s;font-size:13px}.btn:hover{background:var(--surface3);border-color:rgba(99,199,165,.36);box-shadow:0 0 0 3px rgba(99,199,165,.07)}.btn:active{transform:translateY(1px)}.btn:focus-visible,.nav button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.stamp{color:var(--muted);font-size:12px;text-align:right;padding:7px 4px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.alertstrip{display:none}.tape{display:flex;gap:8px;overflow:auto;padding:1px 0 12px;scrollbar-width:thin}.tapeitem{min-width:152px;background:rgba(18,24,23,.84);border:1px solid var(--line);border-radius:var(--r);padding:9px 10px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.tapeitem b{display:block;color:var(--text);font-size:12px}.tapeitem span{display:block;color:var(--muted);font-size:11px}.tapeitem.up{border-color:rgba(120,212,154,.22)}.tapeitem.down{border-color:rgba(225,115,105,.22)}.tapeitem.up span:first-of-type{color:var(--green)}.tapeitem.down span:first-of-type{color:var(--red)}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:0 0 12px}.kpi{min-width:0;background:linear-gradient(180deg,rgba(25,33,31,.95),rgba(18,24,23,.95));border:1px solid var(--line);border-radius:var(--r);padding:13px 12px;min-height:90px;box-shadow:var(--shadow2);overflow:hidden}.label{color:var(--muted);font-size:12px;font-weight:620}.value{font-size:22px;font-weight:780;margin-top:8px;white-space:nowrap;letter-spacing:0;overflow:hidden;text-overflow:ellipsis}.hint{color:var(--muted);font-size:12px;margin-top:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.oktxt{color:var(--green)}.warntxt{color:var(--amber)}.badtxt{color:var(--red)}
.viewgrid{display:grid;grid-template-columns:1.18fr .82fr;gap:12px}.viewwide{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.view.subtabbed{display:block}.subtabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px;padding:6px;background:rgba(16,22,20,.86);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow2)}.subtabs button{min-height:36px;border:1px solid transparent;border-radius:6px;background:transparent;color:var(--soft);padding:0 10px;cursor:pointer;font-size:12.5px;transition:background .18s,border-color .18s,color .18s}.subtabs button:hover{background:rgba(255,255,255,.045);border-color:var(--line2)}.subtabs button.active{background:rgba(99,199,165,.18);border-color:rgba(99,199,165,.42);color:var(--text)}.subtabs button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.subpane{display:none}.subpane.active{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;min-width:0}.subpane .panel.wide{grid-column:1/-1}.panel{background:rgba(18,24,23,.88);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow2);min-width:0;overflow:hidden}.panel.wide{grid-column:1/-1}.panel h3{margin:0;padding:12px 13px;border-bottom:1px solid var(--line);font-size:13px;font-weight:720;color:var(--text);background:rgba(25,33,31,.78)}.body{padding:12px 13px}.kv{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.055);font-size:12.5px}.kv:last-child{border-bottom:0}.kv span{color:var(--muted)}.kv b{font-weight:650;text-align:right;max-width:48ch;overflow:hidden;text-overflow:ellipsis}.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{border:1px solid var(--line2);background:rgba(25,33,31,.88);border-radius:6px;padding:4px 7px;color:#d7e1de;font-size:12px}.chip.warn{border-color:rgba(212,170,84,.46);color:#efce82}.chip.bad{border-color:rgba(225,115,105,.46);color:#efa8a2}.tablewrap{overflow:auto}.table{width:100%;border-collapse:collapse}.table th,.table td{text-align:left;border-bottom:1px solid rgba(255,255,255,.065);padding:8px 10px;font-size:12px;vertical-align:top}.table th{color:var(--muted);font-weight:680;white-space:nowrap;background:rgba(13,18,17,.72);position:sticky;top:0}.table td{color:#d8e1df}.table th.num,.table td.num{text-align:right;white-space:nowrap}.table th.center,.table td.center{text-align:center}.table tr:hover td{background:rgba(99,199,165,.055)}.meter{height:7px;background:#26322f;border-radius:999px;overflow:hidden}.bar{height:100%;background:linear-gradient(90deg,var(--accent),var(--green));width:0}.reportgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.reportcard{border:1px solid var(--line);border-radius:7px;background:rgba(13,18,17,.72);padding:10px 11px;min-height:76px}.reportcard span{display:block;color:var(--muted);font-size:11px}.reportcard b{display:block;margin-top:6px;font-size:18px}.chartbox{height:280px;border:1px solid var(--line);border-radius:7px;background:linear-gradient(180deg,rgba(13,18,17,.72),rgba(9,13,12,.86));padding:8px;overflow:hidden}.chartbox svg{width:100%;height:100%;display:block}.chartline{fill:none;stroke:var(--accent);stroke-width:2.4;vector-effect:non-scaling-stroke}.chartarea{fill:rgba(99,199,165,.1)}.chartaxis{stroke:rgba(189,210,203,.18);stroke-width:1}.chartzero{stroke:rgba(212,170,84,.45);stroke-width:1;stroke-dasharray:5 5}.charttext{fill:var(--muted);font:11px ui-monospace,SFMono-Regular,Consolas,monospace}.tradebars{height:120px;display:flex;align-items:flex-end;gap:4px;border:1px solid var(--line);border-radius:7px;background:rgba(9,13,12,.72);padding:8px;overflow:hidden}.tradebar{flex:1;min-width:4px;border-radius:3px 3px 0 0;background:var(--muted);opacity:.9}.tradebar.win{background:var(--green)}.tradebar.loss{background:var(--red)}.log{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;background:#090d0c;border:1px solid var(--line);border-radius:7px;padding:11px;max-height:560px;overflow:auto;color:#cbd6d3}.timeline{display:grid;gap:7px}.event{border-left:2px solid var(--accent);padding:3px 0 7px 10px;border-bottom:1px solid rgba(255,255,255,.05)}.event b{display:block}.hidden{display:none!important}
.reporthero{display:grid;grid-template-columns:minmax(260px,.95fr) minmax(0,1.35fr);gap:14px;align-items:stretch}.reportheadline{border:1px solid rgba(99,199,165,.24);border-radius:8px;background:linear-gradient(180deg,rgba(21,33,29,.9),rgba(12,17,16,.94));padding:15px;min-width:0}.reportheadline span{display:block;color:var(--muted);font-size:12px}.reportheadline b{display:block;margin-top:6px;font-size:34px;line-height:1;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:0}.reportheadline b.good{color:var(--green)}.reportheadline b.bad{color:var(--red)}.reportheadline b.warn{color:var(--amber)}.reportstatus{display:inline-flex;align-items:center;gap:7px;margin-top:12px;border:1px solid var(--line2);border-radius:999px;padding:6px 9px;color:var(--soft);background:rgba(255,255,255,.035);font-size:12px}.reportstatus.ok{border-color:rgba(120,212,154,.38);color:var(--green)}.reportstatus.warn{border-color:rgba(212,170,84,.48);color:var(--amber)}.reportstatus.bad{border-color:rgba(225,115,105,.5);color:var(--red)}.reportmatrix{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.reportmetric{min-width:0;border:1px solid rgba(189,210,203,.12);border-radius:8px;background:rgba(13,18,17,.72);padding:10px 11px;overflow:hidden}.reportmetric span{display:block;color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.reportmetric b{display:block;margin-top:5px;font-size:18px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.reportmetric small{display:block;margin-top:4px;color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.reportmetric.good b{color:var(--green)}.reportmetric.bad b{color:var(--red)}.reportmetric.warn b{color:var(--amber)}.chartbox{height:340px;padding:10px;background:radial-gradient(circle at 78% 12%,rgba(99,199,165,.12),transparent 34%),linear-gradient(180deg,rgba(12,18,17,.94),rgba(7,10,10,.96));border-color:rgba(189,210,203,.16)}.chartbox.compact{height:190px}.chartline{stroke-width:2.8}.chartarea{fill:url(#equityArea)}.chartgrid{stroke:rgba(189,210,203,.105);stroke-width:1}.chartaxis{stroke:rgba(189,210,203,.2)}.chartzero{stroke:rgba(212,170,84,.62);stroke-dasharray:4 5}.chartdd{fill:rgba(225,115,105,.08)}.chartpoint{fill:var(--bg);stroke:var(--accent);stroke-width:2}.chartpoint.bad{stroke:var(--red)}.chartpoint.warn{stroke:var(--amber)}.charthit{fill:transparent;stroke:transparent;cursor:crosshair;pointer-events:all}.charthit:hover+.chartpoint,.chartpoint:hover{stroke-width:3}.charttext{fill:#9fb0aa;font:11px ui-monospace,SFMono-Regular,Consolas,monospace}.chartlabel{fill:#d9e5e1;font:12px ui-monospace,SFMono-Regular,Consolas,monospace}.reportlegend{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px;color:var(--muted);font-size:11.5px}.legenditem::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;background:var(--accent)}.legenditem.warn::before{background:var(--amber)}.legenditem.bad::before{background:var(--red)}.tradebars{position:relative;height:170px;display:flex;align-items:stretch;gap:3px;padding:12px 9px;border-color:rgba(189,210,203,.16);background:linear-gradient(180deg,rgba(9,13,12,.84),rgba(8,11,11,.96))}.tradebars .zeroline{position:absolute;left:8px;right:8px;top:50%;height:1px;background:rgba(212,170,84,.45)}.tradebarwrap{position:relative;flex:1;min-width:5px;cursor:crosshair}.tradebarwrap:hover .tradebar,.distbar:hover i{filter:brightness(1.18);box-shadow:0 0 0 1px rgba(238,245,242,.28)}.tradebar{position:absolute;left:0;right:0;min-height:3px;border-radius:4px;background:var(--muted);opacity:.95}.tradebar.win{bottom:50%;background:linear-gradient(180deg,#98e7b2,var(--green))}.tradebar.loss{top:50%;background:linear-gradient(180deg,var(--red),#a9433d)}.distbars{height:190px;display:grid;grid-template-columns:repeat(9,minmax(0,1fr));gap:5px;align-items:end;border:1px solid rgba(189,210,203,.16);border-radius:7px;background:rgba(9,13,12,.78);padding:10px}.distbar{display:grid;grid-template-rows:1fr auto;gap:5px;min-width:0;height:100%;align-items:end;cursor:crosshair}.distbar i{display:block;border-radius:4px 4px 0 0;background:linear-gradient(180deg,var(--accent),rgba(99,199,165,.42));min-height:3px}.distbar.loss i{background:linear-gradient(180deg,var(--red),rgba(225,115,105,.36))}.distbar span{font-size:9.5px;color:var(--muted);text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.charttip{position:fixed;z-index:9999;max-width:min(320px,calc(100vw - 24px));pointer-events:none;opacity:0;transform:translate3d(-9999px,-9999px,0);transition:opacity .08s ease;background:rgba(8,12,11,.96);border:1px solid rgba(99,199,165,.45);border-radius:7px;box-shadow:0 12px 34px rgba(0,0,0,.42);padding:8px 9px;color:var(--text);font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-line}.charttip.visible{opacity:1}.minirow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.splitbody{display:grid;grid-template-columns:1fr 1fr;gap:12px;align-items:start}.callout{border-left:3px solid var(--accent);background:rgba(99,199,165,.06);border-radius:7px;padding:10px 11px;color:var(--soft);font-size:12px}.callout.bad{border-left-color:var(--red);background:rgba(225,115,105,.07)}
.charthit-band{fill:transparent;stroke:transparent;cursor:crosshair;pointer-events:all}.charthit-band:hover{fill:rgba(99,199,165,.035)}.chartprobe .chartcursor{opacity:0;stroke:rgba(238,245,242,.62);stroke-width:1;stroke-dasharray:4 4;vector-effect:non-scaling-stroke}.chartprobe:hover .chartcursor,.chartprobe.probe-active .chartcursor{opacity:1}.barcursor{position:absolute;top:0;bottom:0;left:50%;width:1px;background:rgba(238,245,242,.55);opacity:0;transform:translateX(-50%);pointer-events:none}.tradebarwrap:hover .barcursor,.tradebarwrap.probe-active .barcursor{opacity:1}.distbar{position:relative}.distbar::before{content:"";position:absolute;top:0;bottom:20px;left:50%;width:1px;background:rgba(238,245,242,.5);opacity:0;transform:translateX(-50%);pointer-events:none}.distbar:hover::before,.distbar.probe-active::before{opacity:1}.charttime{fill:#9fb0aa;font:10.5px ui-monospace,SFMono-Regular,Consolas,monospace}
@media(max-width:1180px){.layout{display:block}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.reportgrid{grid-template-columns:repeat(2,minmax(0,1fr))}.reporthero,.splitbody{grid-template-columns:1fr}.reportmatrix{grid-template-columns:repeat(2,minmax(0,1fr))}.viewgrid,.viewwide,.subpane.active{grid-template-columns:1fr}.rail{position:relative;top:0;min-height:0;display:grid;grid-template-rows:auto auto;grid-template-columns:1fr;margin-bottom:14px}.railhead{display:none}.nav{grid-template-columns:repeat(3,minmax(0,1fr))}.nav button{text-align:center}.nav button.active{box-shadow:inset 0 0 0 1px rgba(99,199,165,.12)}.railfoot{display:flex;gap:12px;align-items:center;justify-content:space-between}.hero{display:block}.actions{margin-top:12px}.stamp{text-align:left}}
@media(max-width:620px){.topinner{grid-template-columns:minmax(0,1fr);padding:12px 14px}.statusline{display:grid;grid-template-columns:minmax(0,1fr);justify-content:stretch}.pill{min-width:0;overflow:hidden;text-overflow:ellipsis}.layout{padding:12px 14px 22px}.kpis,.alertstrip,.reportgrid,.reportmatrix,.minirow{grid-template-columns:1fr}.nav{grid-template-columns:repeat(2,minmax(0,1fr))}.railfoot{display:block;white-space:normal}.hero{grid-template-columns:minmax(0,1fr)}.hero h2{font-size:23px}.value{font-size:20px}.table th,.table td{font-size:12px}.actions{flex-wrap:wrap}.chartbox{height:260px}.chartbox.compact{height:180px}.reportheadline b{font-size:28px}.distbars{grid-template-columns:repeat(3,minmax(0,1fr));height:auto}.distbar{height:112px}}
</style>
</head>
<body>
<div class="app">
  <header class="topbar"><div class="topinner"><div class="brand"><h1>Bảng điều khiển Trading Agent</h1><p>Theo dõi Paper, Shadow, Risk gate, thị trường, tin tức và learning loop.</p></div><div class="statusline" id="statusline"></div></div></header>
  <div class="layout">
    <aside class="rail"><div class="railhead"><span>Điều hướng</span><b>Khu vực theo dõi</b></div><nav class="nav"><button class="active" data-view="overview">Tổng quan</button><button data-view="report">Báo cáo</button><button data-view="agents">Agent</button><button data-view="market">Thị trường</button><button data-view="news">Tin tức</button><button data-view="learning">Học</button><button data-view="logs">Logs</button></nav><div class="railfoot"><div id="side-health"><span class="dot warn"></span>đang tải</div><div id="side-clock" class="mono" style="margin-top:8px"></div></div></aside>
    <main class="main"><section class="hero"><div><h2>Cockpit Trading Agent</h2><div class="sub">Một màn hình gọn để đọc Paper simulation, Risk gate, market tape, news risk và learning loop.</div></div><div class="actions"><button class="btn" id="refresh">Làm mới</button><button class="btn" id="pause">Tạm dừng</button></div></section><div class="stamp" id="stamp"></div><section class="alertstrip" id="alertstrip"></section><section class="tape" id="market-tape"></section><section class="kpis" id="kpis"></section><section id="view-overview" class="view viewgrid"></section><section id="view-report" class="view viewwide hidden"></section><section id="view-agents" class="view viewwide hidden"></section><section id="view-market" class="view viewwide hidden"></section><section id="view-news" class="view viewwide hidden"></section><section id="view-learning" class="view viewwide hidden"></section><section id="view-logs" class="view hidden"></section></main>
  </div>
</div>
<script>
let DATA=null, paused=false;
const esc=v=>String(v??'').replace(/[&<>]/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[s]));
const attr=v=>esc(v).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const num=v=>Number(v||0);
const pct=v=>((num(v)*100).toFixed(1)+'%');
const money=v=>(num(v)>=0?'+':'')+num(v).toFixed(4);
const moneyRaw=v=>v===null||v===undefined||v===''?'không có':String(v);
const fixed=(v,d=2)=>{const n=Number(v);return Number.isFinite(n)?n.toFixed(d):'không có'};
const compactId=v=>String(v??'không có').replaceAll('_',' ');
const tsAgeText=v=>{if(!v)return'không có';const t=new Date(v);if(Number.isNaN(t.getTime()))return String(v);const s=Math.max(0,Math.round((Date.now()-t.getTime())/1000));if(s<60)return `${s}s`;const m=Math.floor(s/60);if(m<60)return `${m}m`;const h=Math.floor(m/60);if(h<24)return `${h}g`;const d=Math.floor(h/24);return `${d}n`};
const shortTs=v=>{if(!v)return'không có';const t=new Date(v);if(Number.isNaN(t.getTime()))return String(v).slice(0,16);return t.toLocaleString('vi-VN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})};
function paperOpenPositions(d){const pa=(d.paper||{}).account||{};return Array.isArray(pa.open_positions)?pa.open_positions:[]}
function paperOpenPositionSummary(d){const rows=paperOpenPositions(d);const count=rows.length;const margin=rows.reduce((sum,row)=>sum+num(row.margin),0);const notional=rows.reduce((sum,row)=>sum+num(row.notional),0);return {count,margin,notional,rows}}
function paperRuntimeLabel(proc){return (proc?.paper_runtime||{}).state||'unknown'}
function markByPosition(d){const map=new Map();(((d.ops||{}).paper_execution_lifecycle||{}).monitor_results||[]).forEach(r=>{if(r&&r.position_id)map.set(r.position_id,{mark:num(r.mark),symbol:r.symbol});if(r&&r.symbol&&!r.position_id&&r.mark!==undefined)map.set(r.symbol,{mark:num(r.mark),symbol:r.symbol})});return map}
function compactNumber(v){const n=Number(v||0);if(!Number.isFinite(n)) return '0';const a=Math.abs(n);if(a>=1e9) return (n/1e9).toFixed(1)+'B';if(a>=1e6) return (n/1e6).toFixed(1)+'M';if(a>=1e3) return (n/1e3).toFixed(1)+'K';return String(Math.round(n))}
const vi={none:'không có','n/a':'không có',paper:'Paper',live:'Live',unknown:'không rõ',ok:'OK',stale:'quá cũ',missing:'thiếu dữ liệu',dead:'không chạy',duplicate:'bị trùng',warn:'cảnh báo',true:'có',false:'không',low:'thấp',medium:'vừa',high:'cao',defensive:'phòng thủ',neutral:'trung lập',aggressive:'chủ động',running:'đang chạy',degraded:'có cảnh báo',stopped:'đã dừng',tighten_only:'chỉ siết risk',risk_on:'risk-on',risk_off:'risk-off',promote:'promote',kill:'loại bỏ',sleep_observe_and_shadow:'ngủ, quan sát, ghi shadow'};
function txt(v){if(v===null||v===undefined||v==='') return 'không có'; const s=String(v); return vi[s.toLowerCase()]||s}
function stateClass(v){return v===true||v==='ok'||v==='running'?'ok':(v===false||v==='dead'||v==='missing'||v==='stale'?'bad':'warn')}
function pill(label,value){return `<span class="pill ${stateClass(value)}">${esc(label)}: ${esc(txt(value))}</span>`}
function chipList(items, cls=''){return (items||[]).map(x=>`<span class="chip ${cls}">${esc(txt(x))}</span>`).join('')||'<span class="hint">không có</span>'}
function panel(title, body, cls=''){return `<article class="panel ${cls}"><h3>${esc(title)}</h3><div class="body">${body}</div></article>`}
function kv(obj){return Object.entries(obj).map(([k,v])=>`<div class="kv"><span>${esc(k)}</span><b>${esc(txt(v))}</b></div>`).join('')}
const numericHeaders=new Set(['PID','Tuổi dữ liệu','Cập nhật cách đây','Số tin','Giá','24h','Vol quote','Hot','Funding','Risk','Bull','Bear','Ưu tiên','Confidence','Score','Lệnh','WR','Expectancy','Đã đóng','Entry','Mark','Margin','Notional','Lev','SL','TP','PnL tạm','Tuổi','Net','PF','Avg notional','Fee','R','Avg R','Win','Loss','Count']);
function table(headers, rows){const head=headers.map(h=>{const label=typeof h==='string'?h:h.label;const cls=typeof h==='string'?(numericHeaders.has(h)?'num':''):(h.cls||'');return `<th class="${esc(cls)}">${esc(label)}</th>`}).join('');return `<div class="tablewrap"><table class="table"><thead><tr>${head}</tr></thead><tbody>${rows.length?rows.join(''):`<tr><td colspan="${headers.length}">Chưa có dữ liệu</td></tr>`}</tbody></table></div>`}
function td(v, cls=''){return `<td class="${cls}">${esc(txt(v))}</td>`}
const SUBTAB_STATE={};
function mountSubtabs(viewId, groups){const view=document.getElementById(viewId);if(!view)return;const panels=Array.from(view.children).filter(el=>el.classList&&el.classList.contains('panel'));if(!panels.length)return;view.classList.add('subtabbed');const tabs=document.createElement('div');tabs.className='subtabs';tabs.setAttribute('role','tablist');const panes=[];let index=0;groups.forEach((g,i)=>{const pane=document.createElement('section');pane.className='subpane';pane.dataset.subpane=g.id;const count=g.count==='rest'?panels.length-index:Number(g.count||0);for(let n=0;n<count&&index<panels.length;n++,index++)pane.appendChild(panels[index]);panes.push(pane);const btn=document.createElement('button');btn.type='button';btn.textContent=g.label;btn.dataset.subtab=g.id;btn.setAttribute('role','tab');btn.onclick=()=>activateSubtab(viewId,g.id);tabs.appendChild(btn)});if(index<panels.length&&panes.length){while(index<panels.length)panes[panes.length-1].appendChild(panels[index++])}view.prepend(tabs);panes.forEach(p=>view.appendChild(p));const params=new URLSearchParams(location.search);const queryKey=viewId.replace(/^view-/,'')+'Tab';const queryWanted=params.get(queryKey)||params.get('subtab');const wanted=queryWanted&&panes.some(p=>p.dataset.subpane===queryWanted)?queryWanted:SUBTAB_STATE[viewId]&&panes.some(p=>p.dataset.subpane===SUBTAB_STATE[viewId])?SUBTAB_STATE[viewId]:(groups[0]||{}).id;activateSubtab(viewId,wanted)}
function activateSubtab(viewId,id){SUBTAB_STATE[viewId]=id;const view=document.getElementById(viewId);if(!view)return;view.querySelectorAll('.subtabs button').forEach(btn=>{const active=btn.dataset.subtab===id;btn.classList.toggle('active',active);btn.setAttribute('aria-selected',active?'true':'false')});view.querySelectorAll('.subpane').forEach(pane=>pane.classList.toggle('active',pane.dataset.subpane===id))}
function latestSignal(row){const s=row?.signal||{}; return s.symbol?`${s.symbol} ${s.side||''} score ${s.score||''}`:'không có'}
async function fetchJson(url){const r=await fetch(url,{cache:'no-store'}); if(!r.ok) throw new Error(url+' '+r.status); return r.json()}
async function fetchText(url){const r=await fetch(url,{cache:'no-store'}); if(!r.ok) throw new Error(url+' '+r.status); return r.text()}
async function load(){if(paused)return;try{DATA=await fetchJson('/api/status');render(DATA);document.getElementById('stamp').textContent='cập nhật '+new Date().toLocaleTimeString()}catch(e){document.getElementById('stamp').textContent='lỗi '+e}}
function render(d){const o=d.overview||{},p=d.paper||{},pa=p.account||{},ps=paperOpenPositionSummary(d),proc=d.process||{},hb=d.heartbeats||[],n=d.news||{},c=d.cognitive||{},cur=d.curiosity||{},s=d.shadow_performance||{};const bad=hb.filter(x=>x.state!=='ok').length;document.getElementById('side-health').innerHTML=`<span class="dot ${bad?'bad':'ok'}"></span>${bad?bad+' agent cần chú ý':'Core agents OK'}`;document.getElementById('side-clock').textContent=d.now||'';document.getElementById('statusline').innerHTML=[pill('watchdog',proc.watchdog_running?'ok':'dead'),pill('paper',paperRuntimeLabel(proc)),pill('mode',o.live_mode||'paper'),pill('risk',o.risk_posture||'unknown'),pill('tin',`${n.event_count||0} sự kiện`)].join('');renderAlertStrip(d);renderMarketTape(d);document.getElementById('kpis').innerHTML=[['Vốn',fixed(pa.equity,2),'vốn đầu '+fixed(pa.starting_equity||100,2)],['Lãi/lỗ paper',money(p.net),`${p.closes||0} lệnh đóng, ${ps.count} lệnh đang mở`],['Cổng rủi ro',o.min_signal_score||'none','ngủ '+(o.sleep_remaining||'không có')],['Tỉ lệ shadow',pct(s.win_rate),`${s.closed||0} lệnh, kỳ vọng ${money(s.expectancy)}`]].map(([a,b,c])=>`<div class="kpi"><div class="label">${esc(a)}</div><div class="value metric">${esc(b)}</div><div class="hint">${esc(c)}</div></div>`).join('');renderOverview(d);renderReport(d);renderAgents(d);renderMarket(d);renderNews(d);renderLearning(d);renderLogsShell()}
function renderAlertStrip(d){const p=d.paper||{},proc=d.process||{},cur=d.curiosity||{},c=d.cognitive||{},r=d.reasoning||{},latest=p.latest_close||{},b=d.bias||{};const cells=[['Hệ thống',(proc.paper_runtime||{}).running?'Paper loop online':'Paper loop offline',(proc.paper_runtime||{}).running?'good':'danger'],['Paper gần nhất',latest.symbol?`${latest.symbol} ${latest.side} ${money(latest.net)}`:'chưa có close hợp lệ',''],['Focus hiện tại',cur.focus_id?`${cur.focus_type} / ${cur.focus_id}`:'chưa có focus',''],['Quyết định',(c.decision||{}).mode||(r.decision||{}).mode||`min score ${b.min_signal_score||'NA'}`,'']];document.getElementById('alertstrip').innerHTML=cells.map(([k,v,cls])=>`<div class="alertcell ${cls}"><strong>${esc(k)}</strong><span>${esc(txt(v))}</span></div>`).join('')}
function renderMarketTape(d){const hot=((d.market_latest||{}).hot||[]).slice(0,6);document.getElementById('market-tape').innerHTML=hot.map(x=>{const change=num(x.change_pct??x.change_24h_pct);const cls=change>=0?'up':'down';return `<div class="tapeitem ${cls}"><b>${esc(x.symbol||'')}</b><span>${esc(fixed(x.price,4)+' / '+change.toFixed(2)+'%')}</span><span>Vol ${esc(compactNumber(x.quote_volume||x.quote_volume_m))}</span></div>`}).join('')||'<div class="tapeitem"><b>Chưa có market tape</b><span>market observer chưa có hot list</span></div>'}
function renderOpenPaperPositions(d){const rows=paperOpenPositionSummary(d).rows;const marks=markByPosition(d);const renderRow=row=>{const markEntry=marks.get(row.position_id)||marks.get(row.symbol)||{};const mark=Number.isFinite(markEntry.mark)?markEntry.mark:null;const entry=num(row.entry);const qty=num(row.qty);const pnl=Number.isFinite(mark)?(row.side==='SHORT'?(entry-mark)*qty:(mark-entry)*qty):null;return `<tr>${td(row.symbol)}${td(row.side)}${td(fixed(row.entry,6),'num')}${td(Number.isFinite(mark)?fixed(mark,6):'','num')}${td(fixed(row.margin,4),'num')}${td(fixed(row.notional,4),'num')}${td(row.leverage,'num')}${td(row.sl,'num')}${td(row.tp,'num')}${td(Number.isFinite(pnl)?money(pnl):'','num')}${td(tsAgeText(row.opened_at),'num')}</tr>`};return panel('Vị thế paper đang mở',table(['Coin','Chiều','Entry','Mark','Margin','Notional','Lev','SL','TP','PnL tạm','Tuổi'],rows.length?rows.map(renderRow):[]),'wide')}
function renderOverview(d){const o=d.overview||{},b=d.bias||{},p=d.paper||{},pa=p.account||{},ps=paperOpenPositionSummary(d),proc=d.process||{},lr=d.live_readiness||{},latestClose=p.latest_close||{},s=d.shadow_performance||{},q=s.data_quality||{},c=d.cognitive||{},r=d.reasoning||{},cur=d.curiosity||{};const recent=(p.latest_events||[]).slice(-4).reverse();const decision=(c.decision||{}).mode||(r.decision||{}).mode||'không có';const paperState=(proc.paper_runtime||{}).state||'unknown';document.getElementById('view-overview').innerHTML=panel('Tóm tắt hệ thống',kv({'Bot mô phỏng':paperState==='running'?'đang chạy':paperState==='degraded'?'đang chạy, có cảnh báo':'đã dừng','Watchdog':proc.watchdog_running?'đang chạy':'đã dừng','Chế độ':o.live_mode||'paper','Sẵn sàng live':lr.status||lr.mode||'không có'}))+panel('Rủi ro hiện tại',kv({'Tư thế':b.risk_posture||'không rõ','Điểm vào tối thiểu':b.min_signal_score||'không có','Ngủ còn':o.sleep_remaining||'không có','Chặn chiều':(b.blocked_sides||[]).join(', ')||'không có'}))+panel('Mô phỏng paper',kv({'Vốn':fixed(pa.equity,2),'Lệnh đang mở':ps.count,'Margin đang dùng':fixed(ps.margin,4),'Notional đang mở':fixed(ps.notional,4),'Lệnh đã đóng':p.closes||0,'Lãi/lỗ':money(p.net),'Lệnh gần nhất':latestClose.symbol?`${latestClose.symbol} ${latestClose.side} ${money(latestClose.net)}`:'không có'}))+renderOpenPaperPositions(d)+panel('Học & shadow',kv({'Trọng tâm':compactId(cur.focus_id||'không có'),'Quyết định':decision,'Shadow WR':pct(s.win_rate),'Kỳ vọng (Expectancy)':money(s.expectancy),'Độ tin cậy dữ liệu':q.confidence||'thấp'}))+panel('Sự kiện mới',`<div class="timeline">${recent.length?recent.map(e=>`<div class="event"><b>${esc(txt(e.event||'event'))} <span class="hint mono">${esc(e.ts||'')}</span></b><span class="hint">${esc(e.symbol||e.reason||e.block_reason||latestSignal(e)||'không có')}</span></div>`).join(''):'<span class="hint">chưa có sự kiện mới</span>'}</div>`,'wide')+`<!-- compatibility anchors: External Live Monitors | Self Improvement | Shadow Performance | Shadow / would-trade only | Starting equity -->`}
function reportTone(v){const n=num(v);return n>0?'good':n<0?'bad':'warn'}
function reportMetric(label,value,hint='',tone=''){return `<div class="reportmetric ${esc(tone)}"><span>${esc(label)}</span><b>${esc(txt(value))}</b><small>${esc(txt(hint))}</small></div>`}
function reportStatus(report){const pf=num(report.profit_factor),wr=num(report.win_rate),net=num(report.net);if((report.closed_trades||0)<20)return ['warn','đang gom mẫu'];if(net>0&&pf>=1.2&&wr>=.5)return ['ok','edge đang dương'];if(net<0||pf<1)return ['bad','edge đang âm'];return ['warn','cần thêm xác nhận']}
function equityChart(report){
const curve=(report.curve||[]).slice(-120);if(curve.length<2)return '<div class="chartbox"><span class="hint">Chưa đủ lệnh đóng để vẽ equity curve</span></div>';
const w=900,h=320,padL=50,padR=18,padT=22,padB=38;const start=num(report.starting_equity||100);const values=curve.map(x=>num(x.equity)).concat([start]);let min=Math.min(...values),max=Math.max(...values);const span=Math.max(.01,max-min);min-=span*.12;max+=span*.12;
const plotW=w-padL-padR,plotH=h-padT-padB;const x=i=>padL+(i/(curve.length-1))*plotW;const y=v=>padT+(max-v)/(max-min)*plotH;const pts=curve.map((row,i)=>[x(i),y(num(row.equity)),row]);
const path='M'+pts.map(p=>`${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' L');const area=`${path} L${x(curve.length-1).toFixed(1)},${h-padB} L${padL},${h-padB} Z`;
const grid=Array.from({length:5},(_,i)=>{const gy=padT+i*plotH/4;const val=max-i*(max-min)/4;return `<line class="chartgrid" x1="${padL}" y1="${gy.toFixed(1)}" x2="${w-padR}" y2="${gy.toFixed(1)}"/><text class="charttext" x="8" y="${(gy+4).toFixed(1)}">${esc(fixed(val,2))}</text>`}).join('');
const startY=y(start);const last=pts[pts.length-1];const low=pts.reduce((a,p)=>num(p[2].equity)<num(a[2].equity)?p:a,pts[0]);const high=pts.reduce((a,p)=>num(p[2].equity)>num(a[2].equity)?p:a,pts[0]);
const tipFor=(row,i)=>`Lệnh equity #${row.index??i} / ${curve.length-1}\nThời gian: ${row.ts||'không có'}\nCoin: ${row.symbol||'start'} ${row.side||''}\nNet lệnh: ${money(row.net)}\nEquity: ${fixed(row.equity,4)}\nReason: ${row.reason||'không có'}\nSetup: ${compactId(row.setup_id||'không có')}`;
const hits=pts.map((p,i)=>`<circle class="charthit" cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="9" data-tip="${attr(tipFor(p[2],i))}"/>`).join('');
const bands=pts.map((p,i)=>{const left=i===0?padL:(pts[i-1][0]+p[0])/2;const right=i===pts.length-1?w-padR:(p[0]+pts[i+1][0])/2;return `<g class="chartprobe"><line class="chartcursor" x1="${p[0].toFixed(1)}" y1="${padT}" x2="${p[0].toFixed(1)}" y2="${h-padB}"/><rect class="charthit-band" x="${left.toFixed(1)}" y="${padT}" width="${Math.max(1,right-left).toFixed(1)}" height="${plotH}" data-tip="${attr(tipFor(p[2],i))}"/></g>`}).join('');
const first=pts[0][2],lastRow=last[2];
return `<div class="chartbox equitychart"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Equity curve paper trading"><defs><linearGradient id="equityArea" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="rgba(99,199,165,.28)"/><stop offset="1" stop-color="rgba(99,199,165,0)"/></linearGradient></defs><rect class="chartdd" x="${padL}" y="${startY.toFixed(1)}" width="${plotW}" height="${Math.max(0,h-padB-startY).toFixed(1)}"/>${grid}<line class="chartaxis" x1="${padL}" y1="${h-padB}" x2="${w-padR}" y2="${h-padB}"/><line class="chartaxis" x1="${padL}" y1="${padT}" x2="${padL}" y2="${h-padB}"/><line class="chartzero" x1="${padL}" y1="${startY.toFixed(1)}" x2="${w-padR}" y2="${startY.toFixed(1)}"/><path class="chartarea" d="${area}"/><path class="chartline" d="${path}"/>${hits}<circle class="chartpoint warn" cx="${high[0].toFixed(1)}" cy="${high[1].toFixed(1)}" r="4" data-tip="${attr('Đỉnh equity\nEquity: '+fixed(high[2].equity,4)+'\nThời gian: '+(high[2].ts||'không có'))}"/><circle class="chartpoint bad" cx="${low[0].toFixed(1)}" cy="${low[1].toFixed(1)}" r="4" data-tip="${attr('Đáy equity\nEquity: '+fixed(low[2].equity,4)+'\nThời gian: '+(low[2].ts||'không có'))}"/><circle class="chartpoint" cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="4.5" data-tip="${attr('Equity hiện tại\nEquity: '+fixed(last[2].equity,4)+'\nNet: '+money(report.net))}"/>${bands}<text class="chartlabel" x="${Math.max(padL,last[0]-92).toFixed(1)}" y="${Math.max(14,last[1]-10).toFixed(1)}">hiện tại ${esc(fixed(last[2].equity,2))}</text><text class="charttext" x="${w-190}" y="16">${esc(curve.length-1)} lệnh gần nhất</text><text class="charttext" x="${padL}" y="${h-10}">baseline ${esc(fixed(start,2))}</text><text class="charttime" x="${padL}" y="${h-24}">từ ${esc(shortTs(first.ts))}</text><text class="charttime" text-anchor="end" x="${w-padR}" y="${h-24}">đến ${esc(shortTs(lastRow.ts))}</text></svg></div><div class="reportlegend"><span class="legenditem">Equity</span><span class="legenditem warn">Đỉnh</span><span class="legenditem bad">Đáy / vùng dưới vốn đầu</span></div>`}
function tradeBars(report){const rows=(report.recent_closes||[]).slice(-42);if(!rows.length)return '<div class="tradebars"><span class="hint">Chưa có lệnh đóng</span></div>';const maxAbs=Math.max(...rows.map(r=>Math.abs(num(r.net))),0.0001);return `<div class="tradebars" aria-label="Biểu đồ cột từng lệnh paper đã đóng"><div class="zeroline"></div>${rows.map((r,i)=>{const net=num(r.net);const h=Math.max(4,Math.min(48,Math.abs(net)/maxAbs*48));const closed=r.close_ts||r.ts||'không có';const label=net>=0?'Lệnh lời':'Lệnh lỗ';const tip=`${label} #${i+1} / ${rows.length}\nThời gian đóng: ${closed}\nCoin: ${r.symbol||'không có'} ${r.side||''}\nNet: ${money(net)}\nR: ${r.r_multiple===undefined?'không có':fixed(r.r_multiple,2)}\nReason: ${r.reason||'không có'}\nSetup: ${compactId(r.setup_id||'unknown')}`;return `<div class="tradebarwrap" data-tip="${attr(tip)}" aria-label="${attr(label+' '+(i+1)+' '+money(net))}"><span class="barcursor"></span><div class="tradebar ${net>=0?'win':'loss'}" style="height:${h}%"></div></div>`}).join('')}</div>`}
function distributionChart(report){const rows=report.pnl_histogram||[];const maxCount=Math.max(...rows.map(r=>num(r.count)),1);return `<div class="distbars" aria-label="Phân phối PnL theo bucket">${rows.map(r=>{const count=num(r.count),bucket=String(r.bucket||'không có'),loss=bucket.startsWith('-')||bucket.startsWith('<='),net=num(r.net);const h=Math.max(3,count/maxCount*100);const kind=net>0?'nhóm lời':net<0?'nhóm lỗ':'nhóm hòa vốn';const tip=`Bucket PnL: ${bucket}\nLoại: ${kind}\nSố lệnh: ${count}\nTổng net: ${money(net)}\nTỉ trọng: ${pct(count/Math.max(1,rows.reduce((a,x)=>a+num(x.count),0)))}`;return `<div class="distbar ${loss?'loss':''}" data-tip="${attr(tip)}" aria-label="${attr('Bucket '+bucket+' '+count+' lệnh')}"><i style="height:${h}%"></i><span>${esc(bucket)}</span></div>`}).join('')}</div>`}
function dailyNetChart(report){const rows=(((report.breakdown||{}).by_day)||[]).slice(-14);if(!rows.length)return '<div class="tradebars"><span class="hint">Chưa có daily net</span></div>';const maxAbs=Math.max(...rows.map(r=>Math.abs(num(r.net))),0.0001);return `<div class="tradebars compact" aria-label="Biểu đồ net theo ngày"><div class="zeroline"></div>${rows.map((r,i)=>{const net=num(r.net);const h=Math.max(4,Math.min(48,Math.abs(net)/maxAbs*48));const label=net>=0?'Ngày lời':'Ngày lỗ';const tip=`${label} #${i+1} / ${rows.length}\nNgày: ${r.key}\nNet: ${money(net)}\nLệnh: ${r.trades}\nWR: ${pct(r.win_rate)}\nExpectancy: ${money(r.expectancy)}`;return `<div class="tradebarwrap" data-tip="${attr(tip)}" aria-label="${attr(label+' '+r.key+' '+money(net))}"><span class="barcursor"></span><div class="tradebar ${net>=0?'win':'loss'}" style="height:${h}%"></div></div>`}).join('')}</div>`}
function ensureChartTooltip(){let tip=document.getElementById('charttip');if(!tip){tip=document.createElement('div');tip.id='charttip';tip.className='charttip';document.body.appendChild(tip)}if(document.body.dataset.chartTipBound==='1')return;document.body.dataset.chartTipBound='1';let active=null;const clearActive=()=>{if(active){active.classList.remove('probe-active');active=null}};const markActive=(el)=>{let probe=el?.classList?.contains('charthit-band')?el.closest('.chartprobe'):el;if(!probe||!(probe.classList.contains('tradebarwrap')||probe.classList.contains('distbar')||probe.classList.contains('chartprobe')))probe=null;if(active!==probe){clearActive();active=probe;if(active)active.classList.add('probe-active')}};const nearestByX=(items,x)=>{let best=null,dist=Infinity;items.forEach(el=>{const r=el.getBoundingClientRect();const cx=r.left+r.width/2;const d=Math.abs(cx-x);if(d<dist){best=el;dist=d}});return best};const tooltipTargetAtCursor=(e)=>{const direct=e.target.closest&&e.target.closest('[data-tip]');if(direct)return direct;const equity=e.target.closest&&e.target.closest('.equitychart');if(equity)return nearestByX([...equity.querySelectorAll('.charthit-band[data-tip],.charthit[data-tip],.chartpoint[data-tip]')],e.clientX);const bars=e.target.closest&&e.target.closest('.tradebars');if(bars)return nearestByX([...bars.querySelectorAll('.tradebarwrap[data-tip]')],e.clientX);const dist=e.target.closest&&e.target.closest('.distbars');if(dist)return nearestByX([...dist.querySelectorAll('.distbar[data-tip]')],e.clientX);return null};const move=(e)=>{const target=tooltipTargetAtCursor(e);if(!target){clearActive();tip.classList.remove('visible');return}markActive(target);tip.textContent=target.dataset.tip||'';tip.classList.add('visible');const pad=14,rect=tip.getBoundingClientRect();let left=e.clientX+16,top=e.clientY+16;if(left+rect.width+pad>window.innerWidth)left=e.clientX-rect.width-16;if(top+rect.height+pad>window.innerHeight)top=e.clientY-rect.height-16;tip.style.transform=`translate3d(${Math.max(pad,left)}px,${Math.max(pad,top)}px,0)`};document.addEventListener('mousemove',move);document.addEventListener('scroll',()=>{clearActive();tip.classList.remove('visible')},true);document.addEventListener('mouseleave',()=>{clearActive();tip.classList.remove('visible')})}
function breakdownTable(rows){return table(['Key','Lệnh','WR','Net','Expectancy','PF','Avg notional'],(rows||[]).map(r=>`<tr>${td(compactId(r.key))}${td(r.trades,'num')}${td(pct(r.win_rate),'num')}${td(money(r.net),'num')}${td(money(r.expectancy),'num')}${td(fixed(r.profit_factor,2),'num')}${td(fixed(r.avg_notional,2),'num')}</tr>`))}
function renderReport(d){const report=d.paper_report||{},paper=d.paper||{},pa=paper.account||{},ps=paperOpenPositionSummary(d),bd=report.breakdown||{},rolling=report.rolling||{};const closes=(report.recent_closes||[]).slice(-18).reverse();const [statusCls,statusText]=reportStatus(report);const headline=`<div class="reporthero"><div class="reportheadline"><span>Net paper / vốn mô phỏng</span><b class="${reportTone(report.net)}">${esc(money(report.net))}</b><div class="reportstatus ${statusCls}"><span class="dot ${statusCls}"></span>${esc(statusText)} · ${esc(report.progress_state||'chưa đủ mẫu')}</div><div class="hint">Equity ${esc(fixed(report.current_equity??pa.equity,2))} / vốn đầu ${esc(fixed(report.starting_equity??pa.starting_equity??100,2))}</div></div><div class="reportmatrix">${[
reportMetric('Lệnh đóng',report.closed_trades||0,`${report.wins||0} win / ${report.losses||0} loss`),
reportMetric('Winrate',pct(report.win_rate),`rolling 10 ${pct(report.recent_10_win_rate)}`),
reportMetric('Profit factor',fixed(report.profit_factor,2),`payoff ${fixed(report.payoff_ratio,2)}`),
reportMetric('Expectancy',money(report.expectancy),`10 lệnh ${money(report.recent_10_expectancy)}`,reportTone(report.expectancy)),
reportMetric('Max DD',money(-(report.max_drawdown||0)),pct(report.max_drawdown_pct),'bad'),
reportMetric('Exposure mở',fixed(report.open_notional??ps.notional,2),`margin ${fixed(report.open_margin??ps.margin,2)}`),
reportMetric('Avg R',report.avg_r===null||report.avg_r===undefined?'không đủ dữ liệu':fixed(report.avg_r,2),`streak ${txt(report.current_streak_side||'flat')} ${report.current_streak||0}`),
reportMetric('Fees',money(-(report.total_fees||0)),`fee drag ${pct(report.fee_drag_pct)}`,'warn'),
reportMetric('Return',pct(report.return_pct),`equity high ${fixed(report.equity_high,2)}`,reportTone(report.return_pct))
].join('')}</div></div>`;const rollingPanel=`<div class="minirow">${[5,10,20].map(n=>{const r=rolling[String(n)]||{};return reportMetric(`${n} lệnh gần`,money(r.net),`WR ${pct(r.win_rate)} · exp ${money(r.expectancy)}`,reportTone(r.net))}).join('')}</div>`;const best=report.best_trade||{},worst=report.worst_trade||{};document.getElementById('view-report').innerHTML=panel('Báo cáo trader mô phỏng',headline,'wide')+panel('Rolling performance',rollingPanel)+panel('Risk & exposure',kv({'Vốn hiện tại':fixed(report.current_equity??pa.equity,2),'Notional đang mở':fixed(report.open_notional??ps.notional,2),'Margin đang mở':fixed(report.open_margin??ps.margin,2),'Margin usage':pct(report.margin_usage_pct),'Notional / equity':pct(report.notional_exposure_pct),'Current drawdown':`${money(-(report.current_drawdown||0))} · ${pct(report.current_drawdown_pct)}`,'Avg notional/lệnh':fixed(report.avg_notional,2),'Max win/loss streak':`${report.max_win_streak||0} / ${report.max_loss_streak||0}`}))+panel('Equity curve paper',equityChart(report),'wide')+panel('PnL từng lệnh gần đây',tradeBars(report))+panel('Phân phối PnL',distributionChart(report))+panel('Daily net',dailyNetChart(report))+panel('Phân rã theo coin và setup',`<div class="splitbody"><div>${breakdownTable(bd.by_symbol||[])}</div><div>${breakdownTable(bd.by_setup||[])}</div></div>`,'wide')+panel('Phân rã theo chiều và lý do thoát',`<div class="splitbody"><div>${breakdownTable(bd.by_side||[])}</div><div>${breakdownTable(bd.by_reason||[])}</div></div>`,'wide')+panel('Best / Worst trade',`<div class="splitbody"><div class="callout"><b>Best</b><div class="metric">${esc(best.symbol||'không có')} ${esc(best.side||'')} ${esc(money(best.net))}</div><div class="hint">${esc(best.close_ts||best.ts||'')}</div></div><div class="callout bad"><b>Worst</b><div class="metric">${esc(worst.symbol||'không có')} ${esc(worst.side||'')} ${esc(money(worst.net))}</div><div class="hint">${esc(worst.close_ts||worst.ts||'')}</div></div></div>`)+panel('Lệnh paper đã đóng gần nhất',table(['Thời gian','Coin','Chiều','Lý do','Net','R','Fee','Notional','Setup'],closes.map(r=>`<tr>${td(r.close_ts||r.ts)}${td(r.symbol)}${td(r.side)}${td(r.reason)}${td(money(r.net),'num')}${td(r.r_multiple===undefined?'':fixed(r.r_multiple,2),'num')}${td(money(-(num(r.fee||r.fees))),'num')}${td(fixed(r.notional,2),'num')}${td(compactId(r.setup_id||'unknown'))}</tr>`)),'wide');ensureChartTooltip()}
function renderAgents(d){const proc=d.process||{};document.getElementById('view-agents').innerHTML=panel('Watchdog và bot con',kv({'Watchdog PID':proc.watchdog_pid||'none','Watchdog đang chạy':proc.watchdog_running,'Bot con PID':proc.child_pid||'none','Bot con đang chạy':proc.child_running,'Stop file':proc.stop_file_exists}))+panel('Monitor live bên ngoài',table(['Monitor','Trạng thái','PID','Vai trò','Agent control'],(d.live_monitors||[]).map(m=>`<tr>${td(m.name)}${td(m.state)}${td((m.pids||[]).join(', ')||'none','num')}${td(m.role)}${td(m.agent_controls)}</tr>`)))+panel('Heartbeat agent lõi',table(['Agent','Trạng thái','Tuổi dữ liệu','PID','Đang chạy'],(d.heartbeats||[]).map(h=>`<tr>${td(h.name)}<td><span class="dot ${stateClass(h.state)}"></span>${esc(txt(h.state))}</td>${td(h.age,'num')}${td(h.pid,'num')}${td(h.running)}</tr>`)),'wide')+panel('Độ mới logs',table(['File','Có tồn tại','Cập nhật cách đây','Đường dẫn'],(d.logs||[]).map(l=>`<tr>${td(l.name)}${td(l.exists)}${td(l.age,'num')}${td(l.path)}</tr>`)),'wide')}
function renderMarket(d){const m=d.market_state||{},ml=d.market_latest||{},o=d.overview||{};const rowMarket=x=>`<tr>${td(x.symbol)}${td(fixed(x.price,4),'num')}${td(fixed(x.change_pct??x.change_24h_pct,2)+'%','num')}${td(compactNumber(x.quote_volume||x.quote_volume_m),'num')}${td(fixed(x.hot_score,2),'num')}${td(x.funding_pct!==undefined?fixed(x.funding_pct,4)+'%':'','num')}</tr>`;const marketHeaders=['Coin','Giá','24h','Vol quote','Hot','Funding'];document.getElementById('view-market').innerHTML=panel('Regime thị trường',kv({'Regime':m.primary_regime||o.regime,'TB coin lớn 24h':fixed(m.major_avg_24h_pct??0,2)+'%','Số coin lớn tăng':m.major_positive_count??'n/a','Biên độ TB':m.major_range_avg??'n/a','Chase risk':m.chase_risk,'Snapshot thị trường':ml.ts||'none'}))+panel('Tag và crowded',`<div class="chips">${chipList(m.tags||o.tags)}</div><div class="hint" style="margin:10px 0 6px">Coin đang crowded</div><div class="chips">${chipList(m.crowded_symbols||[],'warn')}</div>`)+panel('Coin đang nóng',table(marketHeaders,(ml.hot||m.hot_symbols||[]).map(x=>typeof x==='string'?`<tr>${td(x)}${td('')}${td('')}${td('')}${td('')}${td('')}</tr>`:rowMarket(x))),'wide')+panel('Funding bất thường',table(marketHeaders,(ml.funding_extremes||[]).map(rowMarket)),'wide')+panel('Coin lớn',table(marketHeaders,(ml.majors||[]).map(rowMarket)),'wide')}
function renderNews(d){const n=d.news||{};const impacts=Object.entries(n.symbol_impacts||{}).sort((a,b)=>(num(b[1].risk)-num(a[1].risk))).slice(0,16);document.getElementById('view-news').innerHTML=panel('Rủi ro tin tức',kv({'Cập nhật gần nhất':n.ts||'none','Sự kiện':n.event_count||0,'Macro risk':pct(n.macro_risk_score),'Regulatory risk':pct(n.crypto_regulatory_risk),'Catalyst':pct(n.catalyst_score),'Headline chaos':pct(n.headline_chaos),'Độ mới':pct(n.freshness_score),'Chất lượng nguồn':pct(n.source_quality_score),'Risk contract':n.risk_contract||'tighten_only'}))+panel('Tình trạng nguồn tin',table(['Nguồn','Trạng thái','Số tin','Lỗi'],(n.source_health||[]).map(s=>`<tr>${td(s.source)}${td(s.status)}${td(s.count,'num')}${td(s.error||'')}</tr>`)))+panel('Tin nổi bật',table(['Nguồn','Risk','Coin','Tiêu đề'],(n.top_events||[]).map(e=>`<tr>${td(e.source)}${td(pct(e.risk),'num')}${td((e.symbols||[]).join(', '))}${td(e.title)}</tr>`)),'wide')+panel('Tác động theo coin',table(['Coin','Risk','Bull','Bear'],impacts.map(([sym,v])=>`<tr>${td(sym)}${td(pct(v.risk),'num')}${td(pct(v.bullish),'num')}${td(pct(v.bearish),'num')}</tr>`)),'wide')}
function renderLearning(d){const si=d.self_improvement||{},guard=si.guardrail_proposal||{},c=d.cognitive||{},r=d.reasoning||{},beliefs=d.beliefs||{},setups=d.setups||{},shadow=d.shadow_performance||{},exam=d.daily_exam||{},llm=d.llm_reasoning||{};const examScores=Object.entries(exam.score_snapshot||{});document.getElementById('view-learning').innerHTML=panel('Tự cải thiện',kv({'Cập nhật':si.ts||'none','Điểm học':pct(si.overall_learning_score),'Độ sẵn sàng':si.readiness||'unknown','Điểm mù':(si.blindspots||[]).length,'Có thể nới risk':guard.can_loosen,'Có thể live trade':guard.can_trade_live,'Min score đề xuất':guard.recommended_min_signal_score||'none'}))+panel('LLM reasoning',kv({'Trạng thái':llm.status||'unknown','Provider':llm.provider||'unknown','Model deep':llm.deep_model||'unknown','Model quick':llm.quick_model||'unknown','Tóm tắt':llm.summary||'không có','Không live':!llm.can_place_live_orders,'Không nới risk':!llm.can_loosen_risk,'Lỗi':llm.error||'không có'}))+panel('Kỳ thi ngày',kv({'Ngày':exam.local_date||'chưa có','Loại đề':compactId(exam.exam_type||'không có'),'Điểm chất lượng':fixed(exam.quality_score,1)+' / 100','Grade':exam.quality_grade||'không có','Điểm bài thi':fixed(exam.exam_score,0)+' / 100','Kết quả':exam.passed?'đạt':'chưa đạt','Action':(exam.answer||{}).action||'không có','Paper-only':(exam.contract||{}).paper_only!==false}))+panel('Trọng tâm học hiện tại',kv({'Curiosity type':d.curiosity?.focus_type||'none','Focus id':compactId(d.curiosity?.focus_id||'none'),'Expected value':d.curiosity?.expected_learning_value||'none','Score':d.curiosity?.score||'none','Reasoning mode':(r.decision||{}).mode||'none','Lý do quyết định':(r.decision||{}).reason||'none'}))+panel('Promotion board',renderPromotion(d),'wide')+panel('Ops học máy',renderOpsLearning(d),'wide')+panel('Điểm mù từ model lớn',table(['Blindspot'],(llm.critical_blindspots||[]).map(t=>`<tr>${td(t)}</tr>`)),'wide')+panel('Curriculum từ model lớn',table(['Ưu tiên','Task','Acceptance test'],(llm.curriculum||[]).map(t=>`<tr>${td(t.priority)}${td(t.task)}${td(t.acceptance_test)}</tr>`)),'wide')+panel('Điểm kỳ thi',table(['Trục','Score'],examScores.map(([k,v])=>`<tr>${td(compactId(k))}${td(pct(v),'num')}</tr>`)),'wide')+panel('Learning target từ kỳ thi',table(['Mục tiêu'],(exam.learning_targets||[]).map(t=>`<tr>${td(t)}</tr>`)),'wide')+panel('Lộ trình học',table(['Ưu tiên','Task','Hành động'],(si.learning_curriculum||[]).map(t=>`<tr>${td(t.priority)}${td(t.task)}${td(t.action)}</tr>`)),'wide')+panel('Điểm mù',table(['Mức độ','Loại','Chi tiết'],(si.blindspots||[]).map(b=>`<tr>${td(b.severity)}${td(compactId(b.type))}${td(b.detail)}</tr>`)),'wide')+panel('Giả thuyết cần test',table(['Hypothesis','Setup','Confidence','Coin'],(c.hypotheses_to_test||[]).map(h=>`<tr>${td(compactId(h.hypothesis_id))}${td(compactId(h.setup_id))}${td(pct(h.confidence_prior),'num')}${td((h.symbols||[]).join(', '))}</tr>`)),'wide')+panel('Niềm tin thị trường',table(['Niềm tin','Confidence','Trạng thái'],(beliefs.top||[]).map(b=>`<tr>${td(b.statement)}<td><div class="meter"><div class="bar" style="width:${Math.max(0,Math.min(100,num(b.confidence)*100))}%"></div></div></td>${td(b.status)}</tr>`)),'wide')+panel('Kỹ năng setup',table(['Setup','Bật','Lệnh','WR','Expectancy'],(setups.rows||[]).map(s=>`<tr>${td(compactId(s.setup_id))}${td(s.enabled)}${td(s.trades,'num')}${td(pct(s.win_rate),'num')}${td(money(s.expectancy),'num')}</tr>`)),'wide')+panel('Ứng viên promote / loại bỏ',table(['Loại','Nhóm','Key','Đã đóng','WR','Expectancy'],[...(shadow.promotion_candidates||[]).map(x=>({type:'promote',...x})),...(shadow.kill_candidates||[]).map(x=>({type:'kill',...x}))].map(x=>`<tr>${td(x.type)}${td(x.group)}${td(compactId(x.key))}${td(x.closed,'num')}${td(pct(x.win_rate),'num')}${td(money(x.expectancy),'num')}</tr>`)),'wide')}
function renderPromotion(d){const p=(d.ops||{}).promotion||{};return kv({'Trạng thái':p.state||'paper_learning','Đạt gate':p.passed?'đạt':'chưa đạt','Không đặt live':!p.can_place_live_orders,'Đánh giá lúc':p.evaluated_at||'chưa có','Fail reasons':(p.failures||[]).join(', ')||'không có'})+table(['Metric','Hiện tại','Target','Qua'],(p.rows||[]).map(x=>`<tr>${td(compactId(x.metric))}${td(x.value,'num')}${td(x.target,'num')}${td(x.passed?'OK':'thiếu')}</tr>`))}
function renderOpsLearning(d){const ops=d.ops||{},mu=ops.model_usage||{},sf=ops.skill_forge||{},spi=ops.skill_patch_integration||{},dd=ops.dont_do||{},q=ops.queue||{};return kv({'Model calls':mu.call_count||0,'Token input est':mu.input_tokens_est||0,'Token output est':mu.output_tokens_est||0,'Cost est USD':fixed(mu.cost_usd_est||0,6),'Skill patches pending':sf.pending_count||0,'Skill patches applied':spi.applied_count||0,'DONT_DO rules':(dd.rules||[]).length,'Queue':JSON.stringify(q.by_status||{})})}
function renderCounterfactualLearning(d){const pb=d.phase_b_learning||{},cf=pb.counterfactual||{},recent=pb.recent_replays||[],by=Object.entries(cf.by_conclusion||{}).sort((a,b)=>num(b[1])-num(a[1]));return panel('Counterfactual replay',kv({'Replay total':cf.replay_count||0,'Hoàn tất':cf.complete_count||0,'Chưa đủ dữ liệu':cf.unresolved_count||0,'Coverage':pct(cf.coverage_pct),'Cập nhật':cf.updated_at||'chưa có'}))+panel('Replay gần nhất',table(['Signal','Trạng thái','Lý do','Nguồn candle','Số nến'],recent.map(r=>`<tr>${td(compactId(r.signal_id||''))}${td(r.status)}${td(r.reason||r.conclusion||'')}${td((r.candle_source||{}).source||'unknown')}${td((r.coverage||{}).candle_count,'num')}</tr>`)),'wide')+panel('Kết luận replay',table(['Kết luận','Count'],by.map(([k,v])=>`<tr>${td(compactId(k))}${td(v,'num')}</tr>`)))}
const baseRenderOverview=renderOverview;renderOverview=function(d){baseRenderOverview(d);mountSubtabs('view-overview',[{id:'system',label:'Hệ thống',count:2},{id:'paper',label:'Paper & học',count:3},{id:'events',label:'Sự kiện',count:'rest'}])}
const baseRenderReport=renderReport;renderReport=function(d){baseRenderReport(d);mountSubtabs('view-report',[{id:'summary',label:'Tổng hợp',count:3},{id:'charts',label:'Biểu đồ',count:4},{id:'breakdown',label:'Phân rã',count:2},{id:'history',label:'Lịch sử',count:'rest'}])}
const baseRenderMarket=renderMarket;renderMarket=function(d){baseRenderMarket(d);mountSubtabs('view-market',[{id:'regime',label:'Regime',count:2},{id:'hot',label:'Coin nóng',count:1},{id:'lists',label:'Funding & majors',count:'rest'}])}
const baseRenderNews=renderNews;renderNews=function(d){baseRenderNews(d);mountSubtabs('view-news',[{id:'risk',label:'Risk',count:1},{id:'sources',label:'Nguồn tin',count:1},{id:'events',label:'Tin & coin',count:'rest'}])}
const baseRenderLearning=renderLearning;renderLearning=function(d){baseRenderLearning(d);document.getElementById('view-learning').insertAdjacentHTML('beforeend',renderCounterfactualLearning(d));mountSubtabs('view-learning',[{id:'summary',label:'Tổng hợp',count:4},{id:'promotion',label:'Promotion',count:2},{id:'llm',label:'Model lớn',count:2},{id:'exam',label:'Kỳ thi',count:2},{id:'curriculum',label:'Curriculum',count:3},{id:'skills',label:'Skills & edge',count:3},{id:'counterfactual',label:'Replay',count:'rest'}])}
const baseRenderAgents=renderAgents;renderAgents=function(d){baseRenderAgents(d);const ops=d.ops||{};document.getElementById('view-agents').insertAdjacentHTML('beforeend',panel('Paper loop và microstructure loop',kv({'Paper loop':(ops.paper_loop||{}).status||'chưa có','Paper loop action':((ops.paper_loop||{}).decision||{}).action||(ops.paper_loop||{}).action||'không có','Paper candidates':(ops.paper_loop||{}).candidate_count||0,'Microstructure':(ops.microstructure_loop||{}).status||'chưa có','Micro results':(ops.microstructure_loop||{}).result_count||0,'Host runtime':(ops.host_runtime||{}).status||'chưa có','Security import guard':(ops.security_import_guard||{}).ok===false?'vi phạm':'OK'}),'wide'));mountSubtabs('view-agents',[{id:'runtime',label:'Runtime',count:2},{id:'health',label:'Heartbeat',count:1},{id:'logs',label:'Logs',count:1},{id:'loops',label:'Loops',count:'rest'}])}
async function renderLogsShell(){const el=document.getElementById('view-logs');if(!el.dataset.ready){el.innerHTML=panel('Logs live',`<div class="actions" style="margin-bottom:10px;flex-wrap:wrap"><button class="btn" data-log="scalp_autotrader">Autotrader</button><button class="btn" data-log="scalp_watchdog">Watchdog</button><button class="btn" data-log="autotrader_err">Lỗi</button><button class="btn" data-log="autotrader_out">Output</button><button class="btn" data-log="market_updates">Thị trường</button><button class="btn" data-log="news_events">Tin tức</button></div><div class="log" id="logbox">chọn một log</div>`,'wide');el.dataset.ready='1';el.querySelectorAll('[data-log]').forEach(b=>b.onclick=()=>loadLog(b.dataset.log));loadLog('scalp_autotrader')}}
async function loadLog(name){try{document.getElementById('logbox').textContent=await fetchText('/api/log?name='+encodeURIComponent(name)+'&lines=220')}catch(e){document.getElementById('logbox').textContent='lỗi nhật ký '+e}}
function showView(view){const b=document.querySelector(`.nav button[data-view="${view}"]`);const panel=document.getElementById('view-'+view);if(!b||!panel)return;document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.querySelectorAll('.view').forEach(v=>v.classList.add('hidden'));panel.classList.remove('hidden')}
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>showView(b.dataset.view));
showView(new URLSearchParams(location.search).get('view')||'overview');
document.getElementById('refresh').onclick=load;document.getElementById('pause').onclick=()=>{paused=!paused;document.getElementById('pause').textContent=paused?'Tiếp tục':'Tạm dừng'};
load();setInterval(load,5000);
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AgentDashboard/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, status: int = 200) -> None:
        self.send_bytes(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/dashboard"}:
            self.send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/status":
            self.send_json(load_dashboard_status())
            return
        if parsed.path == "/api/drilldown":
            params = parse_qs(parsed.query)
            identifier = (params.get("id") or [""])[0]
            if not identifier:
                self.send_json({"error": "missing_id"}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json(explain_decision(identifier))
            return
        if parsed.path == "/api/log":
            params = parse_qs(parsed.query)
            name = (params.get("name") or ["scalp_autotrader"])[0]
            lines = int((params.get("lines") or ["120"])[0])
            path = LOG_FILES.get(name)
            if not path:
                self.send_bytes(b"unknown log", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
                return
            self.send_bytes(tail_text(path, lines).encode("utf-8", errors="replace"), "text/plain; charset=utf-8")
            return
        self.send_json({"error": "not_found", "path": parsed.path}, HTTPStatus.NOT_FOUND)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the single-page trading agent dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--open", action="store_true", help="Open one browser tab after server starts")
    parser.add_argument("--once", action="store_true", help="Print status JSON and exit")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.once:
        print(json.dumps(load_dashboard_status(), ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    url = f"http://{args.host}:{args.port}/"
    with ReusableThreadingHTTPServer((args.host, args.port), DashboardHandler) as server:
        print(f"agent_dashboard {url}", flush=True)
        if args.open:
            webbrowser.open(url, new=1)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
