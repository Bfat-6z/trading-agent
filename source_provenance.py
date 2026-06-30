"""Provenance helpers for learning artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl
from data_source_registry import evaluate_sources, load_source_registry
from data_trust import allows_effect
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
PROVENANCE_LOG = STATE_DIR / "source_provenance.jsonl"


def stable_data_id(kind: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))
    return f"{kind}_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def stable_source_snapshot(check: dict[str, Any]) -> str:
    rows = []
    for row in check.get("sources", []) if isinstance(check.get("sources"), list) else []:
        rows.append(
            {
                "source_id": row.get("source_id"),
                "provider": row.get("provider"),
                "source_type": row.get("source_type"),
                "usable": row.get("usable"),
                "quality_score": row.get("quality_score"),
                "trust_score": row.get("trust_score"),
                "parse_confidence": row.get("parse_confidence"),
                "allowed_effect": row.get("allowed_effect"),
                "taint_class": row.get("taint_class"),
                "rights": row.get("rights"),
                "errors": row.get("errors") or [],
                "warnings": row.get("warnings") or [],
                "quota_used": row.get("quota_used"),
                "quota_limit": row.get("quota_limit"),
            }
        )
    return "sha256:" + hashlib.sha256(json.dumps(rows, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


def build_provenance(
    artifact_kind: str,
    source_ids: list[str],
    input_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check = evaluate_sources(source_ids, registry or load_source_registry())
    source_snapshot_hash = stable_source_snapshot(check)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": artifact_kind,
        "source_ids": source_ids,
        "input_ids": input_ids or [],
        "metadata": metadata or {},
        "source_check": check,
        "source_snapshot_hash": source_snapshot_hash,
        "allowed_effect": check.get("allowed_effect", "deny"),
        "taint_classes": check.get("taint_classes", []),
        "provenance_status": "ok" if check.get("usable") else "quarantined",
        "quarantine_reasons": sorted({error for row in check.get("sources", []) for error in row.get("errors", [])}),
        "created_at": utc_now(),
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    payload["provenance_id"] = stable_data_id(
        "prov",
        {
            "artifact_kind": artifact_kind,
            "source_ids": sorted(source_ids),
            "input_ids": sorted(input_ids or []),
            "metadata": metadata or {},
            "source_snapshot_hash": source_snapshot_hash,
        },
    )
    return payload


def attach_provenance(artifact: dict[str, Any], provenance: dict[str, Any], log_path: Path = PROVENANCE_LOG) -> dict[str, Any]:
    row = {
        **artifact,
        "provenance_id": provenance.get("provenance_id"),
        "source_ids": provenance.get("source_ids", []),
        "source_trust": provenance.get("source_check", {}),
        "allowed_effect": provenance.get("allowed_effect", "deny"),
        "taint_classes": provenance.get("taint_classes", []),
        "provenance_status": provenance.get("provenance_status"),
    }
    append_jsonl(log_path, provenance)
    return row


def provenance_allows_effect(provenance: dict[str, Any], requested_effect: str) -> bool:
    return bool((provenance.get("source_check") or {}).get("usable")) and allows_effect(str(provenance.get("allowed_effect") or "deny"), requested_effect)


def require_usable_sources(provenance: dict[str, Any], requested_effect: str | None = None) -> None:
    check = provenance.get("source_check") or {}
    if not check.get("usable"):
        raise ValueError(f"unusable_sources: {check}")
    if requested_effect and not provenance_allows_effect(provenance, requested_effect):
        raise ValueError(f"source_effect_not_allowed:{provenance.get('allowed_effect')}->{requested_effect}")
