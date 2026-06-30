"""OpenClaw-style Light/REM/Deep memory consolidation."""
from __future__ import annotations

import hashlib
import json
import argparse
import os
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, append_jsonl_once, read_json, read_jsonl, write_json_atomic
import belief_ledger
from data_trust import evaluate_evidence_for_learning
import dont_do_memory
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
CANDIDATES_JSONL = MEMORY_DIR / "memory_candidates.jsonl"
OVERFLOW_JSONL = MEMORY_DIR / "memory_candidates_overflow.jsonl"
PROMOTED_JSONL = MEMORY_DIR / "memory_promoted.jsonl"
REJECTED_JSONL = MEMORY_DIR / "memory_rejected.jsonl"
SKILL_FORGE_QUEUE_JSONL = MEMORY_DIR / "memory_skill_forge_queue.jsonl"
RETRIEVAL_DIRTY_JSONL = MEMORY_DIR / "memory_retrieval_dirty.jsonl"
LATEST_JSON = MEMORY_DIR / "memory_consolidation_latest.json"
HISTORY_JSONL = MEMORY_DIR / "memory_consolidation_history.jsonl"
PID_FILE = STATE_DIR / "memory_consolidation_agent.pid"
HEARTBEAT_PATH = STATE_DIR / "memory_consolidation_agent_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_MEMORY_CONSOLIDATION_AGENT"
EPISODES_JSONL = MEMORY_DIR / "episodes.jsonl"
POST_TRADE_REVIEWS_JSONL = MEMORY_DIR / "post_trade_reviews.jsonl"
COUNTERFACTUAL_JSONL = MEMORY_DIR / "counterfactual_replays.jsonl"
LEGACY_COUNTERFACTUAL_JSONL = MEMORY_DIR / "counterfactual_replay_history.jsonl"
DAILY_EXAM_HISTORY_JSONL = MEMORY_DIR / "daily_exam_history.jsonl"
TEST_RESULT_MEMORY_JSONL = MEMORY_DIR / "test_result_memory_history.jsonl"
LLM_REASONING_HISTORY_JSONL = MEMORY_DIR / "llm_reasoning_history.jsonl"
BELIEF_LEDGER_PATH = MEMORY_DIR / "belief_ledger.json"
DONT_DO_PATH = MEMORY_DIR / "dont_do_memory.json"
MEMORY_CONTROL_PATH = MEMORY_DIR / "memory_consolidation_control.json"

OBJECTIVE_SOURCES = {"episode", "post_trade_review", "counterfactual", "daily_exam"}
PROMOTION_SOURCES = {"episode", "post_trade_review", "counterfactual", "daily_exam"}
NON_PROMOTABLE_EVAL_SOURCES = {"test_result", "trace_eval", "prompt_eval", "prompt_trace"}
LOW_TRUST_EFFECTS = {"annotation_only", "hypothesis_only", "shadow_only"}
MAX_LIGHT_CANDIDATES = 500
MAX_STAGED_CANDIDATES = 2000
MAX_EVIDENCE_AGE_DAYS = 30

def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))

def payload_hash(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def memory_id(text: str, kind: str = "memory") -> str:
    return f"{kind}_" + hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()[:20]


def first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def source_type(row: dict[str, Any]) -> str:
    explicit = row.get("_memory_source_type") or row.get("source_type") or row.get("source")
    if explicit:
        return str(explicit)
    if row.get("episode_id"):
        return "episode"
    if row.get("review_id"):
        return "post_trade_review"
    if row.get("replay_id"):
        return "counterfactual"
    if row.get("exam_id"):
        return "daily_exam"
    if row.get("test_id"):
        return "test_result"
    if row.get("reasoning_id"):
        return "llm_reasoning"
    return "unknown"


def evidence_id(row: dict[str, Any]) -> str:
    for key in (
        "episode_id",
        "review_id",
        "replay_id",
        "exam_id",
        "test_id",
        "reasoning_id",
        "trade_id",
        "signal_id",
        "memory_event_id",
    ):
        value = row.get(key)
        if value:
            return f"{source_type(row)}:{value}"
    return f"{source_type(row)}:{payload_hash(row)[:28]}"


def objective_event_key(row: dict[str, Any]) -> str:
    for key in ("trade_id", "paper_trade_id", "position_id", "signal_id", "review_id", "replay_id", "episode_id"):
        value = row.get(key)
        if value:
            if key in {"review_id", "replay_id", "episode_id"} and row.get("trade_id"):
                continue
            return f"{key}:{value}"
    return evidence_id(row)


def outcome_known_at(row: dict[str, Any]) -> str:
    return str(first_present(row, "outcome_known_at", "reviewed_at", "closed_at", "close_ts", "completed_at", "ts", "created_at") or utc_now())


def row_context(row: dict[str, Any]) -> dict[str, Any]:
    source_trade = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    costs = row.get("costs") if isinstance(row.get("costs"), dict) else {}
    counterfactual = row.get("counterfactual") if isinstance(row.get("counterfactual"), dict) else {}
    outcome = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
    return {
        "setup_id": first_present(row, "setup_id") or source_trade.get("setup_id"),
        "symbol": str(first_present(row, "symbol") or source_trade.get("symbol") or "").upper() or None,
        "side": str(first_present(row, "side") or source_trade.get("side") or "").upper() or None,
        "regime": first_present(row, "regime", "market_regime") or source_trade.get("regime") or source_trade.get("market_regime"),
        "classification": first_present(row, "classification") or outcome.get("classification"),
        "failure_reason": first_present(row, "primary_failure_reason", "failure_reason", "reason"),
        "mae": first_present(row, "mae") or source_trade.get("mae"),
        "mfe": first_present(row, "mfe") or source_trade.get("mfe"),
        "fee": first_present(row, "fee", "fees") or costs.get("fees"),
        "funding_payment": first_present(row, "funding_payment") or costs.get("funding_payment"),
        "slippage": first_present(row, "slippage") or costs.get("slippage"),
        "counterfactual_conclusion": first_present(row, "counterfactual_conclusion", "conclusion") or counterfactual.get("conclusion"),
    }


def compact_parts(parts: Iterable[Any]) -> str:
    return " | ".join(str(part).strip() for part in parts if part not in (None, "", [], {}))


def claim_text(row: dict[str, Any]) -> str:
    base = ""
    for key in ("lesson", "conclusion", "statement", "primary_failure_reason", "failure_reason"):
        if row.get(key):
            base = str(row[key])
            break
    outcome = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
    if not base and isinstance(outcome.get("lesson"), str):
        base = str(outcome["lesson"])
    critical = row.get("critical_blindspots")
    if not base and isinstance(critical, list) and critical:
        base = "; ".join(str(item) for item in critical[:5])
    if not base:
        base = str(first_present(row, "summary", "next_action", "gap") or "")
    return " ".join(base.split())


def lesson_text(row: dict[str, Any]) -> str:
    ctx = row_context(row)
    base = claim_text(row)
    if not base:
        return ""
    context = compact_parts(
        [
            f"setup={ctx['setup_id']}" if ctx.get("setup_id") else None,
            f"symbol={ctx['symbol']}" if ctx.get("symbol") else None,
            f"side={ctx['side']}" if ctx.get("side") else None,
            f"regime={ctx['regime']}" if ctx.get("regime") else None,
            f"classification={ctx['classification']}" if ctx.get("classification") else None,
            f"reason={ctx['failure_reason']}" if ctx.get("failure_reason") else None,
            f"mae={ctx['mae']}" if ctx.get("mae") is not None else None,
            f"mfe={ctx['mfe']}" if ctx.get("mfe") is not None else None,
            f"fee={ctx['fee']}" if ctx.get("fee") is not None else None,
            f"funding={ctx['funding_payment']}" if ctx.get("funding_payment") is not None else None,
            f"slippage={ctx['slippage']}" if ctx.get("slippage") is not None else None,
            f"counterfactual={ctx['counterfactual_conclusion']}" if ctx.get("counterfactual_conclusion") else None,
        ]
    )
    return compact_parts([base, context])


def evidence_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id(row),
        "source_type": source_type(row),
        "payload_hash": payload_hash(row),
        "outcome_known_at": outcome_known_at(row),
        "trial_partition_id": row.get("trial_partition_id"),
        "readiness_holdout": bool(row.get("readiness_holdout") or row.get("frozen_readiness_holdout_id")),
        "allowed_effect": row.get("allowed_effect"),
        "taint_class": row.get("taint_class"),
    }


def known_evidence_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {evidence_id(row): {"hash": payload_hash(row), "row": row, "record": evidence_record(row)} for row in rows}


def light_sleep(rows: list[dict[str, Any]], max_candidates: int = MAX_LIGHT_CANDIDATES) -> list[dict[str, Any]]:
    seen: set[str] = set()
    candidates = []
    for row in rows:
        text = " ".join(lesson_text(row).split())
        claim = " ".join(claim_text(row).split())
        if not text:
            continue
        mid = memory_id(claim or text, "candidate")
        if mid in seen:
            continue
        seen.add(mid)
        record = evidence_record(row)
        candidates.append(
            {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": mid,
                "text": text,
                "claim": claim or text,
                "kind": "trade_lesson" if row.get("trade_id") or row.get("review_id") else "episode_lesson",
                "source_ids": [record["evidence_id"]],
                "evidence_ids": [record["evidence_id"]],
                "evidence": [record],
                "created_at": utc_now(),
                "memory_created_at": utc_now(),
                "source_cutoff_proof": {"outcome_known_at": record["outcome_known_at"], "payload_hash": record["payload_hash"]},
                "trial_partition_id": row.get("trial_partition_id"),
                "raw": row,
            }
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def rem_extract_patterns(candidates: list[dict[str, Any]], all_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    contexts: dict[str, set[str]] = defaultdict(set)
    trade_samples: Counter[str] = Counter()
    contradictions: Counter[str] = Counter()
    evidence_by_key: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    days: dict[str, set[str]] = defaultdict(set)
    symbols: dict[str, set[str]] = defaultdict(set)
    regimes: dict[str, set[str]] = defaultdict(set)
    source_types: dict[str, set[str]] = defaultdict(set)
    objective_events: dict[str, set[str]] = defaultdict(set)
    for row in all_rows:
        text = lesson_text(row)
        claim = claim_text(row)
        if not text:
            continue
        key = memory_id(claim or text, "candidate")
        ctx = row_context(row)
        record = evidence_record(row)
        counts[key] += 1
        contexts[key].add(str(row.get("trigger") or row.get("symbol") or row.get("setup_id") or row.get("classification") or "unknown"))
        evidence_by_key[key][record["evidence_id"]] = record
        day = (parse_utc(record["outcome_known_at"]) or parse_utc(utc_now()))
        if day:
            days[key].add(day.date().isoformat())
        if ctx.get("symbol"):
            symbols[key].add(str(ctx["symbol"]))
        if ctx.get("regime"):
            regimes[key].add(str(ctx["regime"]))
        source_types[key].add(source_type(row))
        objective_events[key].add(objective_event_key(row))
        if row.get("trade_id") or row.get("review_id"):
            trade_samples[key] += 1
        if row.get("contradicts_memory_id") or row.get("evidence_polarity") == "against":
            contradictions[key] += 1
        if str(row.get("classification") or "") in {"bad_win", "bad_loss"} and "good" in text.lower():
            contradictions[key] += 1
    enriched = []
    base = {row["candidate_id"]: row for row in candidates}
    for key, item in base.items():
        score = min(1.0, 0.25 + counts[key] * 0.18 + len(contexts[key]) * 0.12 + trade_samples[key] * 0.08 - contradictions[key] * 0.25)
        evidence = list(evidence_by_key[key].values())
        enriched.append(
            {
                **item,
                "source_ids": [row["evidence_id"] for row in evidence],
                "evidence_ids": [row["evidence_id"] for row in evidence],
                "evidence": evidence,
                "recall_count": counts[key],
                "unique_contexts": len(contexts[key]),
                "unique_days": len(days[key]),
                "unique_symbols": len(symbols[key]),
                "unique_regimes": len(regimes[key]),
                "source_quorum": len(source_types[key]),
                "independent_evidence_count": len(objective_events[key]),
                "trade_samples": trade_samples[key],
                "contradiction_count": contradictions[key],
                "evidence_outcome_known_at": max([row["outcome_known_at"] for row in evidence], default=None),
                "confidence_score": round(score, 4),
            }
        )
    return enriched


def resolve_evidence(
    item: dict[str, Any],
    evidence_index: dict[str, dict[str, Any]] | None = None,
    promotion_cutoff: str | None = None,
    trial_partition_id: str | None = None,
    no_holdout: bool = True,
    max_evidence_age_days: int = MAX_EVIDENCE_AGE_DAYS,
) -> dict[str, Any]:
    errors: list[str] = []
    evidence_rows = item.get("evidence") if isinstance(item.get("evidence"), list) else []
    candidate_claim = " ".join(str(item.get("claim") or item.get("text") or "").split()).lower()
    if not evidence_rows:
        return {"errors": ["missing_evidence_records"], "metrics": {}, "evidence": []}
    if evidence_index is None:
        return {"errors": ["missing_evidence_index"], "metrics": {}, "evidence": []}
    cutoff = parse_utc(promotion_cutoff or utc_now())
    canonical_rows: list[dict[str, Any]] = []
    canonical_records: list[dict[str, Any]] = []
    for evidence in evidence_rows:
        if not isinstance(evidence, dict):
            errors.append("invalid_evidence_record")
            continue
        eid = str(evidence.get("evidence_id") or "")
        if not eid:
            errors.append("missing_evidence_id")
            continue
        if not evidence.get("payload_hash"):
            errors.append(f"missing_evidence_payload_hash:{eid}")
        indexed = evidence_index.get(eid)
        if not indexed:
            errors.append(f"evidence_id_not_found:{eid}")
            continue
        hash_ok = evidence.get("payload_hash") == indexed.get("hash")
        if not hash_ok:
            errors.append(f"evidence_hash_mismatch:{eid}")
        source_row = indexed.get("row") if isinstance(indexed.get("row"), dict) else {}
        canonical_record = indexed.get("record") if isinstance(indexed.get("record"), dict) else evidence_record(source_row)
        if evidence.get("source_type") and evidence.get("source_type") != canonical_record.get("source_type"):
            errors.append(f"evidence_source_type_mismatch:{eid}")
        source_claim = " ".join(claim_text(source_row).split()).lower()
        if not source_claim:
            errors.append(f"missing_source_claim:{eid}")
        claim_ok = bool(source_claim) and (not candidate_claim or candidate_claim == source_claim)
        if source_claim and candidate_claim and candidate_claim != source_claim:
            errors.append(f"evidence_claim_mismatch:{eid}")
        trust = evaluate_evidence_for_learning(source_row, requested_effect="memory_promotion")
        if not trust.get("ok"):
            errors.extend(f"{error}:{eid}" for error in (trust.get("errors") or []))
        known_at = parse_utc(canonical_record.get("outcome_known_at"))
        if not known_at:
            errors.append(f"invalid_evidence_outcome_known_at:{eid}")
        if cutoff and known_at and known_at > cutoff:
            errors.append(f"evidence_after_promotion_cutoff:{eid}")
        if cutoff and known_at and (cutoff - known_at).days > max_evidence_age_days:
            errors.append(f"stale_evidence_ttl_expired:{eid}")
        if trial_partition_id and canonical_record.get("trial_partition_id") and canonical_record.get("trial_partition_id") != trial_partition_id:
            errors.append(f"wrong_trial_partition:{eid}")
        if no_holdout and canonical_record.get("readiness_holdout"):
            errors.append(f"readiness_holdout_evidence_forbidden:{eid}")
        allowed_effect = str(canonical_record.get("allowed_effect") or "")
        source = str(canonical_record.get("source_type") or "")
        taint_class = str(canonical_record.get("taint_class") or "")
        if source in NON_PROMOTABLE_EVAL_SOURCES:
            errors.append(f"eval_source_cannot_promote:{eid}")
        if source not in PROMOTION_SOURCES:
            errors.append(f"source_type_cannot_promote:{eid}")
        if taint_class in {"external_social", "manual_claim", "llm_generated", "private_external", "operator_feedback"}:
            errors.append(f"tainted_evidence_cannot_promote:{eid}")
        if allowed_effect in LOW_TRUST_EFFECTS and source not in OBJECTIVE_SOURCES:
            errors.append(f"low_trust_evidence_cannot_promote:{eid}")
        if hash_ok and claim_ok:
            canonical_rows.append(source_row)
            canonical_records.append(canonical_record)
    contexts = {
        str(row.get("trigger") or row.get("symbol") or row.get("setup_id") or row.get("classification") or "unknown")
        for row in canonical_rows
    }
    days = {
        parsed.date().isoformat()
        for record in canonical_records
        if (parsed := parse_utc(record.get("outcome_known_at")))
    }
    source_types = {source_type(row) for row in canonical_rows}
    objective_events = {objective_event_key(row) for row in canonical_rows}
    trade_samples = sum(1 for row in canonical_rows if row.get("trade_id") or row.get("review_id"))
    contradiction_count = sum(
        1
        for row in canonical_rows
        if row.get("contradicts_memory_id") or row.get("evidence_polarity") == "against"
    )
    score = min(
        1.0,
        0.25
        + len(canonical_rows) * 0.18
        + len(contexts) * 0.12
        + trade_samples * 0.08
        - contradiction_count * 0.25,
    )
    metrics = {
        "recall_count": len(canonical_rows),
        "unique_contexts": len(contexts),
        "unique_days": len(days),
        "source_quorum": len(source_types),
        "independent_evidence_count": len(objective_events),
        "trade_samples": trade_samples,
        "contradiction_count": contradiction_count,
        "confidence_score": round(score, 4) if canonical_rows else 0.0,
        "source_ids": [record["evidence_id"] for record in canonical_records],
        "evidence_ids": [record["evidence_id"] for record in canonical_records],
    }
    return {"errors": sorted(set(errors)), "metrics": metrics, "evidence": canonical_records}


def promotion_control_errors(control_path: Path = MEMORY_CONTROL_PATH) -> list[str]:
    control = read_json(control_path, default={})
    errors: list[str] = []
    if control.get("active_trial_freeze"):
        errors.append("active_trial_freeze_blocks_memory_promotion")
    budget = control.get("budget") if isinstance(control.get("budget"), dict) else {}
    if control.get("degraded_mode") or budget.get("status") in {"exhausted", "degraded"}:
        errors.append("memory_budget_degraded_blocks_promotion")
    return errors


def should_make_dont_do(memory: dict[str, Any]) -> bool:
    text = str(memory.get("text") or "").lower()
    return any(token in text for token in ("avoid", "do not", "don't", "khong", "không", "bad_loss", "chase", "too wide", "high risk", "block"))


def apply_promoted_consumers(memory: dict[str, Any]) -> dict[str, Any]:
    effects: list[dict[str, Any]] = []
    ledger = belief_ledger.load_ledger(BELIEF_LEDGER_PATH)
    belief = belief_ledger.upsert_belief(
        ledger,
        str(memory.get("text") or ""),
        scope=str(memory.get("kind") or "memory"),
        topic="memory_consolidation",
        confidence=min(0.85, max(0.5, float(memory.get("confidence_score") or 0.5))),
        metadata={"memory_id": memory.get("memory_id"), "source_ids": memory.get("source_ids") or []},
    )
    belief_ledger.add_evidence(
        ledger,
        belief["belief_id"],
        "for",
        max(1.0, float(memory.get("recall_count") or 1)),
        "memory_consolidation",
        str(memory.get("text") or ""),
        event_id=str(memory.get("memory_id") or ""),
        metadata={"evidence_ids": memory.get("evidence_ids") or []},
    )
    belief_ledger.save_ledger(ledger, BELIEF_LEDGER_PATH)
    effects.append({"consumer": "belief_ledger", "belief_id": belief["belief_id"]})
    if should_make_dont_do(memory):
        rule = dont_do_memory.add_or_update_rule(
            str(memory.get("text") or ""),
            scope=str(memory.get("kind") or "memory"),
            severity="high" if "bad_loss" in str(memory.get("text") or "").lower() or "avoid" in str(memory.get("text") or "").lower() else "medium",
            evidence_delta=max(1, int(memory.get("recall_count") or 1)),
            evidence_ids=[str(item) for item in memory.get("evidence_ids", []) if item] if isinstance(memory.get("evidence_ids"), list) else [],
            path=DONT_DO_PATH,
        )
        effects.append({"consumer": "dont_do_memory", "rule_id": rule.get("rule_id")})
    skill_task = {
        "schema_version": SCHEMA_VERSION,
        "queued_at": utc_now(),
        "memory_id": memory.get("memory_id"),
        "claim": memory.get("claim"),
        "text": memory.get("text"),
        "evidence_ids": memory.get("evidence_ids") or [],
        "allowed_effect": "paper_skill_candidate",
        "can_place_live_orders": False,
    }
    append_jsonl_once(SKILL_FORGE_QUEUE_JSONL, skill_task, "memory_id")
    effects.append({"consumer": "skill_forge_queue", "memory_id": memory.get("memory_id")})
    retrieval_task = {
        "schema_version": SCHEMA_VERSION,
        "queued_at": utc_now(),
        "memory_id": memory.get("memory_id"),
        "action": "rebuild_or_upsert_promoted_memory",
    }
    append_jsonl_once(RETRIEVAL_DIRTY_JSONL, retrieval_task, "memory_id")
    effects.append({"consumer": "retrieval_index_dirty", "memory_id": memory.get("memory_id")})
    return {"consumer_effects": effects, "deterministic_consumer_impact": bool(effects)}


def learning_claim(memory: dict[str, Any]) -> dict[str, Any]:
    effects = memory.get("consumer_effects") if isinstance(memory.get("consumer_effects"), list) else []
    changed_ids = [str(effect.get("belief_id") or effect.get("rule_id") or effect.get("memory_id")) for effect in effects if effect.get("belief_id") or effect.get("rule_id") or effect.get("memory_id")]
    claim_type = "learned" if changed_ids and memory.get("deterministic_consumer_impact") else "hypothesis_only"
    return {
        "schema_version": SCHEMA_VERSION,
        "claim_type": claim_type,
        "memory_id": memory.get("memory_id"),
        "changed_ids": sorted(set(changed_ids)),
        "evidence_ids": memory.get("evidence_ids") or [],
        "deterministic_consumer_impact": bool(changed_ids and memory.get("deterministic_consumer_impact")),
    }


@contextmanager
def promotion_file_lock(path: Path, timeout_seconds: float = 10.0):
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_seconds
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            if time.time() >= deadline:
                raise TimeoutError(f"timed out waiting for promotion lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def finalize_promotion_once(memory: dict[str, Any], apply_consumers: bool = True) -> tuple[bool, dict[str, Any], list[str]]:
    with promotion_file_lock(PROMOTED_JSONL):
        existing = {row.get("memory_id") for row in read_jsonl(PROMOTED_JSONL) if row.get("memory_id")}
        if memory["memory_id"] in existing:
            return False, memory, ["duplicate_existing_memory"]
        if not apply_consumers:
            memory["consumer_effects"] = []
            memory["deterministic_consumer_impact"] = False
            memory["learning_claim"] = learning_claim(memory)
            return False, memory, ["learning_claim_without_deterministic_consumer_impact"]
        memory.update(apply_promoted_consumers(memory))
        memory["learning_claim"] = learning_claim(memory)
        if memory["learning_claim"]["claim_type"] != "learned":
            return False, memory, ["learning_claim_without_deterministic_consumer_impact"]
        if not append_jsonl_once(PROMOTED_JSONL, memory, "memory_id"):
            return False, memory, ["duplicate_existing_memory"]
        return True, memory, []


def deep_promote(
    candidates: list[dict[str, Any]],
    min_recall_count: int = 2,
    min_unique_contexts: int = 2,
    min_trade_samples: int = 0,
    min_confidence: float = 0.65,
    min_unique_days: int = 1,
    min_source_quorum: int = 2,
    min_independent_evidence: int = 2,
    max_contradictions: int = 0,
    evidence_index: dict[str, dict[str, Any]] | None = None,
    promotion_cutoff: str | None = None,
    trial_partition_id: str | None = None,
    no_holdout: bool = True,
    apply_consumers: bool = True,
    control_path: Path | None = None,
) -> dict[str, Any]:
    promoted = []
    rejected = []
    control_errors = promotion_control_errors(control_path or MEMORY_CONTROL_PATH)
    for item in candidates:
        errors = list(control_errors)
        resolution = resolve_evidence(item, evidence_index=evidence_index, promotion_cutoff=promotion_cutoff, trial_partition_id=trial_partition_id, no_holdout=no_holdout)
        errors.extend(resolution["errors"])
        metrics = dict(resolution.get("metrics") or {})
        if metrics.get("recall_count", 0) < min_recall_count:
            errors.append("insufficient_recall_count")
        if metrics.get("unique_contexts", 0) < min_unique_contexts:
            errors.append("insufficient_unique_contexts")
        if metrics.get("trade_samples", 0) < min_trade_samples:
            errors.append("insufficient_trade_samples")
        if metrics.get("unique_days", 0) < min_unique_days:
            errors.append("insufficient_unique_days")
        if metrics.get("source_quorum", 0) < min_source_quorum:
            errors.append("insufficient_source_quorum")
        if metrics.get("independent_evidence_count", 0) < min_independent_evidence:
            errors.append("insufficient_independent_evidence")
        if metrics.get("contradiction_count", 0) > max_contradictions:
            errors.append("contradicted_by_evidence")
        if metrics.get("confidence_score", 0.0) < min_confidence:
            errors.append("low_confidence")
        evidence_trust = evaluate_evidence_for_learning(item.get("raw") if isinstance(item.get("raw"), dict) else {}, requested_effect="memory_promotion")
        errors.extend(evidence_trust.get("errors", []))
        stable_claim = str(item.get("claim") or item.get("text") or "")
        memory = {
            **item,
            **metrics,
            "evidence": resolution.get("evidence") or [],
            "memory_id": memory_id(stable_claim, "memory"),
            "memory_promoted_at": utc_now(),
            "promoted_at": utc_now(),
            "ttl_days": 30,
            "promotion_cutoff": promotion_cutoff or utc_now(),
            "trial_partition_id": trial_partition_id or item.get("trial_partition_id"),
        }
        memory["evidence_trust"] = evidence_trust
        if errors:
            rejected_row = {**memory, "rejected_at": utc_now(), "errors": errors}
            append_jsonl_once(REJECTED_JSONL, rejected_row, "candidate_id")
            rejected.append(rejected_row)
        else:
            promoted_once, memory, promotion_errors = finalize_promotion_once(memory, apply_consumers=apply_consumers)
            if not promoted_once:
                rejected_row = {**memory, "rejected_at": utc_now(), "errors": promotion_errors}
                append_jsonl_once(REJECTED_JSONL, rejected_row, "candidate_id")
                rejected.append(rejected_row)
                continue
            promoted.append(memory)
    summary = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "candidate_count": len(candidates), "promoted_count": len(promoted), "rejected_count": len(rejected), "promoted": promoted[:20], "rejected": rejected[:20]}
    write_json_atomic(LATEST_JSON, summary)
    return summary


def append_bounded_candidates(candidates: list[dict[str, Any]], max_staged: int = MAX_STAGED_CANDIDATES) -> dict[str, Any]:
    existing = read_jsonl(CANDIDATES_JSONL)
    existing_ids = {row.get("candidate_id") for row in existing if row.get("candidate_id")}
    quarantined_ids = {row.get("candidate_id") for row in read_jsonl(OVERFLOW_JSONL) if row.get("candidate_id")}
    staged_count = len(existing)
    appended = 0
    overflowed = 0
    staged_ids: list[str] = []
    overflowed_ids: list[str] = []
    source_counts: Counter[str] = Counter(str(row.get("source_type") or row.get("raw", {}).get("_memory_source_type") or "unknown") for row in existing)
    per_source_cap = max(1, max_staged // 4)
    for candidate in candidates:
        cid = candidate.get("candidate_id")
        source = str((candidate.get("evidence") or [{}])[0].get("source_type") if isinstance(candidate.get("evidence"), list) and candidate.get("evidence") else candidate.get("raw", {}).get("_memory_source_type") or "unknown")
        if cid in quarantined_ids:
            overflowed += 1
            overflowed_ids.append(str(cid))
            continue
        if cid in existing_ids:
            staged_ids.append(str(cid))
            continue
        if staged_count >= max_staged or source_counts[source] >= per_source_cap:
            append_jsonl_once(OVERFLOW_JSONL, {**candidate, "overflowed_at": utc_now(), "reason": "memory_candidate_storage_cap", "max_staged": max_staged, "per_source_cap": per_source_cap}, "candidate_id")
            overflowed += 1
            overflowed_ids.append(str(cid))
            continue
        append_jsonl_once(CANDIDATES_JSONL, candidate, "candidate_id")
        existing_ids.add(cid)
        staged_ids.append(str(cid))
        source_counts[source] += 1
        staged_count += 1
        appended += 1
    return {"staged_existing": len(existing), "staged_appended": appended, "staged_ids": staged_ids, "overflowed": overflowed, "overflowed_ids": overflowed_ids, "max_staged": max_staged, "per_source_cap": per_source_cap}


def consolidate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = rem_extract_patterns(light_sleep(rows), rows)
    storage = append_bounded_candidates(candidates)
    staged = {str(candidate_id) for candidate_id in storage.get("staged_ids", [])}
    promotable = [candidate for candidate in candidates if str(candidate.get("candidate_id")) in staged]
    summary = deep_promote(promotable, evidence_index=known_evidence_index(rows), promotion_cutoff=utc_now())
    return {**summary, "candidate_storage": storage}

def collect_learning_rows(limit_per_file: int = 400) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    specs = [
        ("episode", EPISODES_JSONL),
        ("post_trade_review", POST_TRADE_REVIEWS_JSONL),
        ("counterfactual", COUNTERFACTUAL_JSONL),
        ("counterfactual", LEGACY_COUNTERFACTUAL_JSONL),
        ("daily_exam", DAILY_EXAM_HISTORY_JSONL),
        ("test_result", TEST_RESULT_MEMORY_JSONL),
        ("llm_reasoning", LLM_REASONING_HISTORY_JSONL),
    ]
    seen: set[str] = set()
    for source, path in specs:
        for row in read_jsonl(path)[-limit_per_file:]:
            if isinstance(row, dict):
                enriched = {**row, "_memory_source_type": source, "_memory_source_path": str(path)}
                key = evidence_id(enriched)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(enriched)
    return rows

def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json_atomic(HEARTBEAT_PATH, row)
    return row

def run_once(limit_per_file: int = 400) -> dict[str, Any]:
    rows = collect_learning_rows(limit_per_file=limit_per_file)
    summary = consolidate(rows)
    payload = {
        **summary,
        "source_row_count": len(rows),
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    write_json_atomic(LATEST_JSON, payload)
    append_jsonl(HISTORY_JSONL, payload)
    write_heartbeat("ok", {"source_row_count": len(rows), "promoted_count": payload.get("promoted_count"), "rejected_count": payload.get("rejected_count")})
    return payload

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run OpenClaw-style memory consolidation loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=1800.0)
    parser.add_argument("--limit-per-file", type=int, default=400)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.limit_per_file <= 0:
        parser.error("--limit-per-file must be positive")
    return args

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        row = run_once(limit_per_file=args.limit_per_file)
        print(f"memory_consolidation_agent promoted={row.get('promoted_count')} rejected={row.get('rejected_count')}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
