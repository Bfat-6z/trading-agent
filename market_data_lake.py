"""Replayable local market data cache for paper learning."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION, schema_digest
from atomic_state import read_json, write_json_atomic
from source_provenance import build_provenance, provenance_allows_effect
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MARKET_CACHE_DIR = STATE_DIR / "market_cache"
REPLAY_MANIFEST_DIR = STATE_DIR / "replay_manifests"


def normalize_candle(row: dict[str, Any]) -> dict[str, Any]:
    ts = str(row.get("ts") or row.get("open_time") or row.get("time"))
    close_ts = str(row.get("candle_close_time") or row.get("close_time") or row.get("closed_at") or ts)
    has_volume = "volume" in row or "quote_volume" in row
    volume = float(row.get("volume", row.get("quote_volume", 0.0)) or 0.0)
    return {
        "ts": ts,
        "candle_close_time": close_ts,
        "open": float(row.get("open")),
        "high": float(row.get("high")),
        "low": float(row.get("low")),
        "close": float(row.get("close")),
        "volume": volume,
        "value_status": {"volume": "missing" if not has_volume else "zero" if volume == 0 else "ok"},
        "available_at": str(row.get("available_at") or row.get("known_at") or row.get("finalized_at") or close_ts),
        "known_at": str(row.get("known_at") or row.get("available_at") or row.get("finalized_at") or close_ts),
        "ingested_at": str(row.get("ingested_at") or row.get("known_at") or row.get("available_at") or close_ts),
        "finalized_at": str(row.get("finalized_at") or close_ts),
    }


def candle_cache_id(symbol: str, timeframe: str, candles: list[dict[str, Any]], source_id: str) -> str:
    raw = json.dumps({"symbol": symbol.upper(), "timeframe": timeframe, "source_id": source_id, "candles": candles}, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))
    return "candles_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def cache_path(cache_id: str) -> Path:
    return MARKET_CACHE_DIR / f"{cache_id}.json"


def store_candles(
    symbol: str,
    timeframe: str,
    candles: list[dict[str, Any]],
    source_id: str = "local_state",
    assumptions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = [normalize_candle(row) for row in candles]
    normalized.sort(key=lambda row: row["ts"])
    cache_id = candle_cache_id(symbol, timeframe, normalized, source_id)
    provenance = build_provenance("candles", [source_id], metadata={"symbol": symbol.upper(), "timeframe": timeframe})
    usable = provenance_allows_effect(provenance, "feature_input")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "cache_id": cache_id,
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "source_id": source_id,
        "provenance_id": provenance["provenance_id"],
        "source_trust": provenance["source_check"],
        "provenance_status": provenance["provenance_status"],
        "allowed_effect": provenance["allowed_effect"],
        "taint_classes": provenance["taint_classes"],
        "usable_for_paper": bool(usable),
        "quarantine_reasons": provenance["quarantine_reasons"],
        "created_at": utc_now(),
        "assumptions": assumptions or {},
        "candles": normalized,
    }
    write_json_atomic(cache_path(cache_id), payload)
    return payload


def load_candles(cache_id: str) -> dict[str, Any]:
    return read_json(cache_path(cache_id), default={})


def select_window(candles: list[dict[str, Any]], start_ts: str | None = None, end_ts: str | None = None) -> list[dict[str, Any]]:
    start = parse_utc(start_ts) if start_ts else None
    end = parse_utc(end_ts) if end_ts else None
    rows = []
    for candle in candles:
        ts = parse_utc(candle.get("ts"))
        if not ts:
            continue
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        rows.append(candle)
    return rows


def coverage_report(candles: list[dict[str, Any]], minimum_candles: int = 3) -> dict[str, Any]:
    missing = len(candles) < minimum_candles
    invalid_ts = [idx for idx, candle in enumerate(candles) if not parse_utc(candle.get("ts"))]
    errors = ["insufficient_candle_coverage"] if missing else []
    errors.extend(f"invalid_candle_ts:{idx}" for idx in invalid_ts)
    missing_values = sum(1 for candle in candles for status in (candle.get("value_status") or {}).values() if status == "missing")
    total_values = max(1, sum(len(candle.get("value_status") or {}) for candle in candles))
    return {"ok": not errors, "candle_count": len(candles), "minimum_candles": minimum_candles, "missing_rate": round(missing_values / total_values, 6), "errors": errors}


def create_replay_manifest(
    trade_id: str,
    candle_cache_id: str,
    source_ids: list[str],
    assumptions: dict[str, Any],
    input_ids: list[str] | None = None,
) -> dict[str, Any]:
    provenance = build_provenance("replay_manifest", source_ids, input_ids=[candle_cache_id] + (input_ids or []), metadata={"trade_id": trade_id})
    usable = provenance_allows_effect(provenance, "feature_input")
    material = {
        "trade_id": trade_id,
        "candle_cache_id": candle_cache_id,
        "source_ids": sorted(source_ids),
        "assumptions": assumptions,
        "input_ids": sorted(input_ids or []),
        "source_snapshot_hash": provenance.get("source_snapshot_hash"),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": "replay_" + hashlib.sha256(json.dumps(material, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()[:20],
        "schema_digest": schema_digest("feature.row.created"),
        "code_version": "local-dev",
        "config_digest": "sha256:" + hashlib.sha256(json.dumps(assumptions, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "source_snapshot_hashes": [provenance.get("source_snapshot_hash")],
        "fixture_ids": sorted(input_ids or []),
        "trade_id": trade_id,
        "candle_cache_id": candle_cache_id,
        "source_ids": source_ids,
        "provenance_id": provenance["provenance_id"],
        "source_trust": provenance["source_check"],
        "provenance_status": provenance["provenance_status"],
        "allowed_effect": provenance["allowed_effect"],
        "taint_classes": provenance["taint_classes"],
        "usable_for_paper": bool(usable),
        "quarantine_reasons": provenance["quarantine_reasons"],
        "assumptions": assumptions,
        "created_at": utc_now(),
    }
    write_json_atomic(REPLAY_MANIFEST_DIR / f"{manifest['manifest_id']}.json", manifest)
    return manifest
