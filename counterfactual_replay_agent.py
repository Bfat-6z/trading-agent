"""Counterfactual replay for skipped and closed paper signals."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_json, read_jsonl, write_json_atomic
from host_runtime_monitor import acknowledge_sleep_resume_replay
from live_permission_firewall import sanitize_and_detect
from market_data_lake import coverage_report, load_candles, select_window
from market_learner import valid_paper_close
from paper_execution_simulator import TAKER_FEE_RATE, dec, dstr, exit_slippage, liquidation_price, simulate_entry_order, simulate_round_trip
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PAPER_TRADES_JSONL = MEMORY_DIR / "paper_trades.jsonl"
PAPER_BRAIN_HISTORY_JSONL = MEMORY_DIR / "paper_trading_brain_history.jsonl"
PAPER_CANDIDATE_HISTORY_JSONL = MEMORY_DIR / "paper_candidate_feeder_history.jsonl"
REPLAYS_JSONL = MEMORY_DIR / "counterfactual_replays.jsonl"
LATEST_JSON = MEMORY_DIR / "counterfactual_latest.json"
PID_FILE = STATE_DIR / "counterfactual_replay_agent.pid"
HEARTBEAT_PATH = STATE_DIR / "counterfactual_replay_agent_heartbeat.json"
STOP_FILE = STATE_DIR / "counterfactual_replay_agent.stop"


def replay_id(signal_id: str, variant: str) -> str:
    return "cf_" + hashlib.sha256(f"{signal_id}:{variant}".encode("utf-8")).hexdigest()[:20]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def compact_base_signal(signal: dict[str, Any]) -> dict[str, Any]:
    base = {key: value for key, value in signal.items() if key != "candles"}
    if isinstance(signal.get("candles"), list):
        base["embedded_candle_count"] = len(signal["candles"])
    return base

def signal_id_for(row: dict[str, Any]) -> str:
    return str(row.get("signal_id") or row.get("trade_id") or row.get("shadow_id") or row.get("risk_decision_id") or "signal")

def latest_replay_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    latest: dict[str, tuple[tuple[float, int], dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        key = str(row.get("signal_id") or row.get("replay_id") or f"row_{index}")
        ts_ns = safe_float(row.get("created_at_ns"), -1.0)
        parsed = parse_utc(row.get("created_at") or row.get("updated_at"))
        ts = ts_ns if ts_ns >= 0 else parsed.timestamp() if parsed else -1.0
        sort_key = (ts, index)
        if key not in latest or sort_key > latest[key][0]:
            latest[key] = (sort_key, row)
    return [item[1] for item in latest.values()], max(0, len(rows) - len(latest))

def latest_replay_for_signal(signal_id: str) -> dict[str, Any] | None:
    latest, _ = latest_replay_rows([row for row in read_jsonl(REPLAYS_JSONL) if str(row.get("signal_id")) == str(signal_id)])
    return latest[0] if latest else None

def _explicit_cutoff(signal: dict[str, Any]):
    return parse_utc(signal.get("trial_seq_cutoff") or signal.get("source_available_at_max"))

def _requires_replay_cutoff(signal: dict[str, Any]) -> bool:
    source = str(signal.get("eligible_source") or "")
    return source in {"paper_candidate_census", "paper_brain_decision", "paper_close"} or bool(signal.get("blocked") or signal.get("block_reason"))

def missing_cutoff_errors(signal: dict[str, Any]) -> list[str]:
    if _requires_replay_cutoff(signal) and not _explicit_cutoff(signal):
        return ["missing_replay_cutoff"]
    return []

def future_data_errors(signal: dict[str, Any], candles: list[dict[str, Any]]) -> list[str]:
    cutoff = _explicit_cutoff(signal)
    if not cutoff:
        return []
    errors: list[str] = []
    for index, candle in enumerate(candles):
        for field in ("available_at", "known_at", "ingested_at", "finalized_at", "ts"):
            parsed = parse_utc(candle.get(field))
            if parsed and parsed > cutoff:
                errors.append(f"future_{field}_candle_{index}")
                break
    return errors

def replay_metadata(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "shadow_online": bool(signal.get("shadow_online", not signal.get("backfilled"))),
        "first_computed_at": signal.get("first_computed_at") or signal.get("created_at") or signal.get("updated_at"),
        "source_available_at_max": signal.get("source_available_at_max"),
        "trial_seq_cutoff": signal.get("trial_seq_cutoff"),
        "eligible_source": signal.get("eligible_source"),
        "eligible_reason": signal.get("eligible_reason") or signal.get("block_reason"),
    }

def replay_source_signature(signal: dict[str, Any], candles: list[dict[str, Any]] | None = None, candle_source: dict[str, Any] | None = None) -> str:
    payload = {
        "signal_id": signal_id_for(signal),
        "symbol": signal.get("symbol"),
        "side": signal.get("side"),
        "entry": signal.get("entry"),
        "sl": signal.get("sl"),
        "tp": signal.get("tp"),
        "qty": signal.get("qty"),
        "leverage": signal.get("leverage"),
        "open_ts": signal.get("open_ts"),
        "close_ts": signal.get("close_ts"),
        "source_available_at_max": signal.get("source_available_at_max"),
        "trial_seq_cutoff": signal.get("trial_seq_cutoff"),
        "candle_source": candle_source or {},
        "candle_count": len(candles or []),
        "first_candle_ts": (candles or [{}])[0].get("ts") if candles else None,
        "last_candle_ts": (candles or [{}])[-1].get("ts") if candles else None,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]

def finalize_replay_result(result: dict[str, Any], append: bool) -> dict[str, Any]:
    signal_id = str(result.get("signal_id") or "")
    result.setdefault("created_at", utc_now())
    result.setdefault("created_at_ns", time.time_ns())
    prior = latest_replay_for_signal(signal_id) if signal_id else None
    if prior and prior.get("replay_id") != result.get("replay_id"):
        result["supersedes_replay_id"] = prior.get("replay_id")
        result["previous_status"] = prior.get("status")
        result["previous_reason"] = prior.get("reason") or prior.get("conclusion")
        if prior.get("status") != result.get("status"):
            result["is_correction_event"] = True
            result["correction_reason"] = f"{prior.get('status')}_to_{result.get('status')}"
            result["replay_id"] = replay_id(signal_id, f"{result.get('replay_id')}:correction:{prior.get('replay_id')}")
    if append:
        append_jsonl_once(REPLAYS_JSONL, result, "replay_id")
        write_latest_summary()
    return result

def _cache_id_from_signal(signal: dict[str, Any]) -> str | None:
    for key in ("candle_cache_id", "replay_candle_cache_id"):
        value = signal.get(key)
        if value:
            return str(value)
    position = signal.get("position") if isinstance(signal.get("position"), dict) else {}
    value = position.get("candle_cache_id") or position.get("replay_candle_cache_id")
    return str(value) if value else None

def _window_candles(candles: list[dict[str, Any]], signal: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start_ts = signal.get("open_ts") or signal.get("opened_at")
    end_ts = signal.get("close_ts") or signal.get("closed_at")
    if not start_ts and not end_ts:
        return candles, {"windowed": False}
    window = select_window(candles, str(start_ts) if start_ts else None, str(end_ts) if end_ts else None)
    return window, {"windowed": True, "start_ts": start_ts, "end_ts": end_ts, "source_candle_count": len(candles)}

def candles_for_signal(signal: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    embedded = signal.get("candles")
    if isinstance(embedded, list) and embedded:
        candles, meta = _window_candles(embedded, signal)
        return candles, {"source": "embedded", "cache_id": None, **meta}

    cache_id = _cache_id_from_signal(signal)
    if cache_id:
        payload = load_candles(cache_id)
        candles = payload.get("candles") if isinstance(payload, dict) else None
        if isinstance(candles, list) and candles:
            window, meta = _window_candles(candles, signal)
            return window, {"source": "market_data_lake", "cache_id": cache_id, "timeframe": payload.get("timeframe"), **meta}
        return [], {"source": "market_data_lake", "cache_id": cache_id, "error": "cache_missing_or_empty"}

    if str(signal.get("data_quality") or "") == "mark_only_snapshot":
        price = safe_float(signal.get("entry") or signal.get("price") or signal.get("exit"))
        if price > 0:
            ts = signal.get("open_ts") or signal.get("close_ts") or utc_now()
            return [{"ts": ts, "open": price, "high": price, "low": price, "close": price, "volume": 0.0}], {"source": "mark_only_snapshot", "cache_id": None}

    return [], {"source": "missing", "cache_id": None, "error": "no_replay_candles"}

def replayed_complete_signal_ids(path: Path = REPLAYS_JSONL) -> set[str]:
    return {str(row.get("signal_id")) for row in read_jsonl(path) if row.get("signal_id") and row.get("status") == "complete"}

def validate_replay_signal(signal: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not signal.get("symbol"):
        errors.append("missing_symbol")
    if str(signal.get("side") or "").upper() not in {"LONG", "SHORT"}:
        errors.append("invalid_side")
    for field in ("entry", "sl", "tp", "qty"):
        if safe_float(signal.get(field)) <= 0:
            errors.append(f"invalid_{field}")
    return errors

def _mark_to_market_exit(side: str, entry: Any, qty: Any, exit_price: Any, ts: Any, reason: str) -> dict[str, Any]:
    side_up = str(side).upper()
    entry_dec = dec(entry)
    qty_dec = dec(qty)
    close = exit_slippage(dec(exit_price), side_up)
    gross = (close - entry_dec) * qty_dec if side_up == "LONG" else (entry_dec - close) * qty_dec
    fee = abs(close * qty_dec) * TAKER_FEE_RATE
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "closed",
        "reason": reason,
        "close_ts": ts or utc_now(),
        "exit": dstr(close),
        "gross": dstr(gross),
        "fee": dstr(fee),
        "net": dstr(gross - fee),
        "promotion_blocked": False,
    }

def _simulate_time_exit(trade: dict[str, Any], candles: list[dict[str, Any]]) -> dict[str, Any]:
    if not candles:
        return {"status": "skipped", "reason": "missing_candles"}
    entry = simulate_entry_order(trade["symbol"], trade["side"], trade.get("order_type", "market"), trade["qty"], trade["entry"], candles[0], append_order=False)
    if entry["status"] not in {"filled", "partial"}:
        return {**entry, "trade_status": "not_opened"}
    exit_candles = candles[1:] or candles
    close_candle = exit_candles[-1]
    close_price = close_candle.get("close") or close_candle.get("open") or entry["fill_price"]
    exit_row = _mark_to_market_exit(trade["side"], entry["fill_price"], entry.get("filled_qty") or trade["qty"], close_price, close_candle.get("ts"), "time_exit")
    return {"entry_order": entry, "exit": exit_row, "trade_status": "closed"}

def _simulate_trailing_1r(trade: dict[str, Any], candles: list[dict[str, Any]]) -> dict[str, Any]:
    if not candles:
        return {"status": "skipped", "reason": "missing_candles"}
    entry = simulate_entry_order(trade["symbol"], trade["side"], trade.get("order_type", "market"), trade["qty"], trade["entry"], candles[0], append_order=False)
    if entry["status"] not in {"filled", "partial"}:
        return {**entry, "trade_status": "not_opened"}

    side = str(trade["side"]).upper()
    entry_price = dec(entry["fill_price"])
    qty = dec(entry.get("filled_qty") or trade["qty"])
    active_sl = dec(trade["sl"])
    tp = dec(trade["tp"])
    risk = abs(entry_price - active_sl)
    liq = liquidation_price(entry_price, side, trade.get("leverage", "1"))

    for candle in candles[1:] or candles:
        open_price = dec(candle.get("open"))
        high = dec(candle.get("high"))
        low = dec(candle.get("low"))
        if side == "LONG":
            if high >= entry_price + risk:
                active_sl = max(active_sl, entry_price)
            if low <= liq:
                close = min(open_price, liq) if open_price < entry_price else liq
                reason = "liquidation"
            elif low <= active_sl:
                close = exit_slippage(open_price if open_price < active_sl else active_sl, side)
                reason = "trailing_sl" if active_sl == entry_price else "sl"
            elif high >= tp:
                close = exit_slippage(tp, side)
                reason = "tp"
            else:
                continue
            gross = (close - entry_price) * qty
        else:
            if low <= entry_price - risk:
                active_sl = min(active_sl, entry_price)
            if high >= liq:
                close = max(open_price, liq) if open_price > entry_price else liq
                reason = "liquidation"
            elif high >= active_sl:
                close = exit_slippage(open_price if open_price > active_sl else active_sl, side)
                reason = "trailing_sl" if active_sl == entry_price else "sl"
            elif low <= tp:
                close = exit_slippage(tp, side)
                reason = "tp"
            else:
                continue
            gross = (entry_price - close) * qty
        fee = abs(close * qty) * TAKER_FEE_RATE
        exit_row = {
            "schema_version": SCHEMA_VERSION,
            "status": "closed",
            "reason": reason,
            "close_ts": candle.get("ts") or utc_now(),
            "exit": dstr(close),
            "gross": dstr(gross),
            "fee": dstr(fee),
            "net": dstr(gross - fee),
            "liquidation_price": dstr(liq),
            "promotion_blocked": reason == "liquidation",
        }
        return {"entry_order": entry, "exit": exit_row, "trade_status": "closed"}
    return {"entry_order": entry, "exit": {"status": "open", "reason": "unresolved", "net": "0", "promotion_blocked": False}, "trade_status": "open"}

def _simulate_variant(trade: dict[str, Any], candles: list[dict[str, Any]], variant: dict[str, Any]) -> dict[str, Any]:
    if variant.get("__no_trade"):
        return {"trade_status": "skipped", "exit": {"status": "skipped", "reason": "no_trade", "net": "0", "promotion_blocked": False}}
    if variant.get("__time_exit"):
        return _simulate_time_exit(trade, candles[: max(2, int(safe_float(variant.get("__max_candles"), 2)))])
    if variant.get("__trailing_1r"):
        return _simulate_trailing_1r(trade, candles)
    return simulate_round_trip(trade, candles, append_order=False)

def write_heartbeat(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    now = utc_now()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "agent": "counterfactual_replay_agent",
        "pid": os.getpid(),
        "ts": now,
        "updated_at": now,
        "status": "running",
    }
    if extra:
        payload.update(extra)
    write_json_atomic(HEARTBEAT_PATH, payload)
    return payload

def interruptible_sleep(seconds: float) -> bool:
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if STOP_FILE.exists():
            return False
        time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return not STOP_FILE.exists()

def build_variants(signal: dict[str, Any], candles: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    entry = safe_float(signal.get("entry") or signal.get("price"))
    sl = safe_float(signal.get("sl") or signal.get("stop"))
    tp = safe_float(signal.get("tp") or signal.get("take_profit"))
    side = str(signal.get("side") or "").upper()
    risk = abs(entry - sl)
    variants = [{**signal, "variant": "base_original", "entry": entry, "sl": sl, "tp": tp}]
    if risk > 0:
        for sl_mult in (0.5, 1.0, 1.5):
            for tp_mult in (0.5, 1.0, 1.5):
                if side == "LONG":
                    new_sl = entry - risk * sl_mult
                    new_tp = entry + risk * tp_mult
                else:
                    new_sl = entry + risk * sl_mult
                    new_tp = entry - risk * tp_mult
                variants.append({**signal, "variant": f"sl{sl_mult:g}_tp{tp_mult:g}", "entry": entry, "sl": new_sl, "tp": new_tp})
    leverage = max(1.0, safe_float(signal.get("leverage"), 3.0))
    variants.append({**signal, "variant": "smaller_leverage", "leverage": max(1.0, leverage / 2)})
    variants.append({**signal, "variant": "higher_leverage", "leverage": min(50.0, leverage * 2)})
    variants.append({**signal, "variant": "time_exit", "__time_exit": True, "__max_candles": 2})
    variants.append({**signal, "variant": "trailing_1r", "__trailing_1r": True})
    variants.append({**signal, "variant": "no_trade", "__no_trade": True})
    candle_rows = candles or []
    if len(candle_rows) > 1:
        shifted_entry = safe_float(candle_rows[1].get("open") or candle_rows[1].get("close"), entry)
        if shifted_entry > 0:
            variants.append({**signal, "variant": "entry_plus_1", "entry": shifted_entry, "__candle_offset": 1})
    entry_index = int(safe_float(signal.get("entry_candle_index"), 0))
    if candle_rows and entry_index > 0 and entry_index - 1 < len(candle_rows):
        shifted_entry = safe_float(candle_rows[entry_index - 1].get("open") or candle_rows[entry_index - 1].get("close"), entry)
        if shifted_entry > 0:
            variants.append({**signal, "variant": "entry_minus_1", "entry": shifted_entry, "__candle_offset": entry_index - 1})
    return variants


def replay_signal(
    signal: dict[str, Any],
    candles: list[dict[str, Any]],
    append: bool = True,
    candle_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signal_id = signal_id_for(signal)
    source_signature = replay_source_signature(signal, candles, candle_source)
    signal_errors = validate_replay_signal(signal)
    if signal_errors:
        result = {
            "schema_version": SCHEMA_VERSION,
            "replay_id": replay_id(signal_id, "invalid"),
            "signal_id": signal_id,
            "source_signature": source_signature,
            "status": "unresolved",
            "reason": "invalid_replay_signal",
            "errors": signal_errors,
            "base_signal": compact_base_signal(signal),
            **replay_metadata(signal),
            "candle_source": candle_source or {},
            "created_at": utc_now(),
        }
        return finalize_replay_result(result, append)
    cutoff_errors = missing_cutoff_errors(signal)
    if cutoff_errors:
        result = {
            "schema_version": SCHEMA_VERSION,
            "replay_id": replay_id(signal_id, "missing_replay_cutoff"),
            "signal_id": signal_id,
            "source_signature": source_signature,
            "status": "unresolved",
            "reason": "missing_replay_cutoff",
            "errors": cutoff_errors,
            "base_signal": compact_base_signal(signal),
            **replay_metadata(signal),
            "candle_source": candle_source or {},
            "created_at": utc_now(),
        }
        return finalize_replay_result(result, append)
    future_errors = future_data_errors(signal, candles)
    if future_errors:
        result = {
            "schema_version": SCHEMA_VERSION,
            "replay_id": replay_id(signal_id, "future_data_violation"),
            "signal_id": signal_id,
            "source_signature": source_signature,
            "status": "unresolved",
            "reason": "future_data_violation",
            "errors": future_errors,
            "base_signal": compact_base_signal(signal),
            **replay_metadata(signal),
            "candle_source": candle_source or {},
            "created_at": utc_now(),
        }
        return finalize_replay_result(result, append)
    coverage = coverage_report(candles, minimum_candles=3)
    if not coverage["ok"]:
        result = {
            "schema_version": SCHEMA_VERSION,
            "replay_id": replay_id(signal_id, "coverage"),
            "signal_id": signal_id,
            "source_signature": source_signature,
            "status": "unresolved",
            "reason": "insufficient_candle_coverage",
            "coverage": coverage,
            "base_signal": compact_base_signal(signal),
            **replay_metadata(signal),
            "candle_source": candle_source or {},
            "created_at": utc_now(),
        }
        return finalize_replay_result(result, append)
    rows = []
    for variant in build_variants(signal, candles):
        offset = int(safe_float(variant.get("__candle_offset"), 0))
        variant_candles = candles[offset:] if offset > 0 else candles
        if len(variant_candles) < 2:
            continue
        trade = {
            "symbol": signal.get("symbol"),
            "side": signal.get("side"),
            "order_type": "market",
            "qty": variant.get("qty", signal.get("qty", "1")),
            "entry": variant.get("entry"),
            "sl": variant.get("sl"),
            "tp": variant.get("tp"),
            "leverage": variant.get("leverage", signal.get("leverage", "1")),
        }
        simulation = _simulate_variant(trade, variant_candles, variant)
        exit_row = simulation.get("exit") if isinstance(simulation.get("exit"), dict) else {}
        net = safe_float(exit_row.get("net"))
        rows.append(
            {
                "variant": variant["variant"],
                "entry": variant.get("entry"),
                "sl": variant.get("sl"),
                "tp": variant.get("tp"),
                "qty": variant.get("qty", signal.get("qty", "1")),
                "leverage": variant.get("leverage", signal.get("leverage", "1")),
                "net": net,
                "reason": exit_row.get("reason") or simulation.get("reason"),
                "status": simulation.get("trade_status") or simulation.get("status"),
                "promotion_blocked": bool(exit_row.get("promotion_blocked")),
            }
        )
    if not rows:
        result = {
            "schema_version": SCHEMA_VERSION,
            "replay_id": replay_id(signal_id, "no_variants"),
            "signal_id": signal_id,
            "source_signature": source_signature,
            "status": "unresolved",
            "reason": "no_replay_variants_simulated",
            "base_signal": compact_base_signal(signal),
            **replay_metadata(signal),
            "candle_source": candle_source or {},
            "created_at": utc_now(),
        }
        return finalize_replay_result(result, append)
    best = max(rows, key=lambda row: row["net"])
    base = next((row for row in rows if row["variant"] == "base_original"), rows[0])
    blocked = bool(signal.get("blocked") or signal.get("block_reason"))
    if blocked and best["net"] > 0:
        conclusion = "risk_gate_false_positive_candidate"
    elif blocked and best["net"] <= 0:
        conclusion = "risk_gate_true_positive_evidence"
    elif best["variant"] != base["variant"] and best["net"] > base["net"]:
        conclusion = "parameter_improvement_candidate"
    else:
        conclusion = "no_change"
    result = {
        "schema_version": SCHEMA_VERSION,
        "replay_id": replay_id(signal_id, str(best["variant"])),
        "signal_id": signal_id,
        "source_signature": source_signature,
        "status": "complete",
        "created_at": utc_now(),
        "replay_engine_version": "counterfactual_v2",
        "coverage": coverage,
        "candle_source": candle_source or {},
        "base_signal": compact_base_signal(signal),
        **replay_metadata(signal),
        "blocked_signal": blocked,
        "conclusion": conclusion,
        "best_variant": best,
        "base_variant": base,
        "base_net": base["net"],
        "best_net": best["net"],
        "net_delta": round(float(best["net"]) - float(base["net"]), 8),
        "variants": rows,
        "gate_change_allowed": False,
        "gate_change_reason": "counterfactual_is_evidence_only_until_sample_gate",
    }
    return finalize_replay_result(result, append)


def summarize_replays(rows: list[dict[str, Any]], eligible_count: int | None = None, eligible_ids: set[str] | None = None) -> dict[str, Any]:
    by_conclusion: dict[str, int] = {}
    latest_rows, correction_count = latest_replay_rows(rows)
    complete_count = 0
    unresolved_count = 0
    latest_complete_count = 0
    latest_unresolved_count = 0
    online_complete_count = 0
    online_unresolved_count = 0
    backfill_latest_count = 0
    for row in rows:
        if row.get("status") == "complete":
            complete_count += 1
        elif row.get("status") == "unresolved":
            unresolved_count += 1
        key = str(row.get("conclusion") or row.get("reason") or "unknown")
        by_conclusion[key] = by_conclusion.get(key, 0) + 1
    for row in latest_rows:
        online = bool(row.get("shadow_online", True))
        if not online:
            backfill_latest_count += 1
        if row.get("status") == "complete":
            latest_complete_count += 1
            if online:
                online_complete_count += 1
        elif row.get("status") == "unresolved":
            latest_unresolved_count += 1
            if online:
                online_unresolved_count += 1
    replay_count = len(rows)
    signal_count = len(latest_rows)
    denominator = max(int(eligible_count or 0), len(eligible_ids or set()), signal_count)
    coverage_pct = round(latest_complete_count / denominator, 4) if denominator else 0.0
    raw_row_coverage_pct = round(complete_count / replay_count, 4) if replay_count else 0.0
    latest_coverage_pct = round(latest_complete_count / signal_count, 4) if signal_count else 0.0
    readiness_denominator = max(0, denominator - backfill_latest_count)
    readiness_coverage_pct = round(online_complete_count / readiness_denominator, 4) if readiness_denominator else 0.0
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "replay_count": replay_count,
        "complete_count": complete_count,
        "unresolved_count": unresolved_count,
        "coverage_pct": coverage_pct,
        "raw_row_coverage_pct": raw_row_coverage_pct,
        "signal_count": signal_count,
        "eligible_count": denominator,
        "latest_complete_count": latest_complete_count,
        "latest_unresolved_count": latest_unresolved_count,
        "latest_coverage_pct": latest_coverage_pct,
        "online_complete_count": online_complete_count,
        "online_unresolved_count": online_unresolved_count,
        "backfill_latest_count": backfill_latest_count,
        "readiness_eligible_count": readiness_denominator,
        "readiness_coverage_pct": readiness_coverage_pct,
        "correction_count": correction_count,
        "by_conclusion": by_conclusion,
    }


def write_latest_summary(eligible_count: int | None = None, eligible_ids: set[str] | None = None) -> dict[str, Any]:
    summary = summarize_replays(read_jsonl(REPLAYS_JSONL), eligible_count=eligible_count, eligible_ids=eligible_ids)
    write_json_atomic(LATEST_JSON, summary)
    return summary

def replay_catchup_complete(summary: dict[str, Any], eligible_count: int) -> bool:
    if eligible_count <= 0:
        return False
    return int(summary.get("latest_complete_count") or 0) >= eligible_count and int(summary.get("latest_unresolved_count") or 0) == 0

def replay_catchup_for_ids(eligible_ids: set[str]) -> dict[str, Any]:
    if not eligible_ids:
        return {"complete": False, "missing_count": 0, "unresolved_count": 0, "missing_ids": [], "unresolved_ids": []}
    latest_rows, _ = latest_replay_rows(read_jsonl(REPLAYS_JSONL))
    latest_by_id = {str(row.get("signal_id") or ""): row for row in latest_rows}
    missing_ids = []
    unresolved_ids = []
    for signal_id in sorted(str(item) for item in eligible_ids):
        row = latest_by_id.get(signal_id)
        if not row:
            missing_ids.append(signal_id)
        elif row.get("status") != "complete":
            unresolved_ids.append(signal_id)
    return {
        "complete": not missing_ids and not unresolved_ids,
        "missing_count": len(missing_ids),
        "unresolved_count": len(unresolved_ids),
        "missing_ids": missing_ids[:20],
        "unresolved_ids": unresolved_ids[:20],
    }

def _paper_close_signal(row: dict[str, Any]) -> dict[str, Any]:
    position = row.get("position") if isinstance(row.get("position"), dict) else {}
    signal = {**position, **row}
    signal["signal_id"] = signal_id_for(signal)
    signal["symbol"] = signal.get("symbol") or position.get("symbol")
    signal["side"] = signal.get("side") or position.get("side")
    signal["entry"] = signal.get("entry") or position.get("entry")
    signal["sl"] = signal.get("sl") or position.get("sl")
    signal["tp"] = signal.get("tp") or position.get("tp")
    signal["qty"] = signal.get("qty") or position.get("qty") or "1"
    signal["leverage"] = signal.get("leverage") or position.get("leverage") or "1"
    signal["blocked"] = bool(signal.get("blocked") or signal.get("block_reason"))
    signal["source_available_at_max"] = signal.get("source_available_at_max") or signal.get("close_ts") or signal.get("closed_at")
    signal["trial_seq_cutoff"] = signal.get("trial_seq_cutoff") or signal["source_available_at_max"]
    signal["eligible_source"] = signal.get("eligible_source") or "paper_close"
    return signal

def _decision_signal(row: dict[str, Any]) -> dict[str, Any] | None:
    action = str(row.get("action") or "")
    candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else {}
    if action not in {"skip", "shadow_only"} or not candidate:
        return None
    risk = row.get("risk_decision") if isinstance(row.get("risk_decision"), dict) else {}
    signal = {**candidate, **{key: value for key, value in risk.items() if value not in (None, "")}}
    signal["signal_id"] = str(risk.get("risk_decision_id") or candidate.get("candidate_id") or f"decision_{row.get('decided_at')}")
    signal["blocked"] = True
    signal["block_reason"] = ";".join(str(item) for item in row.get("errors", []) if item) or action
    signal["open_ts"] = row.get("decided_at") or candidate.get("market_snapshot_ts")
    signal["close_ts"] = row.get("replay_close_ts") or candidate.get("replay_close_ts") or candidate.get("close_ts")
    signal["candle_cache_id"] = signal.get("candle_cache_id") or row.get("candle_cache_id") or candidate.get("candle_cache_id")
    signal["source_available_at_max"] = signal.get("source_available_at_max") or risk.get("source_available_at_max") or row.get("source_available_at_max") or candidate.get("source_available_at_max")
    signal["trial_seq_cutoff"] = signal.get("trial_seq_cutoff") or risk.get("trial_seq_cutoff") or row.get("trial_seq_cutoff") or candidate.get("trial_seq_cutoff")
    signal["qty"] = signal.get("qty") or "1"
    signal["leverage"] = signal.get("leverage") or candidate.get("leverage") or "1"
    signal["eligible_source"] = "paper_brain_decision"
    signal["eligible_reason"] = signal["block_reason"]
    return signal

def _bounded_tail(limit: int | None) -> int | None:
    return None if limit is None else max(1, int(limit)) * 5

def _eligible_paper_closes(limit: int | None) -> list[dict[str, Any]]:
    rows = read_jsonl(PAPER_TRADES_JSONL, limit=_bounded_tail(limit))
    closes = [row for row in rows if valid_paper_close(row)]
    return closes if limit is None else closes[-max(1, int(limit)) :]

def _eligible_blocked_decisions(limit: int | None) -> list[dict[str, Any]]:
    rows = read_jsonl(PAPER_BRAIN_HISTORY_JSONL, limit=_bounded_tail(limit))
    signals = []
    for row in rows:
        signal = _decision_signal(row)
        if signal:
            signals.append(signal)
    return signals if limit is None else signals[-max(1, int(limit)) :]

def _candidate_census_signal(candidate: dict[str, Any], payload: dict[str, Any], index: int) -> dict[str, Any]:
    signal = dict(candidate)
    signal["signal_id"] = str(candidate.get("signal_id") or candidate.get("candidate_id") or f"candidate_census_{payload.get('updated_at')}_{index}")
    signal["symbol"] = signal.get("symbol") or candidate.get("instrument")
    signal["side"] = signal.get("side")
    signal["entry"] = signal.get("entry") or signal.get("price")
    signal["sl"] = signal.get("sl") or signal.get("stop")
    signal["tp"] = signal.get("tp") or signal.get("take_profit")
    signal["qty"] = signal.get("qty") or "1"
    signal["leverage"] = signal.get("leverage") or "1"
    signal["blocked"] = True
    signal["block_reason"] = signal.get("block_reason") or signal.get("skip_reason") or "candidate_census_not_selected"
    signal["eligible_source"] = "paper_candidate_census"
    signal["eligible_reason"] = signal["block_reason"]
    signal["first_computed_at"] = payload.get("updated_at")
    signal["open_ts"] = signal.get("open_ts") or signal.get("market_snapshot_ts") or payload.get("market_ts") or payload.get("updated_at")
    signal["candle_cache_id"] = signal.get("candle_cache_id") or payload.get("candle_cache_id")
    signal["source_available_at_max"] = signal.get("source_available_at_max") or payload.get("source_available_at_max")
    signal["trial_seq_cutoff"] = signal.get("trial_seq_cutoff") or payload.get("trial_seq_cutoff")
    return signal

def _eligible_candidate_census(limit: int | None) -> list[dict[str, Any]]:
    rows = read_jsonl(PAPER_CANDIDATE_HISTORY_JSONL, limit=_bounded_tail(limit))
    signals: list[dict[str, Any]] = []
    for payload in rows:
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        if candidates:
            for index, candidate in enumerate(candidates):
                if isinstance(candidate, dict):
                    signals.append(_candidate_census_signal(candidate, payload, index))
        else:
            signals.append(
                {
                    "signal_id": f"candidate_gap_{payload.get('updated_at') or payload.get('market_ts') or len(signals)}",
                    "blocked": True,
                    "block_reason": payload.get("reason") or "candidate_census_empty",
                    "eligible_source": "paper_candidate_census",
                    "eligible_reason": payload.get("reason") or "candidate_census_empty",
                    "first_computed_at": payload.get("updated_at"),
                    "open_ts": payload.get("market_ts") or payload.get("updated_at"),
                    "qty": "1",
                    "leverage": "1",
                }
            )
    return signals if limit is None else signals[-max(1, int(limit)) :]

def dedupe_signals(signals: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for signal in signals:
        by_id[signal_id_for(signal)] = signal
    values = list(by_id.values())
    return values if limit is None else values[-max(1, int(limit)) :]

def sanitize_replay_signal(signal: dict[str, Any]) -> dict[str, Any]:
    scanned = sanitize_and_detect(signal)
    sanitized = scanned.get("sanitized") if isinstance(scanned.get("sanitized"), dict) else {}
    sanitized["can_place_live_orders"] = False
    sanitized["live_permission"] = False
    if scanned.get("live_intent"):
        reasons = [str(sanitized.get("block_reason") or "live_intent_blocked_phase_a"), "live_intent_blocked_phase_a"]
        sanitized["blocked"] = True
        sanitized["block_reason"] = ";".join(sorted(set(reason for reason in reasons if reason)))
        sanitized["live_intent_paths"] = scanned.get("live_paths") or []
    return sanitized

def eligible_signals(limit: int | None = None) -> list[dict[str, Any]]:
    close_signals = [_paper_close_signal(row) for row in _eligible_paper_closes(limit)]
    blocked_signals = _eligible_blocked_decisions(limit)
    census_signals = _eligible_candidate_census(limit)
    return dedupe_signals([sanitize_replay_signal(signal) for signal in [*census_signals, *blocked_signals, *close_signals]], limit)

def run_once(limit: int = 100) -> dict[str, Any]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    already = replayed_complete_signal_ids()
    eligible = eligible_signals(limit)
    eligible_ids = {signal_id_for(signal) for signal in eligible}
    all_eligible_ids = {signal_id_for(signal) for signal in eligible_signals(None)}
    results: list[dict[str, Any]] = []

    for signal in eligible:
        signal_id = signal_id_for(signal)
        try:
            candles, source = candles_for_signal(signal)
            source_signature = replay_source_signature(signal, candles, source)
            prior = latest_replay_for_signal(signal_id)
            if signal_id in already and prior and prior.get("source_signature") == source_signature:
                continue
            result = replay_signal(signal, candles, append=True, candle_source=source)
        except Exception as exc:
            result = {
                "schema_version": SCHEMA_VERSION,
                "replay_id": replay_id(signal_id, "error"),
                "signal_id": signal_id,
                "source_signature": replay_source_signature(signal),
                "status": "unresolved",
                "reason": "replay_exception",
                "error": str(exc)[:300],
                "created_at": utc_now(),
            }
            result = finalize_replay_result(result, append=True)
        results.append(result)
        if result.get("status") == "complete":
            already.add(signal_id)

    summary = write_latest_summary(eligible_count=len(all_eligible_ids), eligible_ids=all_eligible_ids)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "eligible_scanned": len(eligible),
        "eligible_total": len(all_eligible_ids),
        "new_replays": len(results),
        "recent_results": results[-10:],
        "summary": summary,
    }
    catchup = replay_catchup_for_ids(all_eligible_ids)
    payload["catchup"] = catchup
    if replay_catchup_complete(summary, len(all_eligible_ids)) and catchup["complete"]:
        try:
            payload["host_runtime_replay_ack"] = acknowledge_sleep_resume_replay(detail={"eligible_scanned": len(eligible), "eligible_total": len(all_eligible_ids), "new_replays": len(results), "latest_complete_count": summary.get("latest_complete_count"), "catchup": catchup})
        except Exception as exc:
            payload["host_runtime_replay_ack"] = {"ok": False, "reason": "ack_failed", "error": str(exc)[:160]}
    else:
        payload["host_runtime_replay_ack"] = {"ok": False, "reason": "replay_catchup_incomplete", "eligible_scanned": len(eligible), "eligible_total": len(all_eligible_ids), "latest_complete_count": summary.get("latest_complete_count"), "latest_unresolved_count": summary.get("latest_unresolved_count"), "catchup": catchup}
    write_heartbeat({"last_run": payload})
    return payload

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay closed paper trades against alternate entry/SL/TP assumptions")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--status", action="store_true", help="print latest summary and exit")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    return parser.parse_args(list(argv) if argv is not None else None)

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        print(json.dumps(read_json(LATEST_JSON, default={"status": "no_counterfactual_summary"}), ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    write_heartbeat({"status": "starting"})

    if args.once:
        print(json.dumps(run_once(limit=args.limit), ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    while not STOP_FILE.exists():
        run_once(limit=args.limit)
        if not interruptible_sleep(args.interval_seconds):
            break
    write_heartbeat({"status": "stopped"})
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
