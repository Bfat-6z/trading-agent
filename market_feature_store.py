"""Canonical feature store for paper learning and setup scoring."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from regime_labeler import label_decision_regime, label_regime
from source_provenance import build_provenance, provenance_allows_effect
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
FEATURE_STORE_DIR = STATE_DIR / "feature_store"
REGIME_LATEST = STATE_DIR / "agent_memory" / "regime_latest.json"

FEATURE_VERSION = "market_features_v2"
FEATURE_FAMILY = "market_features_core"
FEATURE_SOURCE_MATRIX = {
    FEATURE_FAMILY: {
        "required": ["ohlcv"],
        "optional": ["derivatives", "btc_eth_regime", "market_breadth_beta"],
    }
}


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))


def digest_payload(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def short_digest(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]


def safe_number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def classify_value(row: dict[str, Any], field: str, *, required: bool = True) -> tuple[float, str]:
    if field not in row or row.get(field) in (None, ""):
        return 0.0, "invalid_missing" if required else "missing"
    value = safe_number(row.get(field))
    if value is None:
        return 0.0, "invalid"
    if value == 0:
        return 0.0, "zero"
    return value, "ok"


def canonical_ts(value: Any, fallback: Any | None = None) -> str:
    parsed = parse_utc(value) or parse_utc(fallback)
    return parsed.isoformat(timespec="seconds") if parsed else str(value or fallback or "")


def normalize_feature_candles(candles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    normalized: list[dict[str, Any]] = []
    value_status: dict[str, str] = {}
    errors: list[str] = []
    for idx, row in enumerate(candles):
        if not isinstance(row, dict):
            errors.append(f"invalid_candle:{idx}")
            continue
        ts = canonical_ts(row.get("ts") or row.get("open_time") or row.get("time"))
        close_time = canonical_ts(row.get("candle_close_time") or row.get("close_time") or row.get("closed_at") or ts, ts)
        item: dict[str, Any] = {
            "ts": ts,
            "candle_close_time": close_time,
            "available_at": canonical_ts(row.get("available_at") or row.get("known_at") or row.get("finalized_at") or close_time, close_time),
            "known_at": canonical_ts(row.get("known_at") or row.get("available_at") or row.get("finalized_at") or close_time, close_time),
            "ingested_at": canonical_ts(row.get("ingested_at") or row.get("observed_at") or row.get("known_at") or row.get("available_at") or close_time, close_time),
            "finalized_at": canonical_ts(row.get("finalized_at") or row.get("closed_at") or close_time, close_time),
        }
        for field in ("open", "high", "low", "close"):
            value, status = classify_value(row, field, required=True)
            item[field] = value
            value_status[f"candles[{idx}].{field}"] = status
            if status.startswith("invalid"):
                errors.append(f"{field}_{status}:{idx}")
        volume, status = classify_value(row, "volume", required=False)
        item["volume"] = volume
        value_status[f"candles[{idx}].volume"] = status
        normalized.append(item)
    normalized.sort(key=lambda item: item.get("ts") or "")
    return normalized, value_status, sorted(set(errors))


def dependency_times(candles: list[dict[str, Any]], derivatives: dict[str, Any] | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for idx, candle in enumerate(candles):
        rows.append(
            {
                "input_id": f"candle:{idx}:{candle.get('ts')}",
                "available_at": candle.get("available_at") or candle.get("candle_close_time") or candle.get("ts"),
                "known_at": candle.get("known_at") or candle.get("available_at") or candle.get("ts"),
                "ingested_at": candle.get("ingested_at") or candle.get("known_at") or candle.get("available_at") or candle.get("ts"),
                "finalized_at": candle.get("finalized_at") or candle.get("candle_close_time") or candle.get("ts"),
            }
        )
    if derivatives:
        rows.append(
            {
                "input_id": str(derivatives.get("derivatives_id") or derivatives.get("event_id") or "derivatives"),
                "available_at": derivatives.get("available_at") or derivatives.get("known_at") or derivatives.get("finalized_at") or derivatives.get("ts") or derivatives.get("updated_at"),
                "known_at": derivatives.get("known_at") or derivatives.get("available_at") or derivatives.get("ts") or derivatives.get("updated_at"),
                "ingested_at": derivatives.get("ingested_at") or derivatives.get("known_at") or derivatives.get("available_at") or derivatives.get("ts") or derivatives.get("updated_at"),
                "finalized_at": derivatives.get("finalized_at") or derivatives.get("available_at") or derivatives.get("ts") or derivatives.get("updated_at"),
            }
        )
    return rows


def build_cutoff_proof(inputs: list[dict[str, str]], decision_cutoff: str, latency_buffer_seconds: int = 0) -> dict[str, Any]:
    cutoff_dt = parse_utc(decision_cutoff)
    errors: list[str] = []
    if not cutoff_dt:
        errors.append("invalid_decision_cutoff")
        return {"ok": False, "decision_cutoff": decision_cutoff, "latency_buffer_seconds": latency_buffer_seconds, "errors": errors}
    allowed_dt = cutoff_dt - timedelta(seconds=max(0, int(latency_buffer_seconds)))
    max_seen = None
    checked: list[str] = []
    for item in inputs:
        input_id = str(item.get("input_id") or "unknown")
        row_max = None
        # Lookahead is defined by data-existence timestamps (available_at/known_at/
        # finalized_at) — those must be <= cutoff. ingested_at is the operational
        # cache write-time (~now) and legitimately exceeds an older decision
        # cutoff; gating on it would starve the decision path without adding
        # safety. It is still validated for presence below.
        for field in ("available_at", "known_at", "finalized_at"):
            parsed = parse_utc(item.get(field))
            if not parsed:
                errors.append(f"invalid_{field}:{input_id}")
                continue
            row_max = parsed if row_max is None or parsed > row_max else row_max
            if parsed > allowed_dt:
                errors.append(f"{field}_after_cutoff:{input_id}")
        if not parse_utc(item.get("ingested_at")):
            errors.append(f"invalid_ingested_at:{input_id}")
        if row_max:
            max_seen = row_max if max_seen is None or row_max > max_seen else max_seen
        checked.append(input_id)
    return {
        "ok": not errors,
        "decision_cutoff": cutoff_dt.isoformat(timespec="seconds"),
        "latency_buffer_seconds": max(0, int(latency_buffer_seconds)),
        "usable_input_deadline": allowed_dt.isoformat(timespec="seconds"),
        "max_input_time": max_seen.isoformat(timespec="seconds") if max_seen else None,
        "checked_input_ids": checked,
        "errors": sorted(set(errors)),
    }


def feature_path(feature_row_id: str) -> Path:
    return FEATURE_STORE_DIR / f"{feature_row_id}.json"


def load_feature_row(feature_row_id: str) -> dict[str, Any]:
    path = feature_path(feature_row_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def ema(values: list[float], span: int) -> float:
    if not values:
        return 0.0
    alpha = 2 / (span + 1)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1 - alpha) * value
    return value


def true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    ranges: list[float] = []
    prev_close: float | None = None
    for row in candles:
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        if prev_close is None:
            ranges.append(high - low)
        else:
            ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return ranges


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < 2:
        return 50.0
    diffs = [values[i] - values[i - 1] for i in range(1, len(values))][-period:]
    gains = [max(0.0, item) for item in diffs]
    losses = [abs(min(0.0, item)) for item in diffs]
    avg_gain = mean(gains) if gains else 0.0
    avg_loss = mean(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def feature_id(
    symbol: str,
    timeframe: str,
    candles: list[dict[str, Any]],
    derivatives: dict[str, Any] | None,
    *,
    source_ids: list[str] | None = None,
    input_event_ids: list[str] | None = None,
    source_manifest_ids: list[str] | None = None,
) -> str:
    return short_digest(
        "features",
        {
            "feature_version": FEATURE_VERSION,
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "candles": candles,
            "derivatives": derivatives or {},
            "source_ids": sorted(source_ids or []),
            "input_event_ids": sorted(input_event_ids or []),
            "source_manifest_ids": sorted(source_manifest_ids or []),
        },
    )


def build_capability_mask(
    provenance: dict[str, Any],
    *,
    missing_optional: list[str],
    stale_optional: list[str] | None = None,
    cutoff_proof: dict[str, Any] | None = None,
    value_errors: list[str] | None = None,
) -> dict[str, Any]:
    source_check = provenance.get("source_check") if isinstance(provenance.get("source_check"), dict) else {}
    source_errors = sorted({str(error) for row in source_check.get("sources", []) for error in (row.get("errors") or [])})
    source_warnings = sorted({str(warn) for row in source_check.get("sources", []) for warn in (row.get("warnings") or [])})
    required_missing = []
    required_stale = []
    for error in source_errors:
        if error == "source_missing":
            required_missing.append("source")
        if error == "source_stale":
            required_stale.append("source")
    cutoff_errors = list((cutoff_proof or {}).get("errors") or [])
    required = FEATURE_SOURCE_MATRIX[FEATURE_FAMILY]["required"]
    optional = FEATURE_SOURCE_MATRIX[FEATURE_FAMILY]["optional"]
    action = "normal"
    if required_missing or required_stale or source_errors or value_errors or cutoff_errors:
        action = "skip"
    elif missing_optional or stale_optional or source_warnings:
        action = "size_cap"
    return {
        "feature_family": FEATURE_FAMILY,
        "required": required,
        "optional": optional,
        "missing_required": sorted(set(required_missing)),
        "stale_required": sorted(set(required_stale)),
        "missing_optional": sorted(set(missing_optional)),
        "stale_optional": sorted(set(stale_optional or [])),
        "source_confidence": round(float(source_check.get("min_quality_score") or 0.0), 4),
        "source_errors": source_errors,
        "source_warnings": source_warnings,
        "cutoff_errors": cutoff_errors,
        "value_errors": sorted(set(value_errors or [])),
        "action": action,
    }


def compute_market_features(
    symbol: str,
    timeframe: str,
    candles: list[dict[str, Any]],
    derivatives: dict[str, Any] | None = None,
    source_ids: list[str] | None = None,
    input_event_ids: list[str] | None = None,
    source_manifest_ids: list[str] | None = None,
    decision_cutoff: str | None = None,
    latency_buffer_seconds: int = 0,
    fit_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_candles, value_status, value_errors = normalize_feature_candles(candles)
    if len(normalized_candles) < 3:
        raise ValueError("insufficient_candles_for_features")
    closes = [float(row["close"]) for row in normalized_candles]
    volumes = [float(row.get("volume", 0.0) or 0.0) for row in normalized_candles]
    trs = true_ranges(normalized_candles)
    last_close = closes[-1]
    ema_fast = ema(closes, min(5, len(closes)))
    ema_slow = ema(closes, min(20, len(closes)))
    atr = mean(trs[-min(14, len(trs)) :]) if trs else 0.0
    volume_base = mean(volumes[:-1]) if len(volumes) > 1 else volumes[-1]
    derivatives_confidence = 0.0 if not derivatives else float(derivatives.get("confidence", 0.5))
    microstructure_flow = derivatives.get("microstructure_flow") if isinstance(derivatives, dict) and isinstance(derivatives.get("microstructure_flow"), dict) else {}
    missing = [] if derivatives else ["derivatives"]
    cutoff = decision_cutoff or normalized_candles[-1].get("finalized_at") or normalized_candles[-1].get("candle_close_time") or normalized_candles[-1].get("ts")
    cutoff_proof = build_cutoff_proof(dependency_times(normalized_candles, derivatives), str(cutoff), latency_buffer_seconds=latency_buffer_seconds)
    clean_source_ids = source_ids or ["local_state"]
    fid = feature_id(
        symbol,
        timeframe,
        normalized_candles,
        derivatives,
        source_ids=clean_source_ids,
        input_event_ids=input_event_ids,
        source_manifest_ids=source_manifest_ids,
    )
    features = {
        "schema_version": SCHEMA_VERSION,
        "feature_version": FEATURE_VERSION,
        "feature_id": fid,
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "feature_window": {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "start": normalized_candles[0].get("ts"),
            "end": normalized_candles[-1].get("ts"),
            "candle_close_time": normalized_candles[-1].get("candle_close_time"),
        },
        "window_start": normalized_candles[0].get("ts"),
        "window_end": normalized_candles[-1].get("ts"),
        "candle_close_time": normalized_candles[-1].get("candle_close_time"),
        "candle_count": len(normalized_candles),
        "last_close": round(last_close, 10),
        "ohlcv": {
            "open": round(float(normalized_candles[-1]["open"]), 10),
            "high": round(float(normalized_candles[-1]["high"]), 10),
            "low": round(float(normalized_candles[-1]["low"]), 10),
            "close": round(last_close, 10),
            "volume": round(float(normalized_candles[-1]["volume"]), 10),
        },
        "ema_fast": round(ema_fast, 10),
        "ema_slow": round(ema_slow, 10),
        "trend_strength": round((ema_fast - ema_slow) / last_close if last_close else 0.0, 8),
        "atr": round(atr, 10),
        "atr_pct": round(atr / last_close if last_close else 0.0, 8),
        "range_pct": round((float(normalized_candles[-1]["high"]) - float(normalized_candles[-1]["low"])) / last_close if last_close else 0.0, 8),
        "volume_ratio": round(volumes[-1] / volume_base, 6) if volume_base else 0.0,
        "volume_spike": round(max(0.0, (volumes[-1] / volume_base) - 1.0), 6) if volume_base else 0.0,
        "rsi": round(rsi(closes), 4),
        "derivatives_confidence": round(derivatives_confidence, 4),
        "microstructure_flow": microstructure_flow,
        "canonical_instrument_id": microstructure_flow.get("canonical_instrument_id"),
        "instrument_snapshot_id": microstructure_flow.get("instrument_snapshot_id"),
        "price_basis": microstructure_flow.get("price_basis"),
        "missing_features": missing,
        "value_status": value_status,
        "missing_rate": round(sum(1 for status in value_status.values() if status in {"missing", "invalid_missing", "invalid", "imputed"}) / max(1, len(value_status)), 6),
        "feature_confidence": round(max(0.0, min(1.0, 0.85 - 0.25 * len(missing) + derivatives_confidence * 0.1)), 4),
        "computed_at": utc_now(),
    }
    decision_regime = label_decision_regime(
        features,
        decision_cutoff=str(cutoff),
        input_event_ids=input_event_ids or [],
        finalized_candle_lag=latency_buffer_seconds,
    )
    regime = label_regime(features)
    input_bundle = {
        "feature_version": FEATURE_VERSION,
        "candles": normalized_candles,
        "derivatives": derivatives or {},
        "input_event_ids": input_event_ids or [],
        "source_manifest_ids": source_manifest_ids or [],
        "decision_cutoff": str(cutoff),
        "latency_buffer_seconds": latency_buffer_seconds,
    }
    input_hash = digest_payload(input_bundle)
    manifest_id = short_digest("feature_manifest", {"feature_id": fid, "input_hash": input_hash, "source_ids": clean_source_ids})
    provenance = build_provenance(
        "market_features",
        clean_source_ids,
        input_ids=[input_hash, *(input_event_ids or []), *(source_manifest_ids or [])],
        metadata={"symbol": symbol.upper(), "feature_family": FEATURE_FAMILY, "manifest_id": manifest_id},
    )
    capability_mask = build_capability_mask(
        provenance,
        missing_optional=missing,
        cutoff_proof=cutoff_proof,
        value_errors=value_errors,
    )
    usable_for_features = provenance_allows_effect(provenance, "feature_input") and capability_mask["action"] != "skip" and cutoff_proof.get("ok", False)
    quarantine_reasons = sorted(set((provenance.get("quarantine_reasons", []) or []) + capability_mask["source_errors"] + capability_mask["cutoff_errors"] + capability_mask["value_errors"]))
    adjusted_confidence = features["feature_confidence"] if usable_for_features else round(features["feature_confidence"] * 0.25, 4)
    artifact = {
        **features,
        "input_hash": input_hash,
        "source_ids": clean_source_ids,
        "source_manifest_ids": source_manifest_ids or [],
        "input_event_ids": input_event_ids or [],
        "decision_cutoff": str(cutoff),
        "latency_buffer_seconds": latency_buffer_seconds,
        "cutoff_proof": cutoff_proof,
        "decision_data_capability_mask": capability_mask,
        "decision_regime_state": decision_regime,
        "fit_metadata": {
            "fit_window": (fit_metadata or {}).get("fit_window", "none_runtime_transform"),
            "fit_cutoff": (fit_metadata or {}).get("fit_cutoff", str(cutoff)),
            "train_partition": (fit_metadata or {}).get("train_partition", "none_deterministic_transform"),
            "input_event_ids": input_event_ids or [],
            "artifact_digest": None,
        },
    }
    artifact_digest = digest_payload({k: v for k, v in artifact.items() if k not in {"computed_at", "fit_metadata"}})
    artifact["fit_metadata"]["artifact_digest"] = artifact_digest
    payload = {
        **artifact,
        "feature_confidence": adjusted_confidence,
        "feature_status": "ok" if usable_for_features else "quarantined",
        "usable_for_paper": bool(usable_for_features),
        "quarantine_reasons": quarantine_reasons,
        "regime": regime,
        "manifest_id": manifest_id,
        "artifact_digest": artifact_digest,
        "input_artifact_digests": {"feature_input_bundle": input_hash},
        "transform": {"name": FEATURE_FAMILY, "version": FEATURE_VERSION, "artifact_digest": artifact_digest},
        "provenance_id": provenance["provenance_id"],
        "source_ids": provenance["source_ids"],
        "source_trust": provenance["source_check"],
        "source_snapshot_hashes": [provenance.get("source_snapshot_hash")],
        "allowed_effect": provenance["allowed_effect"],
        "taint_classes": provenance["taint_classes"],
        "can_place_live_orders": False,
        "live_permission": False,
    }
    if not usable_for_features:
        try:
            from event_store import append_event_envelope

            append_event_envelope(
                "feature.quarantined",
                {"feature_id": payload["feature_id"], "reason": ",".join(quarantine_reasons) or "source_not_usable", "source_ids": payload["source_ids"]},
                "market_feature_store",
                "market_feature_store",
                payload["feature_id"],
            )
        except Exception:
            pass
    else:
        try:
            from event_store import append_event_envelope

            append_event_envelope(
                "feature.row.created",
                {
                    "feature_id": payload["feature_id"],
                    "manifest_id": payload["manifest_id"],
                    "symbol": payload["symbol"],
                    "timeframe": payload["timeframe"],
                    "window_start": payload["window_start"],
                    "window_end": payload["window_end"],
                    "candle_close_time": payload["candle_close_time"],
                    "input_event_ids": payload["input_event_ids"],
                    "artifact_digest": payload["artifact_digest"],
                    "decision_cutoff": payload["decision_cutoff"],
                    "latency_buffer_seconds": payload["latency_buffer_seconds"],
                    "cutoff_proof": payload["cutoff_proof"],
                },
                "market_feature_store",
                "market_feature_store",
                payload["feature_id"],
                provenance_id=payload["provenance_id"],
            )
        except Exception:
            pass
    FEATURE_STORE_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(feature_path(payload["feature_id"]), payload)
    write_json_atomic(REGIME_LATEST, {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "latest_feature_id": payload["feature_id"], "regime": regime})
    return payload
