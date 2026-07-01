"""Data source registry and quota/degraded-state tracking.

Phase B learning artifacts must know where their inputs came from. This module
keeps source metadata local and deterministic; it does not call providers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, write_json_atomic
from data_trust import combine_allowed_effects, source_policy
from timebase import parse_utc, seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DATA_SOURCES_LATEST = STATE_DIR / "data_sources_latest.json"
DATA_SOURCE_EVENTS = STATE_DIR / "data_source_events.jsonl"

DEFAULT_SOURCES = {
    "local_state": {"provider": "local", "source_type": "state", "freshness_sla_seconds": 3600, "trust_score": 0.8},
    "market_observer": {"provider": "binance", "source_type": "market", "freshness_sla_seconds": 900, "trust_score": 0.82},
    "binance_usdm_klines": {"provider": "binance", "source_type": "market_candles", "freshness_sla_seconds": 300, "trust_score": 0.85},
    # Phase 1: real closed candles served from the local chart cache
    # (chart_candle_service.load_closed_candles). Provider "local" so the
    # registry treats the on-disk cache as verified with a fresh timestamp;
    # the underlying bars are real Binance closed klines (manifest
    # binance_usdm_klines) and each bar carries its own finalized_at that the
    # cutoff_proof independently checks for lookahead. 5m bars -> 900s SLA.
    "chart_candle_cache": {"provider": "local", "source_type": "market_candles", "freshness_sla_seconds": 900, "trust_score": 0.85},
    "binance_usdm_orderbook": {"provider": "binance", "source_type": "orderbook", "freshness_sla_seconds": 30, "trust_score": 0.85},
    "binance_usdm_liquidations": {"provider": "binance", "source_type": "liquidation", "freshness_sla_seconds": 120, "trust_score": 0.78},
    "binance_usdm_funding_oi": {"provider": "binance", "source_type": "funding", "freshness_sla_seconds": 600, "trust_score": 0.78},
    "binance_usdm_exchange_info": {"provider": "binance", "source_type": "exchange_info", "freshness_sla_seconds": 86400, "trust_score": 0.92},
    "news_observer": {"provider": "mixed", "source_type": "news", "freshness_sla_seconds": 3600, "trust_score": 0.55},
    "cryptopanic": {"provider": "cryptopanic", "source_type": "news_api", "freshness_sla_seconds": 3600, "trust_score": 0.62},
    "alpha_vantage": {"provider": "alpha_vantage", "source_type": "news_api", "freshness_sla_seconds": 3600, "trust_score": 0.68},
    "rss_news": {"provider": "rss", "source_type": "rss", "freshness_sla_seconds": 3600, "trust_score": 0.62},
    "telegram_public_whale_flow": {"provider": "telegram_public", "source_type": "telegram", "freshness_sla_seconds": 900, "trust_score": 0.25},
    "manual_screenshot": {"provider": "operator", "source_type": "manual_screenshot", "freshness_sla_seconds": 86400, "trust_score": 0.1},
    "derivatives_observer": {"provider": "binance", "source_type": "derivatives", "freshness_sla_seconds": 600, "trust_score": 0.7},
}

PROVIDER_COMPLIANCE_DEFAULTS = {
    "allowed_use": "paper_research_only",
    "retention": "minimal_local_cache",
    "redistribution": "disabled_by_default",
    "retry_backoff": "exponential",
    "ban_circuit_breaker": True,
    "tos_evidence": "verify_before_production",
}


def apply_source_defaults(source_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    row = {**DEFAULT_SOURCES.get(source_id, {}), **metadata, "source_id": source_id}
    policy = source_policy(row.get("source_type"), row.get("rights"), row.get("provider"))
    row.setdefault("rights", policy["rights"])
    row.setdefault("allowed_effect", policy["allowed_effect"])
    row.setdefault("taint_class", policy["taint_class"])
    row.setdefault("parse_confidence", 1.0 if policy["taint_class"] in {"public_market", "objective_ledger"} else 0.5)
    row.setdefault("provider_compliance", PROVIDER_COMPLIANCE_DEFAULTS)
    row.setdefault("endpoint_weight", None)
    row.setdefault("quota_limit", None)
    row.setdefault("quota_used", 0)
    row.setdefault("cache_ttl_seconds", row.get("freshness_sla_seconds"))
    row.setdefault("fallback_provider", None)
    row.setdefault("blackout_behavior", "degrade_to_unknown")
    row.setdefault("cost_per_day_usd", 0.0)
    row.setdefault("source_identity", {"provider": row.get("provider"), "source_id": source_id})
    return row


def default_registry() -> dict[str, Any]:
    now = utc_now()
    sources = {}
    for source_id, row in DEFAULT_SOURCES.items():
        is_local = row.get("provider") == "local"
        sources[source_id] = apply_source_defaults(source_id, {
            "source_id": source_id,
            "endpoint": None,
            "status": "ok" if is_local else "unverified",
            "last_success_at": now if is_local else None,
            "last_failure_at": None,
            "cooldown_until": None,
            "license_notes": "local registry entry; verify provider terms before production use",
            **row,
        })
    return {"schema_version": SCHEMA_VERSION, "updated_at": now, "sources": sources}


def load_source_registry(path: Path = DATA_SOURCES_LATEST) -> dict[str, Any]:
    payload = read_json(path, default={})
    if not isinstance(payload, dict) or not payload.get("sources"):
        return default_registry()
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("updated_at", utc_now())
    # Backfill any DEFAULT_SOURCES entry missing from a stale on-disk registry
    # (e.g. sources added in code after the file was written), so newly declared
    # sources like chart_candle_cache resolve instead of erroring source_missing.
    sources = payload.get("sources")
    if isinstance(sources, dict):
        now = utc_now()
        for source_id, row in DEFAULT_SOURCES.items():
            if source_id not in sources:
                is_local = row.get("provider") == "local"
                sources[source_id] = apply_source_defaults(source_id, {
                    "source_id": source_id,
                    "endpoint": None,
                    "status": "ok" if is_local else "unverified",
                    "last_success_at": now if is_local else None,
                    "last_failure_at": None,
                    "cooldown_until": None,
                    "license_notes": "local registry entry; verify provider terms before production use",
                    **row,
                })
    return payload


def source_age_seconds(source: dict[str, Any]) -> float | None:
    ts = source.get("last_success_at") or source.get("updated_at")
    if not parse_utc(ts):
        return None
    return seconds_between(ts, utc_now())


def evaluate_source(source: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    source = apply_source_defaults(str(source.get("source_id") or "unknown"), source)
    status = str(source.get("status") or "unknown")
    if status == "unverified":
        errors.append("source_unverified")
    if status in {"rate_limited", "outage", "disabled"}:
        errors.append(f"source_{status}")
    age = source_age_seconds(source)
    sla = int(source.get("freshness_sla_seconds") or 0)
    if age is None:
        if source.get("provider") == "local":
            warnings.append("missing_source_timestamp")
        else:
            errors.append("missing_source_timestamp")
    elif sla and age > sla:
        errors.append("source_stale")
    cooldown = parse_utc(source.get("cooldown_until"))
    if cooldown and seconds_between(utc_now(), source.get("cooldown_until")) and seconds_between(utc_now(), source.get("cooldown_until")) > 0:
        errors.append("source_in_cooldown")
    quota_limit = source.get("quota_limit")
    quota_used = int(source.get("quota_used") or 0)
    if quota_limit is not None:
        limit_value = int(quota_limit)
        if quota_used >= limit_value:
            errors.append("quota_exhausted")
        elif limit_value and quota_used / limit_value >= 0.8:
            warnings.append("quota_near_limit")
    parse_confidence = float(source.get("parse_confidence") or 0.0)
    if parse_confidence < 0.35:
        errors.append("low_parse_confidence")
    elif parse_confidence < 0.65:
        warnings.append("low_parse_confidence")
    trust = float(source.get("trust_score") or 0.0)
    quality = max(0.0, min(1.0, trust - (0.25 if errors else 0.0) - (0.1 if warnings else 0.0)))
    return {
        "source_id": source.get("source_id"),
        "provider": source.get("provider"),
        "source_type": source.get("source_type"),
        "usable": not errors,
        "quality_score": round(quality, 4),
        "trust_score": trust,
        "parse_confidence": round(parse_confidence, 4),
        "allowed_effect": source.get("allowed_effect"),
        "taint_class": source.get("taint_class"),
        "rights": source.get("rights"),
        "errors": errors,
        "warnings": warnings,
        "age_seconds": age,
        "quota_used": quota_used,
        "quota_limit": quota_limit,
    }


def register_source(source_id: str, metadata: dict[str, Any], path: Path = DATA_SOURCES_LATEST) -> dict[str, Any]:
    registry = load_source_registry(path)
    row = apply_source_defaults(source_id, {**metadata, "updated_at": utc_now()})
    row.setdefault("status", "ok")
    row.setdefault("last_success_at", utc_now())
    registry["sources"][source_id] = row
    registry["updated_at"] = utc_now()
    write_json_atomic(path, registry)
    return row


def _emit_source_bus_event(source: dict[str, Any], event: str, detail: str = "", event_db_path: Path | None = None) -> None:
    if event_db_path is None:
        return
    try:
        from event_store import append_event_envelope

        source_id = str(source.get("source_id") or "unknown")
        if event == "success":
            event_type = "source.restored"
            payload = {"source_id": source_id, "status": source.get("status") or "ok"}
        elif event == "quota_exhausted":
            event_type = "source.quota_exhausted"
            payload = {"source_id": source_id, "used": int(source.get("quota_used") or 0), "limit": int(source.get("quota_limit") or 0)}
        else:
            event_type = "source.degraded"
            payload = {"source_id": source_id, "status": source.get("status") or "degraded", "reason": event, "detail": detail[:300]}
        append_event_envelope(event_type, payload, "data_source_registry", "data_source_registry", f"{source_id}:{event}:{utc_now()}", db_path=event_db_path)
    except Exception:
        return


def mark_source_event(source_id: str, event: str, detail: str = "", path: Path = DATA_SOURCES_LATEST, event_db_path: Path | None = None) -> dict[str, Any]:
    registry = load_source_registry(path)
    source = apply_source_defaults(source_id, registry["sources"].get(source_id, {"source_id": source_id, "provider": "unknown", "source_type": "unknown", "trust_score": 0.3}))
    now = utc_now()
    if event == "success":
        source.update({"status": "ok", "last_success_at": now, "cooldown_until": None})
    elif event == "rate_limited":
        source.update({"status": "rate_limited", "last_failure_at": now, "cooldown_until": now})
    elif event in {"failure", "outage"}:
        source.update({"status": "outage", "last_failure_at": now})
    elif event == "quota_exhausted":
        source.update({"status": "rate_limited", "last_failure_at": now, "cooldown_until": now})
    source["quota_used"] = int(source.get("quota_used") or 0) + (1 if event in {"success", "rate_limited"} else 0)
    registry["sources"][source_id] = source
    registry["updated_at"] = now
    write_json_atomic(path, registry)
    append_jsonl(DATA_SOURCE_EVENTS, {"schema_version": SCHEMA_VERSION, "ts": now, "source_id": source_id, "event": event, "detail": detail})
    _emit_source_bus_event(source, event, detail, event_db_path=event_db_path)
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
        "allowed_effect": combine_allowed_effects([str(row.get("allowed_effect") or "deny") for row in rows]),
        "taint_classes": sorted({str(row.get("taint_class")) for row in rows if row.get("taint_class")}),
        "missing_sources": missing,
        "sources": rows,
    }
