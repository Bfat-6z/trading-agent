"""OpenClaw-style Light/REM/Deep memory consolidation."""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
CANDIDATES_JSONL = MEMORY_DIR / "memory_candidates.jsonl"
PROMOTED_JSONL = MEMORY_DIR / "memory_promoted.jsonl"
REJECTED_JSONL = MEMORY_DIR / "memory_rejected.jsonl"
LATEST_JSON = MEMORY_DIR / "memory_consolidation_latest.json"


def memory_id(text: str, kind: str = "memory") -> str:
    return f"{kind}_" + hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()[:20]


def lesson_text(row: dict[str, Any]) -> str:
    for key in ("lesson", "classification", "conclusion", "statement"):
        if row.get(key):
            return str(row[key])
    outcome = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
    if outcome.get("classification"):
        return str(outcome["classification"])
    return ""


def light_sleep(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    candidates = []
    for row in rows:
        text = " ".join(lesson_text(row).split())
        if not text:
            continue
        mid = memory_id(text, "candidate")
        if mid in seen:
            continue
        seen.add(mid)
        candidates.append(
            {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": mid,
                "text": text,
                "kind": "trade_lesson" if row.get("trade_id") or row.get("review_id") else "episode_lesson",
                "source_ids": [str(row.get("episode_id") or row.get("review_id") or row.get("replay_id") or row.get("trade_id") or mid)],
                "created_at": utc_now(),
                "raw": row,
            }
        )
    return candidates


def rem_extract_patterns(candidates: list[dict[str, Any]], all_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    contexts: dict[str, set[str]] = defaultdict(set)
    trade_samples: Counter[str] = Counter()
    contradictions: Counter[str] = Counter()
    for row in all_rows:
        text = lesson_text(row)
        if not text:
            continue
        key = memory_id(text, "candidate")
        counts[key] += 1
        contexts[key].add(str(row.get("trigger") or row.get("symbol") or row.get("setup_id") or row.get("classification") or "unknown"))
        if row.get("trade_id") or row.get("review_id"):
            trade_samples[key] += 1
        if str(row.get("classification") or "") in {"bad_win", "bad_loss"} and "good" in text.lower():
            contradictions[key] += 1
    enriched = []
    base = {row["candidate_id"]: row for row in candidates}
    for key, item in base.items():
        score = min(1.0, 0.25 + counts[key] * 0.18 + len(contexts[key]) * 0.12 + trade_samples[key] * 0.08 - contradictions[key] * 0.25)
        enriched.append({**item, "recall_count": counts[key], "unique_contexts": len(contexts[key]), "trade_samples": trade_samples[key], "contradiction_count": contradictions[key], "confidence_score": round(score, 4)})
    return enriched


def deep_promote(candidates: list[dict[str, Any]], min_recall_count: int = 2, min_unique_contexts: int = 2, min_trade_samples: int = 0, min_confidence: float = 0.65) -> dict[str, Any]:
    promoted = []
    rejected = []
    existing = {row.get("memory_id") for row in read_jsonl(PROMOTED_JSONL) if row.get("memory_id")}
    for item in candidates:
        errors = []
        if item.get("recall_count", 0) < min_recall_count:
            errors.append("insufficient_recall_count")
        if item.get("unique_contexts", 0) < min_unique_contexts:
            errors.append("insufficient_unique_contexts")
        if item.get("trade_samples", 0) < min_trade_samples:
            errors.append("insufficient_trade_samples")
        if item.get("contradiction_count", 0) > 0:
            errors.append("contradicted_by_evidence")
        if item.get("confidence_score", 0.0) < min_confidence:
            errors.append("low_confidence")
        memory = {**item, "memory_id": memory_id(item["text"], "memory"), "promoted_at": utc_now()}
        if memory["memory_id"] in existing:
            errors.append("duplicate_existing_memory")
        if errors:
            rejected_row = {**memory, "rejected_at": utc_now(), "errors": errors}
            append_jsonl_once(REJECTED_JSONL, rejected_row, "candidate_id")
            rejected.append(rejected_row)
        else:
            append_jsonl_once(PROMOTED_JSONL, memory, "memory_id")
            promoted.append(memory)
    summary = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "candidate_count": len(candidates), "promoted_count": len(promoted), "rejected_count": len(rejected), "promoted": promoted[:20], "rejected": rejected[:20]}
    write_json_atomic(LATEST_JSON, summary)
    return summary


def consolidate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = rem_extract_patterns(light_sleep(rows), rows)
    for row in candidates:
        append_jsonl_once(CANDIDATES_JSONL, row, "candidate_id")
    return deep_promote(candidates)
