"""Data source registry and quota/degraded-state tracking.

Phase B learning artifacts must know where their inputs came from. This module
keeps source metadata local and deterministic; it does not call providers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, write_json_atomic
from timebase import parse_utc, seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DATA_SOURCES_LATEST = STATE_DIR / "data_sources_latest.json"
DATA_SOURCE_EVENTS = STATE_DIR / "data_source_events.jsonl"

DEFAULT_SOURCES = {
    "local_state": {"provider": "local", "source_type": "state", "freshness_sla_seconds": 3600, "trust_score": 0.8},
    "binance_usdm_klines": {"provider": "binance", "source_type": "market_candles", "freshness_sla_seconds": 300, "trust_score": 0.85},
    "news_observer": {"provider": "mixed", "source_type": "news", "freshness_sla_seconds": 3600, "trust_score": 0.55},
    "derivatives_observer": {"provider": "binance", "source_type": "derivatives", "freshness_sla_seconds": 600, "trust_score": 0.7},
}


def default_registry() -> dict[str, Any]:
    now = utc_now()
    sources = {}
    for source_id, row in DEFAULT_SOURCES.items():
        sources[source_id] = {
            "source_id": source_id,
            "endpoint": None,
            "status": "ok",
            "last_success_at": now,
            "last_failure_at": None,
            "cooldown_until": None,
            "quota_used": 0,
            "quota_limit": None,
            "license_notes": "local registry entry; verify provider terms before production use",
            **row,
        }
    return {"schema_version": SCHEMA_VERSION, "updated_at": now, "sources": sources}


def load_source_registry(path: Path = DATA_SOURCES_LATEST) -> dict[str, Any]:
    payload = read_json(path, default={})
    if not isinstance(payload, dict) or not payload.get("sources"):
        return default_registry()
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("updated_at", utc_now())
    return payload


def source_age_seconds(source: dict[str, Any]) -> float | None:
    ts = source.get("last_success_at") or source.get("updated_at")
    if not parse_utc(ts):
        return None
    return seconds_between(ts, utc_now())


def evaluate_source(source: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    status = str(source.get("status") or "unknown")
    if status in {"rate_limited", "outage", "disabled"}:
        errors.append(f"source_{status}")
    age = source_age_seconds(source)
    sla = int(source.get("freshness_sla_seconds") or 0)
    if age is None:
        warnings.append("missing_source_timestamp")
    elif sla and age > sla:
        errors.append("source_stale")
    cooldown = parse_utc(source.get("cooldown_until"))
    if cooldown and seconds_between(utc_now(), source.get("cooldown_until")) and seconds_between(utc_now(), source.get("cooldown_until")) > 0:
        errors.append("source_in_cooldown")
    trust = float(source.get("trust_score") or 0.0)
    quality = max(0.0, min(1.0, trust - (0.25 if errors else 0.0) - (0.1 if warnings else 0.0)))
    return {"source_id": source.get("source_id"), "usable": not errors, "quality_score": round(quality, 4), "errors": errors, "warnings": warnings, "age_seconds": age}


def register_source(source_id: str, metadata: dict[str, Any], path: Path = DATA_SOURCES_LATEST) -> dict[str, Any]:
    registry = load_source_registry(path)
    row = {**DEFAULT_SOURCES.get(source_id, {}), **metadata, "source_id": source_id, "updated_at": utc_now()}
    row.setdefault("status", "ok")
    row.setdefault("last_success_at", utc_now())
    registry["sources"][source_id] = row
    registry["updated_at"] = utc_now()
    write_json_atomic(path, registry)
    return row


def mark_source_event(source_id: str, event: str, detail: str = "", path: Path = DATA_SOURCES_LATEST) -> dict[str, Any]:
    registry = load_source_registry(path)
    source = registry["sources"].get(source_id, {"source_id": source_id, "provider": "unknown", "source_type": "unknown", "trust_score": 0.3})
    now = utc_now()
    if event == "success":
        source.update({"status": "ok", "last_success_at": now, "cooldown_until": None})
    elif event == "rate_limited":
        source.update({"status": "rate_limited", "last_failure_at": now, "cooldown_until": now})
    elif event in {"failure", "outage"}:
        source.update({"status": "outage", "last_failure_at": now})
    source["quota_used"] = int(source.get("quota_used") or 0) + (1 if event in {"success", "rate_limited"} else 0)
    registry["sources"][source_id] = source
    registry["updated_at"] = now
    write_json_atomic(path, registry)
    append_jsonl(DATA_SOURCE_EVENTS, {"schema_version": SCHEMA_VERSION, "ts": now, "source_id": source_id, "event": event, "detail": detail})
    return evaluate_source(source)


def evaluate_sources(source_ids: list[str], registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_source_registry()
    rows = []
    missing = []
    for source_id in source_ids:
        source = (registry.get("sources") or {}).get(source_id)
        if not source:
            missing.append(source_id)
            rows.append({"source_id": source_id, "usable": False, "quality_score": 0.0, "errors": ["source_missing"], "warnings": []})
        else:
            rows.append(evaluate_source(source))
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "usable": all(row["usable"] for row in rows),
        "min_quality_score": min([row["quality_score"] for row in rows], default=0.0),
        "missing_sources": missing,
        "sources": rows,
    }
