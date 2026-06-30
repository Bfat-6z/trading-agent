"""Separate human feedback ledger that cannot overwrite market outcomes."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from data_trust import classify_human_feedback, sanitize_external_text
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
FEEDBACK_JSONL = MEMORY_DIR / "human_feedback.jsonl"
FEEDBACK_LATEST = MEMORY_DIR / "human_feedback_latest.json"

ALLOWED_TYPES = {"setup_label_correction", "reason_correction", "source_trust_adjustment", "this_was_chase", "this_was_valid_loss", "manual_aplus_thesis", "reject_hallucinated_lesson"}


def feedback_id(target_id: str, feedback_type: str, text: str) -> str:
    return "feedback_" + hashlib.sha256(f"{target_id}:{feedback_type}:{text}".encode("utf-8")).hexdigest()[:20]


def record_feedback(target_id: str, feedback_type: str, text: str, user_id: str = "operator", path: Path = FEEDBACK_JSONL, latest_path: Path = FEEDBACK_LATEST, event_db_path: Path | None = None) -> dict[str, Any]:
    errors = []
    if feedback_type not in ALLOWED_TYPES:
        errors.append("unknown_feedback_type")
    classification = classify_human_feedback(text)
    sanitized = sanitize_external_text(text)
    if classification["panic_revenge"]:
        errors.append("panic_revenge_feedback_rejected")
    row = {
        "schema_version": SCHEMA_VERSION,
        "feedback_id": feedback_id(target_id, feedback_type, text),
        "ts": utc_now(),
        "target_id": target_id,
        "feedback_type": feedback_type,
        "text": sanitized["text"],
        "text_hash": sanitized["content_hash"],
        "sanitize_flags": sanitized["flags"],
        "classification": classification,
        "allowed_effect": classification["allowed_effect"],
        "taint_class": classification["taint_class"],
        "learning_weight": classification["learning_weight"],
        "user_id": user_id,
        "errors": errors,
        "objective_metrics_mutable": False,
    }
    if not errors:
        append_jsonl_once(path, row, "feedback_id")
    elif event_db_path is not None:
        try:
            from event_store import append_event_envelope

            append_event_envelope(
                "human_feedback.rejected",
                {"feedback_id": row["feedback_id"], "reason": ";".join(errors), "text_hash": row["text_hash"], "taint_class": row["taint_class"]},
                "human_feedback_ledger",
                "human_feedback_ledger",
                row["feedback_id"],
                db_path=event_db_path,
            )
        except Exception:
            pass
    summary = summarize_feedback(path)
    summary["last_feedback"] = row
    write_json_atomic(latest_path, summary)
    return row


def summarize_feedback(path: Path = FEEDBACK_JSONL) -> dict[str, Any]:
    rows = read_jsonl(path)
    by_target: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for row in rows:
        by_target[str(row.get("target_id"))] = by_target.get(str(row.get("target_id")), 0) + 1
        by_type[str(row.get("feedback_type"))] = by_type.get(str(row.get("feedback_type")), 0) + 1
    return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "feedback_count": len(rows), "by_target": by_target, "by_type": by_type}
