"""Replayable local market data cache for paper learning."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from source_provenance import build_provenance
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MARKET_CACHE_DIR = STATE_DIR / "market_cache"
REPLAY_MANIFEST_DIR = STATE_DIR / "replay_manifests"


def normalize_candle(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": str(row.get("ts") or row.get("open_time") or row.get("time")),
        "open": float(row.get("open")),
        "high": float(row.get("high")),
        "low": float(row.get("low")),
        "close": float(row.get("close")),
        "volume": float(row.get("volume", row.get("quote_volume", 0.0)) or 0.0),
    }


def candle_cache_id(symbol: str, timeframe: str, candles: list[dict[str, Any]], source_id: str) -> str:
    first = candles[0].get("ts") if candles else "empty"
    last = candles[-1].get("ts") if candles else "empty"
    raw = f"{symbol.upper()}:{timeframe}:{source_id}:{first}:{last}:{len(candles)}"
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
    payload = {
        "schema_version": SCHEMA_VERSION,
        "cache_id": cache_id,
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "source_id": source_id,
        "provenance_id": provenance["provenance_id"],
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
    return {"ok": not missing, "candle_count": len(candles), "minimum_candles": minimum_candles, "errors": ["insufficient_candle_coverage"] if missing else []}


def create_replay_manifest(
    trade_id: str,
    candle_cache_id: str,
    source_ids: list[str],
    assumptions: dict[str, Any],
    input_ids: list[str] | None = None,
) -> dict[str, Any]:
    provenance = build_provenance("replay_manifest", source_ids, input_ids=[candle_cache_id] + (input_ids or []), metadata={"trade_id": trade_id})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": "replay_" + hashlib.sha256(f"{trade_id}:{candle_cache_id}:{assumptions}".encode("utf-8")).hexdigest()[:20],
        "trade_id": trade_id,
        "candle_cache_id": candle_cache_id,
        "source_ids": source_ids,
        "provenance_id": provenance["provenance_id"],
        "assumptions": assumptions,
        "created_at": utc_now(),
    }
    write_json_atomic(REPLAY_MANIFEST_DIR / f"{manifest['manifest_id']}.json", manifest)
    return manifest
