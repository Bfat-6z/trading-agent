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
from market_data_lake import coverage_report, load_candles, select_window
from market_learner import valid_paper_close
from paper_execution_simulator import simulate_round_trip
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PAPER_TRADES_JSONL = MEMORY_DIR / "paper_trades.jsonl"
PAPER_BRAIN_HISTORY_JSONL = MEMORY_DIR / "paper_trading_brain_history.jsonl"
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


def signal_id_for(row: dict[str, Any]) -> str:
    return str(row.get("signal_id") or row.get("trade_id") or row.get("shadow_id") or row.get("risk_decision_id") or "signal")

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
    variants = []
    for sl_mult in (0.5, 1.0, 1.5):
        for tp_mult in (0.5, 1.0, 1.5):
            if side == "LONG":
                new_sl = entry - risk * sl_mult
                new_tp = entry + risk * tp_mult
            else:
                new_sl = entry + risk * sl_mult
                new_tp = entry - risk * tp_mult
            variants.append({**signal, "variant": f"sl{sl_mult:g}_tp{tp_mult:g}", "entry": entry, "sl": new_sl, "tp": new_tp})
    variants.append({**signal, "variant": "smaller_leverage", "leverage": max(1.0, safe_float(signal.get("leverage"), 3.0) / 2)})
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
    signal_errors = validate_replay_signal(signal)
    if signal_errors:
        result = {
            "schema_version": SCHEMA_VERSION,
            "replay_id": replay_id(signal_id, "invalid"),
            "signal_id": signal_id,
            "status": "unresolved",
            "reason": "invalid_replay_signal",
            "errors": signal_errors,
            "candle_source": candle_source or {},
            "created_at": utc_now(),
        }
        if append:
            append_jsonl_once(REPLAYS_JSONL, result, "replay_id")
            write_latest_summary()
        return result
    coverage = coverage_report(candles, minimum_candles=3)
    if not coverage["ok"]:
        result = {
            "schema_version": SCHEMA_VERSION,
            "replay_id": replay_id(signal_id, "coverage"),
            "signal_id": signal_id,
            "status": "unresolved",
            "reason": "insufficient_candle_coverage",
            "coverage": coverage,
            "candle_source": candle_source or {},
            "created_at": utc_now(),
        }
        if append:
            append_jsonl_once(REPLAYS_JSONL, result, "replay_id")
            write_latest_summary()
        return result
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
            "qty": signal.get("qty", "1"),
            "entry": variant.get("entry"),
            "sl": variant.get("sl"),
            "tp": variant.get("tp"),
            "leverage": variant.get("leverage", signal.get("leverage", "1")),
        }
        simulation = simulate_round_trip(trade, variant_candles, append_order=False)
        exit_row = simulation.get("exit") if isinstance(simulation.get("exit"), dict) else {}
        net = safe_float(exit_row.get("net"))
        rows.append(
            {
                "variant": variant["variant"],
                "net": net,
                "reason": exit_row.get("reason") or simulation.get("reason"),
                "status": simulation.get("trade_status") or simulation.get("status"),
                "promotion_blocked": bool(exit_row.get("promotion_blocked")),
            }
        )
    best = max(rows, key=lambda row: row["net"])
    base = next((row for row in rows if row["variant"] == "sl1_tp1"), rows[0])
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
        "status": "complete",
        "created_at": utc_now(),
        "coverage": coverage,
        "candle_source": candle_source or {},
        "blocked_signal": blocked,
        "conclusion": conclusion,
        "best_variant": best,
        "base_variant": base,
        "variants": rows,
        "gate_change_allowed": False,
        "gate_change_reason": "counterfactual_is_evidence_only_until_sample_gate",
    }
    if append:
        append_jsonl_once(REPLAYS_JSONL, result, "replay_id")
        write_latest_summary()
    return result


def summarize_replays(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_conclusion: dict[str, int] = {}
    complete_count = 0
    unresolved_count = 0
    for row in rows:
        if row.get("status") == "complete":
            complete_count += 1
        elif row.get("status") == "unresolved":
            unresolved_count += 1
        key = str(row.get("conclusion") or row.get("reason") or "unknown")
        by_conclusion[key] = by_conclusion.get(key, 0) + 1
    replay_count = len(rows)
    coverage_pct = round(complete_count / replay_count, 4) if replay_count else 0.0
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "replay_count": replay_count,
        "complete_count": complete_count,
        "unresolved_count": unresolved_count,
        "coverage_pct": coverage_pct,
        "by_conclusion": by_conclusion,
    }


def write_latest_summary() -> dict[str, Any]:
    summary = summarize_replays(read_jsonl(REPLAYS_JSONL))
    write_json_atomic(LATEST_JSON, summary)
    return summary

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
    signal["close_ts"] = row.get("decided_at") or candidate.get("market_snapshot_ts")
    signal["qty"] = signal.get("qty") or "1"
    signal["leverage"] = signal.get("leverage") or candidate.get("leverage") or "1"
    return signal

def _eligible_paper_closes(limit: int) -> list[dict[str, Any]]:
    rows = read_jsonl(PAPER_TRADES_JSONL, limit=max(1, limit) * 5)
    closes = [row for row in rows if valid_paper_close(row)]
    return closes[-max(1, limit) :]

def _eligible_blocked_decisions(limit: int) -> list[dict[str, Any]]:
    rows = read_jsonl(PAPER_BRAIN_HISTORY_JSONL, limit=max(1, limit) * 5)
    signals = []
    for row in rows:
        signal = _decision_signal(row)
        if signal:
            signals.append(signal)
    return signals[-max(1, limit) :]

def run_once(limit: int = 100) -> dict[str, Any]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    already = replayed_complete_signal_ids()
    close_signals = [_paper_close_signal(row) for row in _eligible_paper_closes(limit)]
    blocked_signals = _eligible_blocked_decisions(limit)
    eligible = [*close_signals, *blocked_signals][-max(1, limit) :]
    results: list[dict[str, Any]] = []

    for signal in eligible:
        signal_id = signal_id_for(signal)
        if signal_id in already:
            continue
        try:
            candles, source = candles_for_signal(signal)
            result = replay_signal(signal, candles, append=True, candle_source=source)
        except Exception as exc:
            result = {
                "schema_version": SCHEMA_VERSION,
                "replay_id": replay_id(signal_id, "error"),
                "signal_id": signal_id,
                "status": "unresolved",
                "reason": "replay_exception",
                "error": str(exc)[:300],
                "created_at": utc_now(),
            }
            append_jsonl_once(REPLAYS_JSONL, result, "replay_id")
        results.append(result)
        if result.get("status") == "complete":
            already.add(signal_id)

    summary = write_latest_summary()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "eligible_scanned": len(eligible),
        "new_replays": len(results),
        "recent_results": results[-10:],
        "summary": summary,
    }
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
