"""Aligned microstructure and external-flow feature bundle for paper decisions."""
from __future__ import annotations

import argparse
import hashlib
import os
import time
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, write_json_atomic
from instrument_registry import can_trade_paper, load_registry, normalize_symbol, registry_version
from timebase import parse_utc, seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
ORDERBOOK_SOURCE = STATE_DIR / "orderbook_microstructure_latest.json"
DERIVATIVES_SOURCE = STATE_DIR / "derivatives_latest.json"
LIQUIDATIONS_SOURCE = STATE_DIR / "liquidations_latest.json"
WHALE_FLOW_SOURCE = MEMORY_DIR / "whale_flow_latest.json"
NEWS_SOURCE = MEMORY_DIR / "news_latest.json"
LATEST_PATH = MEMORY_DIR / "microstructure_flow_latest.json"
HISTORY_PATH = MEMORY_DIR / "microstructure_flow_history.jsonl"
HEARTBEAT_PATH = STATE_DIR / "microstructure_flow_factory_heartbeat.json"
PID_FILE = STATE_DIR / "microstructure_flow_factory.pid"
STOP_FILE = STATE_DIR / "STOP_MICROSTRUCTURE_FLOW_FACTORY"

MAX_AGE_SECONDS = {
    "orderbook": 30,
    "derivatives": 600,
    "liquidations": 300,
    "whale_flow": 900,
    "news": 3600,
}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def canonical_instrument_id(symbol: str, instrument: dict[str, Any] | None = None) -> str:
    row = instrument or {}
    contract_type = str(row.get("contract_type") or row.get("contractType") or "PERPETUAL").upper()
    return f"binance_usdm:{normalize_symbol(symbol)}:{contract_type}"


def instrument_snapshot_id(symbol: str, registry: dict[str, Any]) -> str:
    material = f"{registry_version(registry)}:{normalize_symbol(symbol)}"
    return "instrument_snapshot_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:18]


def source_age(row: dict[str, Any], now: str) -> float | None:
    return seconds_between(row.get("updated_at") or row.get("ts") or row.get("observed_at"), now)


def source_state(name: str, row: dict[str, Any], now: str, max_age: int) -> dict[str, Any]:
    if not row:
        return {"name": name, "status": "missing", "usable": False, "errors": [f"{name}_missing"], "warnings": []}
    age = source_age(row, now)
    errors: list[str] = []
    warnings: list[str] = []
    if age is None:
        errors.append(f"{name}_missing_timestamp")
    elif age > max_age:
        errors.append(f"{name}_stale")
    if row.get("paper_entry_allowed") is False:
        errors.extend(str(item) for item in (row.get("errors") or []))
        warnings.extend(str(item) for item in (row.get("warnings") or []))
    if row.get("usable_for_features") is False:
        errors.append(f"{name}_coverage_unknown")
    return {"name": name, "status": "ok" if not errors else "degraded", "usable": not errors, "age_seconds": age, "errors": sorted(set(errors)), "warnings": sorted(set(warnings))}


def social_row(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
    by_symbol = payload.get("by_symbol") if isinstance(payload.get("by_symbol"), dict) else {}
    row = by_symbol.get(normalize_symbol(symbol)) or by_symbol.get("MARKET") or {}
    if not isinstance(row, dict):
        row = {}
    return {
        "pressure_side": row.get("pressure_side") or "NEUTRAL",
        "pressure_score": safe_float(row.get("pressure_score")),
        "source_quorum_passed": bool(row.get("source_quorum_passed")),
        "market_confirmed": bool(row.get("market_confirmed")),
        "allowed_effect": row.get("allowed_effect") or payload.get("allowed_effect") or "shadow_only",
        "event_count": int(safe_float(row.get("event_count"))),
        "source_trust": "quorum" if row.get("source_quorum_passed") and row.get("market_confirmed") else "shadow_only",
    }


def news_row(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
    impacts = payload.get("symbol_impacts") if isinstance(payload.get("symbol_impacts"), dict) else {}
    base = normalize_symbol(symbol).replace("USDT", "")
    impact = impacts.get(base) or impacts.get(normalize_symbol(symbol)) or {}
    if not isinstance(impact, dict):
        impact = {}
    return {
        "macro_risk_score": safe_float(payload.get("macro_risk_score")),
        "regulatory_risk": safe_float(payload.get("crypto_regulatory_risk")),
        "headline_chaos": safe_float(payload.get("headline_chaos")),
        "symbol_risk": safe_float(impact.get("risk")),
        "symbol_bullish": safe_float(impact.get("bullish")),
        "symbol_bearish": safe_float(impact.get("bearish")),
        "confidence": safe_float(impact.get("confidence"), safe_float(payload.get("source_quality_score"))),
    }


def build_symbol_bundle(symbol: str, *, decision_cutoff: str | None = None, market_price: Any = None) -> dict[str, Any]:
    now = utc_now()
    cutoff = decision_cutoff or now
    registry = load_registry()
    instrument_check = can_trade_paper(symbol, registry=registry, allow_missing=False)
    instrument = instrument_check.get("instrument") if isinstance(instrument_check.get("instrument"), dict) else {}
    orderbook = read_json(ORDERBOOK_SOURCE, default={})
    derivatives = read_json(DERIVATIVES_SOURCE, default={})
    liquidations = read_json(LIQUIDATIONS_SOURCE, default={})
    whale_flow = read_json(WHALE_FLOW_SOURCE, default={})
    news = read_json(NEWS_SOURCE, default={})
    states = {
        "orderbook": source_state("orderbook", orderbook if normalize_symbol(orderbook.get("symbol")) == normalize_symbol(symbol) else {}, now, MAX_AGE_SECONDS["orderbook"]),
        "derivatives": source_state("derivatives", derivatives if normalize_symbol(derivatives.get("symbol")) == normalize_symbol(symbol) else {}, now, MAX_AGE_SECONDS["derivatives"]),
        "liquidations": source_state("liquidations", liquidations if normalize_symbol(liquidations.get("symbol")) == normalize_symbol(symbol) else {}, now, MAX_AGE_SECONDS["liquidations"]),
        "whale_flow": source_state("whale_flow", whale_flow, now, MAX_AGE_SECONDS["whale_flow"]),
        "news": source_state("news", news, now, MAX_AGE_SECONDS["news"]),
    }
    errors = list(instrument_check.get("errors") or [])
    warnings = list(instrument_check.get("warnings") or [])
    for state in states.values():
        warnings.extend(state.get("warnings") or [])
    if states["orderbook"]["errors"]:
        warnings.extend(states["orderbook"]["errors"])
    if states["derivatives"]["errors"]:
        warnings.extend(states["derivatives"]["errors"])
    if states["liquidations"]["errors"]:
        warnings.extend(states["liquidations"]["errors"])
    social = social_row(symbol, whale_flow)
    news_features = news_row(symbol, news)
    spread_bps = safe_float(orderbook.get("spread_bps"), 999999.0)
    action = "normal"
    if errors or "crossed_orderbook" in warnings or spread_bps > 50:
        action = "skip"
    elif warnings or not states["orderbook"]["usable"] or not states["derivatives"]["usable"] or not states["liquidations"]["usable"]:
        action = "size_cap"
    if news_features["headline_chaos"] >= 0.6 or news_features["macro_risk_score"] >= 0.75:
        action = "skip" if news_features["headline_chaos"] >= 0.85 else "size_cap"
    confidence_parts = [
        1.0 if instrument_check.get("can_trade_paper") else 0.0,
        safe_float(orderbook.get("confidence")),
        safe_float(derivatives.get("confidence")),
        1.0 if liquidations.get("usable_for_features", True) else 0.0,
        0.7 if social["source_quorum_passed"] and social["market_confirmed"] else 0.35,
        max(0.0, min(1.0, news_features["confidence"])),
    ]
    feature_confidence = round(sum(confidence_parts) / len(confidence_parts), 4)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now,
        "decision_cutoff": cutoff,
        "symbol": normalize_symbol(symbol),
        "canonical_instrument_id": canonical_instrument_id(symbol, instrument),
        "instrument_snapshot_id": instrument_snapshot_id(symbol, registry),
        "instrument": instrument,
        "price_basis": {
            "fills": "BOOK_MID/LAST+slippage",
            "liquidation": "MARK",
            "funding_notional": "MARK@boundary",
            "premium": "MARK-INDEX",
            "candles": "CANDLE_CLOSE",
        },
        "components": {
            "orderbook": {
                "price_basis": "BOOK_MID",
                "spread_bps": spread_bps,
                "imbalance": safe_float(orderbook.get("imbalance")),
                "bid_depth_10": safe_float(orderbook.get("bid_depth_10")),
                "ask_depth_10": safe_float(orderbook.get("ask_depth_10")),
                "depth_level": orderbook.get("depth_level"),
            },
            "derivatives": {
                "price_basis": "MARK",
                "predicted_funding_rate": safe_float(derivatives.get("predicted_funding_rate"), safe_float(derivatives.get("funding_rate"))),
                "open_interest_delta": safe_float(derivatives.get("open_interest_delta")),
                "open_interest_usd_notional": derivatives.get("open_interest_usd_notional"),
                "payer_side": derivatives.get("payer_side"),
            },
            "liquidations": {
                "price_basis": "MARK",
                "total_notional": safe_float(liquidations.get("total_notional")),
                "imbalance": safe_float(liquidations.get("imbalance")),
                "near_price_event_count": int(safe_float(liquidations.get("near_price_event_count"))),
                "coverage": liquidations.get("coverage") or "unknown",
            },
            "social": social,
            "news": news_features,
        },
        "source_states": states,
        "decision_data_capability_mask": {
            "feature_family": "microstructure_flow_v1",
            "required": ["instrument"],
            "optional": ["orderbook", "derivatives", "liquidations", "whale_flow", "news"],
            "missing": [name for name, state in states.items() if state.get("status") == "missing"],
            "stale": [name for name, state in states.items() if any(str(error).endswith("_stale") for error in state.get("errors", []))],
            "source_confidence": feature_confidence,
            "action": action,
            "errors": sorted(set(errors)),
            "warnings": sorted(set(warnings)),
        },
        "feature_confidence": feature_confidence,
        "usable_for_paper": action != "skip",
        "by_symbol": {
            normalize_symbol(symbol): {
                **social,
                "symbol": normalize_symbol(symbol),
                "market_confirmed": bool(social["market_confirmed"] or states["orderbook"]["usable"]),
            }
        },
        "can_place_live_orders": False,
    }
    return payload


def run_once(symbols: Iterable[str] | None = None) -> dict[str, Any]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    symbols = list(symbols or [])
    if not symbols:
        market = read_json(STATE_DIR / "market_updates_latest.json", default={})
        for row in market.get("hot", []) if isinstance(market.get("hot"), list) else []:
            if isinstance(row, dict) and row.get("symbol"):
                symbols.append(str(row.get("symbol")))
    symbols = list(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols if symbol))[:50]
    bundles = [build_symbol_bundle(symbol) for symbol in symbols]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "status": "ok" if bundles else "waiting_for_symbols",
        "symbol_count": len(bundles),
        "symbols": {row["symbol"]: row for row in bundles},
        "by_symbol": {symbol: row.get("by_symbol", {}).get(symbol, {}) for symbol, row in ((item["symbol"], item) for item in bundles)},
        "can_place_live_orders": False,
    }
    write_json_atomic(LATEST_PATH, payload)
    append_jsonl(HISTORY_PATH, payload)
    write_heartbeat(payload["status"], {"symbol_count": len(bundles)})
    return payload


def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> None:
    write_json_atomic(HEARTBEAT_PATH, {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})})


def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build aligned microstructure/flow feature bundle")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--symbol", action="append", default=[])
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        row = run_once(args.symbol)
        print(f"microstructure_flow_factory status={row.get('status')} symbols={row.get('symbol_count')}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
