"""Provenance helpers for learning artifacts."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl
from data_source_registry import evaluate_sources, load_source_registry
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
PROVENANCE_LOG = STATE_DIR / "source_provenance.jsonl"


def stable_data_id(kind: str, payload: dict[str, Any]) -> str:
    raw = repr(sorted(payload.items()))
    return f"{kind}_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def build_provenance(
    artifact_kind: str,
    source_ids: list[str],
    input_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check = evaluate_sources(source_ids, registry or load_source_registry())
    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": artifact_kind,
        "source_ids": source_ids,
        "input_ids": input_ids or [],
        "metadata": metadata or {},
        "source_check": check,
        "created_at": utc_now(),
    }
    payload["provenance_id"] = stable_data_id("prov", payload)
    return payload


def attach_provenance(artifact: dict[str, Any], provenance: dict[str, Any], log_path: Path = PROVENANCE_LOG) -> dict[str, Any]:
    row = {**artifact, "provenance_id": provenance.get("provenance_id"), "source_ids": provenance.get("source_ids", [])}
    append_jsonl(log_path, provenance)
    return row


def require_usable_sources(provenance: dict[str, Any]) -> None:
    check = provenance.get("source_check") or {}
    if not check.get("usable"):
        raise ValueError(f"unusable_sources: {check}")
