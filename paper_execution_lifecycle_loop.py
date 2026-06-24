"""Paper-only execution lifecycle loop.

This daemon turns an approved ``paper_open_candidate`` decision into a clean
simulated trade lifecycle. It opens paper positions, monitors mark-price
snapshots, closes on SL/TP/timeout, and emits validated learning rows. It never
imports exchange clients and cannot place live orders.
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, append_jsonl_once, read_json, write_json_atomic
from live_permission_firewall import evaluate_live_permission
from market_data_lake import store_candles
from paper_execution_simulator import TAKER_FEE_RATE
from paper_portfolio_manager import close_paper_position, load_account, open_paper_position, save_account
from post_trade_learning_agent import review_closed_trade
from timebase import seconds_between, utc_now
from trade_lifecycle_validator import write_latest_report

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PID_FILE = STATE_DIR / "paper_execution_lifecycle_loop.pid"
HEARTBEAT_PATH = STATE_DIR / "paper_execution_lifecycle_loop_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_PAPER_EXECUTION_LIFECYCLE_LOOP"
DECISION_LATEST = MEMORY_DIR / "autonomous_paper_trading_loop_latest.json"
MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
LATEST_PATH = MEMORY_DIR / "paper_execution_lifecycle_latest.json"
HISTORY_PATH = MEMORY_DIR / "paper_execution_lifecycle_history.jsonl"
SEEN_PATH = MEMORY_DIR / "paper_execution_lifecycle_seen.json"
PAPER_TRADES_PATH = MEMORY_DIR / "paper_trades.jsonl"
MAX_HOLD_SECONDS = 30 * 60
MAX_OPEN_POSITIONS = 3
FUNDING_INTERVAL_HOURS = 8
MAX_REPLAY_CANDLES = 240


def dec(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def dec_str(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.00000001")).normalize())

def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except Exception:
        return None

def calculate_entry_fee(risk: dict[str, Any]) -> Decimal:
    return abs(dec(risk.get("notional")) * TAKER_FEE_RATE)

def funding_rate_from_row(row: dict[str, Any] | None) -> Decimal:
    if not isinstance(row, dict):
        return Decimal("0")
    if row.get("funding_rate") not in (None, ""):
        return dec(row.get("funding_rate"))
    if row.get("funding_pct") not in (None, ""):
        return dec(row.get("funding_pct")) / Decimal("100")
    return Decimal("0")

def funding_periods_crossed(open_ts: Any, close_ts: Any, interval_hours: int = FUNDING_INTERVAL_HOURS) -> int:
    opened = parse_ts(open_ts)
    closed = parse_ts(close_ts)
    if not opened or not closed or closed <= opened:
        return 0
    interval = timedelta(hours=interval_hours)
    anchor = closed.replace(hour=(closed.hour // interval_hours) * interval_hours, minute=0, second=0, microsecond=0)
    while anchor > closed:
        anchor -= interval
    count = 0
    boundary = anchor
    while boundary > opened:
        count += 1
        boundary -= interval
    return count

def calculate_funding_payment(position: dict[str, Any], market_row: dict[str, Any] | None, close_ts: Any) -> Decimal:
    rate = funding_rate_from_row(market_row)
    periods = funding_periods_crossed(position.get("opened_at"), close_ts)
    if rate == 0 or periods <= 0:
        return Decimal("0")
    payment = dec(position.get("notional")) * rate * Decimal(periods)
    return -payment if str(position.get("side") or "").upper() == "LONG" else payment


def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json_atomic(HEARTBEAT_PATH, row)
    return row


def write_latest(row: dict[str, Any]) -> dict[str, Any]:
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), **row, "can_place_live_orders": False}
    write_json_atomic(LATEST_PATH, payload)
    append_jsonl(HISTORY_PATH, payload)
    return payload


def load_seen(path: Path | None = None) -> dict[str, Any]:
    path = path or SEEN_PATH
    payload = read_json(path, default={})
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("risk_decision_ids", [])
    payload.setdefault("candidate_ids", [])
    return payload


def save_seen(seen: dict[str, Any], path: Path | None = None) -> None:
    path = path or SEEN_PATH
    trimmed = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "risk_decision_ids": list(dict.fromkeys(str(x) for x in seen.get("risk_decision_ids", []) if x))[-500:],
        "candidate_ids": list(dict.fromkeys(str(x) for x in seen.get("candidate_ids", []) if x))[-500:],
    }
    write_json_atomic(path, trimmed)


def market_rows(market: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("hot", "top_gainers", "top_losers", "top_volume", "funding_extremes", "majors"):
        batch = market.get(key) if isinstance(market.get(key), list) else []
        rows.extend(row for row in batch if isinstance(row, dict))
    return rows


def find_market_row(symbol: str, market: dict[str, Any]) -> dict[str, Any] | None:
    target = str(symbol or "").upper()
    for row in market_rows(market):
        if str(row.get("symbol") or "").upper() == target:
            return row
    return None


def mark_candle(symbol: str, market: dict[str, Any]) -> dict[str, Any] | None:
    row = find_market_row(symbol, market)
    if not row:
        return None
    price = dec(row.get("price"))
    if price <= 0:
        return None
    ts = str(market.get("ts") or market.get("updated_at") or utc_now())
    return {
        "ts": ts,
        "open": float(price),
        "high": float(price),
        "low": float(price),
        "close": float(price),
        "volume": float(row.get("quote_volume") or 0),
        "quality": "mark_only_snapshot",
    }

def append_replay_candle(position: dict[str, Any], candle: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in position.get("replay_candles", []) if isinstance(row, dict)]
    normalized = {
        "ts": candle.get("ts") or utc_now(),
        "open": float(candle.get("open") or candle.get("close") or 0.0),
        "high": float(candle.get("high") or candle.get("close") or 0.0),
        "low": float(candle.get("low") or candle.get("close") or 0.0),
        "close": float(candle.get("close") or candle.get("open") or 0.0),
        "volume": float(candle.get("volume") or 0.0),
        "quality": str(candle.get("quality") or "mark_only_snapshot"),
    }
    if normalized["close"] <= 0:
        return position
    if rows and rows[-1].get("ts") == normalized["ts"]:
        rows[-1] = normalized
    else:
        rows.append(normalized)
    rows = rows[-MAX_REPLAY_CANDLES:]
    return {
        **position,
        "replay_candles": rows,
        "replay_candle_count": len(rows),
        "replay_data_quality": "mark_sequence" if len(rows) >= 3 else "mark_only_snapshot",
    }

def persist_open_position(position: dict[str, Any]) -> None:
    account = load_account()
    positions = []
    changed = False
    for row in account.get("open_positions", []) if isinstance(account.get("open_positions"), list) else []:
        if isinstance(row, dict) and row.get("position_id") == position.get("position_id"):
            positions.append(position)
            changed = True
        else:
            positions.append(row)
    if changed:
        save_account({**account, "open_positions": positions})

def build_replay_cache(position: dict[str, Any], fallback_candle: dict[str, Any]) -> dict[str, Any]:
    candles = [row for row in position.get("replay_candles", []) if isinstance(row, dict)]
    if not candles and fallback_candle:
        candles = [fallback_candle]
    if len(candles) < 3:
        return {"data_quality": "mark_only_snapshot", "replay_candle_count": len(candles)}
    cached = store_candles(
        str(position.get("symbol") or "UNKNOWN"),
        "mark_sequence",
        candles,
        source_id="paper_lifecycle_mark_sequence",
        assumptions={"quality": "observed_mark_snapshots", "not_full_ohlcv": True},
    )
    return {
        "data_quality": "mark_sequence",
        "replay_candle_count": len(candles),
        "replay_candle_cache_id": cached.get("cache_id"),
        "candle_cache_id": cached.get("cache_id"),
    }


def open_position_conflicts(account: dict[str, Any], candidate: dict[str, Any], risk: dict[str, Any]) -> bool:
    for position in account.get("open_positions", []) if isinstance(account.get("open_positions"), list) else []:
        if not isinstance(position, dict):
            continue
        if position.get("risk_decision_id") == risk.get("risk_decision_id"):
            return True
        same_symbol = str(position.get("symbol") or "").upper() == str(candidate.get("symbol") or "").upper()
        same_side = str(position.get("side") or "").upper() == str(candidate.get("side") or "").upper()
        same_setup = str(position.get("setup_id") or "") == str(candidate.get("setup_id") or "")
        if same_symbol and same_side and same_setup:
            return True
    return False


def build_open_event(position: dict[str, Any], candidate: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "paper_open",
        "trade_id": position.get("position_id"),
        "mode": "paper",
        "ts": position.get("opened_at") or utc_now(),
        "open_ts": position.get("opened_at") or utc_now(),
        "symbol": position.get("symbol"),
        "side": position.get("side"),
        "setup_id": position.get("setup_id"),
        "entry": position.get("entry"),
        "qty": position.get("qty"),
        "margin": position.get("margin"),
        "leverage": position.get("leverage"),
        "entry_fee": position.get("entry_fee") or "0",
        "fee": position.get("entry_fee") or "0",
        "sl": position.get("sl"),
        "tp": position.get("tp"),
        "risk_decision_id": position.get("risk_decision_id"),
        "candidate_id": candidate.get("candidate_id"),
        "market_snapshot_ts": candidate.get("market_snapshot_ts"),
        "reasoning_id": decision.get("decided_at"),
        "status": "open",
        "position": position,
        "can_place_live_orders": False,
    }


def should_close(position: dict[str, Any], candle: dict[str, Any], max_hold_seconds: int = MAX_HOLD_SECONDS) -> dict[str, Any] | None:
    side = str(position.get("side") or "").upper()
    mark = dec(candle.get("close"))
    sl = dec(position.get("sl"))
    tp = dec(position.get("tp"))
    if side == "LONG":
        if mark <= sl:
            return {"reason": "sl", "exit": mark}
        if mark >= tp:
            return {"reason": "tp", "exit": mark}
    if side == "SHORT":
        if mark >= sl:
            return {"reason": "sl", "exit": mark}
        if mark <= tp:
            return {"reason": "tp", "exit": mark}
    age = seconds_between(position.get("opened_at"), candle.get("ts"))
    wall_age = seconds_between(position.get("opened_at"), utc_now())
    if (age is not None and age >= max_hold_seconds) or (wall_age is not None and wall_age >= max_hold_seconds):
        return {"reason": "timeout", "exit": mark}
    return None


def build_close_event(position: dict[str, Any], closed: dict[str, Any], candle: dict[str, Any]) -> dict[str, Any]:
    replay_cache = build_replay_cache({**position, **closed}, candle)
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "paper_close",
        "trade_id": position.get("position_id"),
        "mode": "paper",
        "ts": closed.get("closed_at") or candle.get("ts") or utc_now(),
        "open_ts": position.get("opened_at"),
        "close_ts": closed.get("closed_at") or candle.get("ts") or utc_now(),
        "symbol": position.get("symbol"),
        "side": position.get("side"),
        "setup_id": position.get("setup_id"),
        "entry": position.get("entry"),
        "exit": closed.get("exit"),
        "qty": position.get("qty"),
        "margin": position.get("margin"),
        "leverage": position.get("leverage"),
        "sl": position.get("sl"),
        "tp": position.get("tp"),
        "entry_fee": closed.get("entry_fee") or position.get("entry_fee") or "0",
        "exit_fee": closed.get("exit_fee") or "0",
        "fee": closed.get("fee") or "0",
        "fees": closed.get("fees") or closed.get("fee") or "0",
        "funding_payment": closed.get("funding_payment") or "0",
        "net_before_funding": closed.get("net_before_funding"),
        "slippage": "0",
        "gross": closed.get("gross"),
        "net": closed.get("net"),
        "reason": closed.get("reason"),
        "status": "closed",
        "risk_decision_id": position.get("risk_decision_id"),
        "market_snapshot_ts": candle.get("ts"),
        "data_quality": replay_cache.get("data_quality") or candle.get("quality"),
        "replay_candle_count": replay_cache.get("replay_candle_count"),
        "replay_candle_cache_id": replay_cache.get("replay_candle_cache_id"),
        "candle_cache_id": replay_cache.get("candle_cache_id"),
        "position": {**position, **closed, **replay_cache},
        "can_place_live_orders": False,
    }


def try_open_latest_decision(account: dict[str, Any], market: dict[str, Any] | None = None) -> dict[str, Any] | None:
    latest = read_json(DECISION_LATEST, default={})
    decision = latest.get("decision") if isinstance(latest.get("decision"), dict) else {}
    if decision.get("action") != "paper_open_candidate":
        return None
    open_positions = [row for row in account.get("open_positions", []) if isinstance(row, dict)] if isinstance(account.get("open_positions"), list) else []
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return {"action": "open_skipped", "reason": "max_open_positions_reached", "open_positions": len(open_positions)}
    candidate = decision.get("candidate") if isinstance(decision.get("candidate"), dict) else {}
    risk = decision.get("risk_decision") if isinstance(decision.get("risk_decision"), dict) else {}
    if not risk.get("can_open_paper"):
        return {"action": "open_skipped", "reason": "risk_decision_rejected", "risk_decision_id": risk.get("risk_decision_id")}
    seen = load_seen()
    candidate_id = str(candidate.get("candidate_id") or "")
    risk_id = str(risk.get("risk_decision_id") or "")
    if risk_id in set(seen.get("risk_decision_ids") or []) or candidate_id in set(seen.get("candidate_ids") or []):
        return {"action": "open_skipped", "reason": "decision_already_consumed", "risk_decision_id": risk_id, "candidate_id": candidate_id}
    if open_position_conflicts(account, candidate, risk):
        return {"action": "open_skipped", "reason": "matching_position_already_open", "risk_decision_id": risk_id, "candidate_id": candidate_id}
    entry_fee = calculate_entry_fee(risk)
    opened = open_paper_position(risk, account=account, entry_fee=entry_fee)
    if not opened.get("ok"):
        return {"action": "open_failed", "reason": opened.get("reason"), "risk_decision_id": risk_id, "candidate_id": candidate_id}
    position = opened["position"]
    entry_candle = mark_candle(str(position.get("symbol") or ""), market or {})
    if entry_candle:
        position = append_replay_candle(position, entry_candle)
        persist_open_position(position)
    event = build_open_event(position, candidate, decision)
    append_jsonl_once(PAPER_TRADES_PATH, event, "trade_id")
    seen["risk_decision_ids"] = list(seen.get("risk_decision_ids") or []) + [risk_id]
    if candidate_id:
        seen["candidate_ids"] = list(seen.get("candidate_ids") or []) + [candidate_id]
    save_seen(seen)
    return {"action": "opened", "event": event, "account": load_account(), "can_place_live_orders": False}


def monitor_open_positions(account: dict[str, Any], market: dict[str, Any], max_hold_seconds: int = MAX_HOLD_SECONDS) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for position in list(account.get("open_positions", []) if isinstance(account.get("open_positions"), list) else []):
        if not isinstance(position, dict):
            continue
        candle = mark_candle(str(position.get("symbol") or ""), market)
        if not candle:
            results.append({"action": "monitor_wait", "position_id": position.get("position_id"), "reason": "missing_mark_price"})
            continue
        position = append_replay_candle(position, candle)
        close_plan = should_close(position, candle, max_hold_seconds=max_hold_seconds)
        if not close_plan:
            persist_open_position(position)
            results.append({"action": "monitor_hold", "position_id": position.get("position_id"), "symbol": position.get("symbol"), "mark": candle.get("close")})
            continue
        persist_open_position(position)
        close_ts = candle.get("ts") or utc_now()
        fee = abs(close_plan["exit"] * dec(position.get("qty")) * TAKER_FEE_RATE)
        funding_payment = calculate_funding_payment(position, find_market_row(str(position.get("symbol") or ""), market), close_ts)
        closed_result = close_paper_position(str(position.get("position_id")), close_plan["exit"], fee=fee, reason=str(close_plan["reason"]), funding_payment=funding_payment)
        if not closed_result.get("ok"):
            results.append({"action": "close_failed", "position_id": position.get("position_id"), "reason": closed_result.get("reason")})
            continue
        close_event = build_close_event(position, closed_result["position"], candle)
        append_jsonl(PAPER_TRADES_PATH, close_event)
        review_candles = [row for row in close_event.get("position", {}).get("replay_candles", []) if isinstance(row, dict)] or [candle]
        review = review_closed_trade(close_event, review_candles, setup_score={"score": 0.6}, append=True)
        account = closed_result["account"]
        results.append({"action": "closed", "event": close_event, "review": review, "account": account, "can_place_live_orders": False})
    return results


def run_once(max_hold_seconds: int = MAX_HOLD_SECONDS) -> dict[str, Any]:
    live_gate = evaluate_live_permission({"action": "paper_execution_lifecycle", "mode": "paper"})
    if not live_gate.get("allowed"):
        row = write_latest({"status": "blocked", "action": "skip", "reason": "live_firewall_block", "live_gate": live_gate})
        write_heartbeat("blocked", {"reason": "live_firewall_block"})
        return row
    account = load_account()
    market = read_json(MARKET_LATEST, default={})
    monitor_results = monitor_open_positions(account, market, max_hold_seconds=max_hold_seconds)
    account_after_monitor = load_account()
    open_result = try_open_latest_decision(account_after_monitor, market=market)
    account_for_validation = load_account()
    lifecycle_report = write_latest_report(paths=[PAPER_TRADES_PATH], min_open_ts=account_for_validation.get("created_at"))
    actions = [row.get("action") for row in monitor_results]
    if open_result:
        actions.append(str(open_result.get("action")))
    status = "ok" if lifecycle_report.get("learning_allowed") else "degraded"
    row = write_latest(
        {
            "status": status,
            "action": "lifecycle_tick",
            "actions": actions,
            "monitor_results": monitor_results[-10:],
            "open_result": open_result,
            "open_positions": len(load_account().get("open_positions") or []),
            "lifecycle": lifecycle_report,
        }
    )
    write_heartbeat(status, {"actions": actions[-5:], "open_positions": row.get("open_positions")})
    return row


def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))


def read_pid(path: Path = PID_FILE) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run paper-only execution lifecycle loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--max-hold-seconds", type=int, default=MAX_HOLD_SECONDS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.max_hold_seconds <= 0:
        parser.error("--max-hold-seconds must be positive")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        print({"pid": read_pid(), "latest": str(LATEST_PATH), "heartbeat": str(HEARTBEAT_PATH), "stop_file": str(STOP_FILE)})
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        row = run_once(max_hold_seconds=args.max_hold_seconds)
        print(f"paper_execution_lifecycle_loop status={row.get('status')} actions={','.join(row.get('actions') or [])}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
