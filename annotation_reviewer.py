"""Review human feedback conflicts without mutating objective outcomes."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_jsonl, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
ANNOTATION_LATEST = MEMORY_DIR / "annotation_review_latest.json"

CONFLICT_PAIRS = [{"this_was_chase", "this_was_valid_loss"}, {"manual_aplus_thesis", "reject_hallucinated_lesson"}]


def review_annotations(path: Path = MEMORY_DIR / "human_feedback.jsonl", output_path: Path = ANNOTATION_LATEST) -> dict[str, Any]:
    rows = read_jsonl(path)
    by_target: dict[str, set[str]] = {}
    for row in rows:
        by_target.setdefault(str(row.get("target_id")), set()).add(str(row.get("feedback_type")))
    conflicts = []
    for target, types in by_target.items():
        for pair in CONFLICT_PAIRS:
            if pair.issubset(types):
                conflicts.append({"target_id": target, "conflict": sorted(pair)})
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "feedback_count": len(rows), "conflict_count": len(conflicts), "conflicts": conflicts, "objective_metrics_mutated": False}
    write_json_atomic(output_path, payload)
    return payload
