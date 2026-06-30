"""Canonical closed-candle service for chart intelligence.

This module normalizes futures candles into a strict chart contract. It is
paper-only plumbing: no order placement, no live execution authority.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_data_contracts import CHART_MODEL_VERSION, SCHEMA_VERSION, validate_chart_contract
from atomic_state import append_jsonl, canonical_json, read_jsonl, write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
CHART_CANDLE_DIR = STATE_DIR / "chart" / "candles"

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1D": 86400}
PRICE_BASIS = {"last_trade", "mark", "index"}
SOURCE_POLICY_VERSION = "chart_source_policy_v1"


def timeframe_seconds(timeframe: str) -> int:
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported_timeframe:{timeframe}")
    return TIMEFRAME_SECONDS[timeframe]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def dt_from_ms(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc)


def stable_digest(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]


def source_policy(
    provider: str,
    price_basis: str,
    *,
    native_timeframe: bool = True,
    strict_finality: bool = True,
    fallback_mode: str = "degrade",
    listing_status: str = "TRADING",
    server_time_drift_seconds: float | None = None,
) -> dict[str, Any]:
    return {
        "policy_version": SOURCE_POLICY_VERSION,
        "provider": provider,
        "price_basis": price_basis,
        "native_timeframe": bool(native_timeframe),
        "strict_finality": bool(strict_finality),
        "fallback_mode": fallback_mode,
        "listing_status": listing_status,
        "server_time_drift_seconds": server_time_drift_seconds,
    }


def cache_path(symbol: str, timeframe: str) -> Path:
    return CHART_CANDLE_DIR / symbol.upper() / f"{timeframe}.jsonl"


def latest_path(symbol: str, timeframe: str) -> Path:
    return CHART_CANDLE_DIR / symbol.upper() / f"{timeframe}.latest.json"


def build_cutoff_proof(bars: list[dict[str, Any]], decision_cutoff: str) -> dict[str, Any]:
    cutoff_dt = parse_utc(decision_cutoff)
    errors: list[str] = []
    max_seen = None
    checked: list[str] = []
    if not cutoff_dt:
        return {"ok": False, "decision_cutoff": decision_cutoff, "errors": ["invalid_decision_cutoff"], "checked_input_ids": []}
    for idx, bar in enumerate(bars):
        input_id = f"bar:{idx}:{bar.get('open_time')}"
        checked.append(input_id)
        row_max = None
        for field in ("available_at", "known_at", "ingested_at", "finalized_at"):
            parsed = parse_utc(bar.get(field))
            if not parsed:
                errors.append(f"invalid_{field}:{input_id}")
                continue
            row_max = parsed if row_max is None or parsed > row_max else row_max
            if parsed > cutoff_dt:
                errors.append(f"{field}_after_cutoff:{input_id}")
        if row_max:
            max_seen = row_max if max_seen is None or row_max > max_seen else max_seen
    return {
        "ok": not errors,
        "decision_cutoff": iso(cutoff_dt),
        "max_input_time": iso(max_seen) if max_seen else None,
        "checked_input_ids": checked,
        "errors": sorted(set(errors)),
    }


def normalize_binance_kline(
    row: list[Any] | tuple[Any, ...],
    timeframe: str,
    *,
    server_time: str | None,
    ingested_at: str,
    price_basis: str,
    native_timeframe: bool,
    finality_latency_seconds: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    try:
        open_dt = dt_from_ms(row[0])
        close_dt = open_dt + timedelta(seconds=timeframe_seconds(timeframe))
        server_dt = parse_utc(server_time)
        is_final = bool(server_dt and server_dt >= close_dt)
        available_dt = close_dt + timedelta(seconds=max(0, int(finality_latency_seconds)))
        bar = {
            "open_time": iso(open_dt),
            "close_time": iso(close_dt),
            "open": str(row[1]),
            "high": str(row[2]),
            "low": str(row[3]),
            "close": str(row[4]),
            "volume": str(row[5]) if len(row) > 5 else "0",
            "quote_volume": str(row[7]) if len(row) > 7 else None,
            "trade_count": int(row[8]) if len(row) > 8 and str(row[8]).isdigit() else None,
            "is_final": is_final,
            "available_at": iso(available_dt),
            "known_at": iso(available_dt),
            "ingested_at": ingested_at,
            "finalized_at": iso(close_dt),
            "price_basis": price_basis,
            "native_timeframe": bool(native_timeframe),
        }
        return bar, errors
    except Exception as exc:
        return None, [f"malformed_kline:{str(exc)[:80]}"]


def normalize_dict_candle(
    row: dict[str, Any],
    *,
    price_basis: str,
    native_timeframe: bool,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    required = ("open_time", "close_time", "open", "high", "low", "close", "is_final", "available_at", "known_at", "ingested_at", "finalized_at")
    missing = [field for field in required if row.get(field) in (None, "")]
    if missing:
        return None, ["missing_finality_metadata:" + ",".join(missing)]
    open_dt = parse_utc(row.get("open_time"))
    close_dt = parse_utc(row.get("close_time"))
    if not open_dt or not close_dt:
        errors.append("invalid_open_or_close_time")
    bar = {
        "open_time": iso(open_dt) if open_dt else str(row.get("open_time")),
        "close_time": iso(close_dt) if close_dt else str(row.get("close_time")),
        "open": str(row.get("open")),
        "high": str(row.get("high")),
        "low": str(row.get("low")),
        "close": str(row.get("close")),
        "volume": str(row.get("volume", "0")),
        "is_final": bool(row.get("is_final")),
        "available_at": iso(parse_utc(row.get("available_at"))) if parse_utc(row.get("available_at")) else str(row.get("available_at")),
        "known_at": iso(parse_utc(row.get("known_at"))) if parse_utc(row.get("known_at")) else str(row.get("known_at")),
        "ingested_at": iso(parse_utc(row.get("ingested_at"))) if parse_utc(row.get("ingested_at")) else str(row.get("ingested_at")),
        "finalized_at": iso(parse_utc(row.get("finalized_at"))) if parse_utc(row.get("finalized_at")) else str(row.get("finalized_at")),
        "price_basis": price_basis,
        "native_timeframe": bool(native_timeframe),
    }
    return bar, errors


def sequence_errors(bars: list[dict[str, Any]], timeframe: str, original_open_times: list[str]) -> list[str]:
    errors: list[str] = []
    if original_open_times != sorted(original_open_times):
        errors.append("out_of_order_candles")
    seen: set[str] = set()
    for open_time in original_open_times:
        if open_time in seen:
            errors.append(f"duplicate_candle:{open_time}")
        seen.add(open_time)
    sorted_bars = sorted(bars, key=lambda item: item.get("open_time") or "")
    expected_delta = timeframe_seconds(timeframe)
    for prev, current in zip(sorted_bars, sorted_bars[1:]):
        prev_dt = parse_utc(prev.get("open_time"))
        current_dt = parse_utc(current.get("open_time"))
        if not prev_dt or not current_dt:
            continue
        delta = int((current_dt - prev_dt).total_seconds())
        if delta != expected_delta:
            errors.append(f"candle_gap:{prev.get('open_time')}->{current.get('open_time')}")
    return sorted(set(errors))


def build_chart_candle_batch(
    symbol: str,
    timeframe: str,
    raw_rows: list[Any],
    *,
    decision_cutoff: str | None = None,
    source_id: str = "binance_usdm_klines",
    provider: str = "binance_usdm",
    exchange: str = "BINANCE_USDM",
    price_basis: str = "last_trade",
    server_time: str | None = None,
    ingested_at: str | None = None,
    input_event_ids: list[str] | None = None,
    source_manifest_ids: list[str] | None = None,
    native_timeframe: bool = True,
    strict_finality: bool = True,
    closed_only: bool = True,
    finality_latency_seconds: int = 1,
    listing_status: str = "TRADING",
    min_candles: int = 1,
) -> dict[str, Any]:
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported_timeframe:{timeframe}")
    if price_basis not in PRICE_BASIS:
        raise ValueError(f"unsupported_price_basis:{price_basis}")
    observed_at = ingested_at or utc_now()
    cutoff = decision_cutoff or observed_at
    server_dt = parse_utc(server_time)
    observed_dt = parse_utc(observed_at)
    drift = (observed_dt - server_dt).total_seconds() if server_dt and observed_dt else None
    errors: list[str] = []
    warnings: list[str] = []
    bars: list[dict[str, Any]] = []
    excluded_forming = 0
    original_open_times: list[str] = []
    for raw in raw_rows:
        if isinstance(raw, (list, tuple)):
            bar, row_errors = normalize_binance_kline(
                raw,
                timeframe,
                server_time=server_time,
                ingested_at=observed_at,
                price_basis=price_basis,
                native_timeframe=native_timeframe,
                finality_latency_seconds=finality_latency_seconds,
            )
        elif isinstance(raw, dict):
            bar, row_errors = normalize_dict_candle(raw, price_basis=price_basis, native_timeframe=native_timeframe)
        else:
            bar, row_errors = None, ["invalid_raw_candle_type"]
        errors.extend(row_errors)
        if not bar:
            continue
        original_open_times.append(str(bar.get("open_time")))
        if closed_only and bar.get("is_final") is not True:
            excluded_forming += 1
            continue
        bars.append(bar)
    bars.sort(key=lambda item: item.get("open_time") or "")
    errors.extend(sequence_errors(bars, timeframe, original_open_times))
    if excluded_forming:
        warnings.append(f"excluded_forming_candles:{excluded_forming}")
    if listing_status.upper() != "TRADING":
        errors.append(f"symbol_not_trading:{listing_status}")
    if len(bars) < min_candles:
        errors.append("insufficient_candle_history")
    cutoff_proof = build_cutoff_proof(bars, cutoff)
    if not cutoff_proof.get("ok"):
        errors.extend(cutoff_proof.get("errors") or [])
    degradation_state = "ok"
    if errors:
        degradation_state = "quarantined"
    elif warnings:
        degradation_state = "partial"
    material = {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "source_id": source_id,
        "price_basis": price_basis,
        "bars": bars,
        "input_event_ids": sorted(input_event_ids or []),
        "source_manifest_ids": sorted(source_manifest_ids or []),
    }
    batch_id = stable_digest("chart_candles", material)
    clean_input_event_ids = input_event_ids or [stable_digest("chart_input", material)]
    provenance_id = stable_digest(
        "chart_provenance",
        {
            "source_id": source_id,
            "source_manifest_ids": source_manifest_ids or [],
            "provider": provider,
            "exchange": exchange,
            "price_basis": price_basis,
            "native_timeframe": native_timeframe,
            "listing_status": listing_status,
        },
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartCandleBatch.v1",
        "batch_id": batch_id,
        "symbol": symbol.upper(),
        "exchange": exchange,
        "provider": provider,
        "timeframe": timeframe,
        "price_basis": price_basis,
        "native_timeframe": bool(native_timeframe),
        "closed_only": bool(closed_only),
        "source_policy": source_policy(
            provider,
            price_basis,
            native_timeframe=native_timeframe,
            strict_finality=strict_finality,
            listing_status=listing_status,
            server_time_drift_seconds=round(drift, 6) if drift is not None else None,
        ),
        "source_ids": [source_id],
        "provenance_id": provenance_id,
        "input_event_ids": clean_input_event_ids,
        "source_manifest_ids": source_manifest_ids or [],
        "decision_cutoff": cutoff,
        "cutoff_proof": cutoff_proof,
        "degradation_state": degradation_state,
        "capability_mask": {
            "action": "normal" if degradation_state == "ok" else "skip",
            "value_errors": sorted(set(errors)),
            "warnings": sorted(set(warnings)),
            "source_confidence": 1.0 if degradation_state == "ok" else 0.0,
        },
        "gap_report": {
            "ok": not any(error.startswith(("candle_gap", "duplicate_candle", "out_of_order")) for error in errors),
            "errors": sorted(error for error in set(errors) if error.startswith(("candle_gap", "duplicate_candle", "out_of_order"))),
        },
        "created_at": observed_at,
        "bars": bars,
        "can_place_live_orders": False,
        "live_permission": False,
    }
    validation = validate_chart_contract("ChartCandleBatch.v1", payload)
    if not validation.ok and degradation_state != "quarantined":
        payload["degradation_state"] = "quarantined"
        payload["capability_mask"]["action"] = "skip"
        payload["capability_mask"]["value_errors"] = sorted(set(payload["capability_mask"]["value_errors"] + validation.errors))
    return payload


def provider_error_batch(
    symbol: str,
    timeframe: str,
    reason: str,
    *,
    source_id: str = "binance_usdm_klines",
    provider: str = "binance_usdm",
    price_basis: str = "last_trade",
    decision_cutoff: str | None = None,
) -> dict[str, Any]:
    cutoff = decision_cutoff or utc_now()
    payload = build_chart_candle_batch(
        symbol,
        timeframe,
        [],
        decision_cutoff=cutoff,
        source_id=source_id,
        provider=provider,
        price_basis=price_basis,
        server_time=cutoff,
        ingested_at=cutoff,
    )
    payload["degradation_state"] = "quarantined"
    payload["capability_mask"]["action"] = "skip"
    payload["capability_mask"]["value_errors"] = sorted(set(payload["capability_mask"]["value_errors"] + [f"provider_error:{reason}"]))
    return payload


def store_candle_batch(batch: dict[str, Any]) -> dict[str, Any]:
    path = cache_path(str(batch.get("symbol") or ""), str(batch.get("timeframe") or ""))
    append_jsonl(path, batch)
    write_json_atomic(latest_path(str(batch.get("symbol") or ""), str(batch.get("timeframe") or "")), batch)
    try:
        from event_store import append_event_envelope

        append_event_envelope(
            "chart.candles.cached",
            {
                "batch_id": batch.get("batch_id"),
                "symbol": batch.get("symbol"),
                "timeframe": batch.get("timeframe"),
                "price_basis": batch.get("price_basis"),
                "degradation_state": batch.get("degradation_state"),
            },
            "chart_candle_service",
            str((batch.get("source_ids") or ["chart_candle_service"])[0]),
            str(batch.get("batch_id") or "chart_candles"),
        )
    except Exception:
        pass
    return batch


def load_cached_batches(symbol: str, timeframe: str) -> list[dict[str, Any]]:
    return read_jsonl(cache_path(symbol, timeframe))


def load_closed_candles(symbol: str, timeframe: str, cutoff: str, limit: int = 200) -> dict[str, Any]:
    batches = load_cached_batches(symbol, timeframe)
    if not batches:
        return provider_error_batch(symbol, timeframe, "missing_cache", decision_cutoff=cutoff)
    rows: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    input_event_ids: list[str] = []
    price_basis = None
    native_timeframe = True
    for batch in batches:
        for source_id in batch.get("source_ids") or []:
            source_ids.add(str(source_id))
        input_event_ids.append(str(batch.get("batch_id")))
        price_basis = price_basis or batch.get("price_basis") or "last_trade"
        native_timeframe = bool(batch.get("native_timeframe", True))
        for bar in batch.get("bars") or []:
            if bar.get("is_final") is not True:
                continue
            proof = build_cutoff_proof([bar], cutoff)
            if proof.get("ok"):
                rows.append(bar)
    unique_by_open = {str(row.get("open_time")): row for row in rows}
    selected = sorted(unique_by_open.values(), key=lambda item: item.get("open_time") or "")[-max(0, int(limit)) :]
    return build_chart_candle_batch(
        symbol,
        timeframe,
        selected,
        decision_cutoff=cutoff,
        source_id="chart_candle_cache",
        provider="local_cache",
        price_basis=str(price_basis or "last_trade"),
        server_time=cutoff,
        ingested_at=cutoff,
        input_event_ids=input_event_ids,
        source_manifest_ids=sorted(source_ids),
        native_timeframe=native_timeframe,
        min_candles=1,
    )


def fetch_binance_futures_candles(
    symbol: str,
    timeframe: str,
    *,
    limit: int = 200,
    price_basis: str = "last_trade",
    client: Any | None = None,
) -> dict[str, Any]:
    if client is None:
        from tradingagents.binance.client import spot_client

        client = spot_client()
    try:
        server_payload = client.futures_time() if hasattr(client, "futures_time") else {}
        server_ms = server_payload.get("serverTime") if isinstance(server_payload, dict) else None
        server_time = iso(dt_from_ms(server_ms)) if server_ms else utc_now()
        if price_basis == "last_trade":
            raw_rows = client.futures_klines(symbol=symbol.upper(), interval=timeframe, limit=limit)
        elif price_basis == "mark" and hasattr(client, "futures_mark_price_klines"):
            raw_rows = client.futures_mark_price_klines(symbol=symbol.upper(), interval=timeframe, limit=limit)
        elif price_basis == "index" and hasattr(client, "futures_index_price_klines"):
            raw_rows = client.futures_index_price_klines(symbol=symbol.upper(), interval=timeframe, limit=limit)
        else:
            return provider_error_batch(symbol, timeframe, f"unsupported_provider_price_basis:{price_basis}", price_basis=price_basis, decision_cutoff=server_time)
        return build_chart_candle_batch(symbol, timeframe, raw_rows, decision_cutoff=server_time, price_basis=price_basis, server_time=server_time, ingested_at=utc_now())
    except Exception as exc:
        return provider_error_batch(symbol, timeframe, str(exc)[:80], price_basis=price_basis)
