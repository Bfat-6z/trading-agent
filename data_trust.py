"""Source provenance, taint, and egress policy helpers.

This module is deterministic and local-only. It classifies external data so
social/manual/LLM text cannot silently become alpha, risk loosening, or prompt
payload without proof.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from timebase import seconds_between, utc_now

EFFECT_ORDER = {
    "deny": 0,
    "annotation_only": 1,
    "hypothesis_only": 2,
    "shadow_only": 3,
    "risk_tighten_only": 4,
    "feature_input": 5,
}

SOCIAL_SOURCE_TYPES = {"social", "telegram", "reddit", "x", "twitter", "discord", "forum"}
MANUAL_SOURCE_TYPES = {"manual", "manual_text", "manual_screenshot", "screenshot", "operator_text"}
NEWS_SOURCE_TYPES = {"news", "rss", "news_api"}
MARKET_SOURCE_TYPES = {
    "state",
    "market",
    "market_candles",
    "orderbook",
    "liquidation",
    "funding",
    "open_interest",
    "derivatives",
    "exchange_info",
    "instrument",
}
PRIVATE_RIGHTS = {"private", "protected", "confidential"}
PRIVATE_MARKER_KEYS = {"metadata", "meta", "source", "provenance", "policy", "rights"}
LLM_BLOCKED_TEXT_TAINTS = {"external_social", "external_news", "manual_claim", "private_external", "operator_feedback", "llm_generated"}

RAW_TEXT_KEYS = {"text", "raw_text", "content", "html", "ocr", "summary", "title", "message"}
INTERNAL_STRATEGY_KEYS = {
    "execution_bias",
    "setup_skills",
    "belief_ledger",
    "semantic_memory",
    "recent_trade_events",
    "reasoning_trace",
}
SECRET_KEY_RE = re.compile(r"(api[_-]?key|secret|token|password|passphrase|private[_-]?key|mnemonic|seed)", re.I)
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|xox[baprs]-[A-Za-z0-9\-]{12,}|AKIA[0-9A-Z]{12,}|"
    r"BINANCE_[A-Z_]*KEY\s*=|NINEROUTER_[A-Z_]*KEY\s*=|OPENAI_[A-Z_]*KEY\s*=)",
    re.I,
)
HTML_TAG_RE = re.compile(r"<[^>]+>")
PROMPT_INJECTION_PATTERNS = (
    re.compile(r"ignore (all )?(previous|prior|above) (instructions|messages)", re.I),
    re.compile(r"(system|developer) (prompt|message|instruction)", re.I),
    re.compile(r"\b(tool|function)\s*call\b", re.I),
    re.compile(r"\b(place|open|execute|send|submit)\b.{0,40}\b(order|trade|market order)\b", re.I),
    re.compile(r"\b(all[- ]?in|max leverage|50x|100x|ignore stops?|recover losses)\b", re.I),
    re.compile(r"\b(api key|secret|withdraw|transfer funds?)\b", re.I),
)
PANIC_REVENGE_PATTERNS = (
    re.compile(r"\b(recover losses|make it back|gỡ|gỡ lại|phục thù|revenge)\b", re.I),
    re.compile(r"\b(ignore stops?|bỏ stop|all[- ]?in|max leverage|full margin)\b", re.I),
    re.compile(r"\b(50x|100x|đòn bẩy cao nhất)\b", re.I),
)
RISK_REDUCING_PATTERNS = (
    re.compile(r"\b(lower risk|reduce size|giảm risk|giảm size|tighten|stop trading|pause)\b", re.I),
)


def text_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def stable_id(prefix: str, payload: Any) -> str:
    return prefix + "_" + hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()[:20]


def sanitize_external_text(value: Any, max_chars: int = 800) -> dict[str, Any]:
    raw = str(value or "")
    flags: list[str] = []
    if "<script" in raw.lower() or HTML_TAG_RE.search(raw):
        flags.append("html_stripped")
    text = HTML_TAG_RE.sub(" ", raw)
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            flags.append("external_instruction_stripped")
            text = pattern.sub("[STRIPPED_EXTERNAL_INSTRUCTION]", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()[:max_chars]
    return {
        "schema_version": SCHEMA_VERSION,
        "text": text,
        "content_hash": text_hash(raw),
        "flags": sorted(set(flags)),
        "tainted": True,
    }


def source_policy(source_type: str | None, rights: str | None = None, provider: str | None = None) -> dict[str, Any]:
    stype = str(source_type or "unknown").strip().lower()
    rights_value = str(rights or "public").strip().lower()
    if rights_value in PRIVATE_RIGHTS:
        effect = "annotation_only"
        taint = "private_external"
    elif stype in MARKET_SOURCE_TYPES or str(provider or "").lower() in {"binance", "exchange"}:
        effect = "feature_input"
        taint = "public_market"
    elif stype in NEWS_SOURCE_TYPES:
        effect = "risk_tighten_only"
        taint = "external_news"
    elif stype in SOCIAL_SOURCE_TYPES:
        effect = "shadow_only"
        taint = "external_social"
    elif stype in MANUAL_SOURCE_TYPES:
        effect = "annotation_only"
        taint = "manual_claim"
    elif stype in {"llm", "model"}:
        effect = "hypothesis_only"
        taint = "llm_generated"
    elif stype in {"post_trade_review", "paper_trade", "counterfactual", "daily_exam", "test_result"}:
        effect = "feature_input"
        taint = "objective_ledger"
    else:
        effect = "annotation_only"
        taint = "unknown_external"
    return {"allowed_effect": effect, "taint_class": taint, "rights": rights_value}


def combine_allowed_effects(effects: list[str]) -> str:
    if not effects:
        return "deny"
    return min(effects, key=lambda item: EFFECT_ORDER.get(str(item), 0))


def allows_effect(allowed_effect: str | None, requested_effect: str) -> bool:
    return EFFECT_ORDER.get(str(allowed_effect or "deny"), 0) >= EFFECT_ORDER.get(requested_effect, 0)


def latency_fields(source_posted_at: Any, first_seen_at: Any | None = None, ttl_seconds: int = 900) -> dict[str, Any]:
    seen = str(first_seen_at or utc_now())
    delay = seconds_between(source_posted_at, seen)
    too_late = delay is not None and delay > ttl_seconds
    return {
        "source_posted_at": str(source_posted_at or ""),
        "first_seen_at": seen,
        "decision_delay_ms": int(delay * 1000) if delay is not None else None,
        "ttl_seconds": int(ttl_seconds),
        "too_late_to_copy": bool(too_late),
    }


def classify_human_feedback(text: Any, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = str(text or "")
    panic = any(pattern.search(raw) for pattern in PANIC_REVENGE_PATTERNS)
    risk_reducing = any(pattern.search(raw) for pattern in RISK_REDUCING_PATTERNS)
    evidence_backed = bool((evidence or {}).get("ledger_backed") or (evidence or {}).get("evidence_ids"))
    return {
        "schema_version": SCHEMA_VERSION,
        "sentiment": "unknown",
        "instruction": "risk_reducing" if risk_reducing else "panic_revenge" if panic else "operator_note",
        "outcome_claim": "unknown",
        "preference": "unknown",
        "metric_claim": "unknown",
        "panic_revenge": bool(panic),
        "risk_reducing_command": bool(risk_reducing),
        "learning_weight": 0.0 if panic or not evidence_backed else 1.0,
        "allowed_effect": "risk_tighten_only" if risk_reducing else "annotation_only",
        "taint_class": "operator_feedback",
    }


def _objective_ids(evidence: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in (
        "post_trade_review_ids",
        "review_id",
        "trade_ids",
        "trade_id",
        "paper_trade_ids",
        "counterfactual_ids",
        "replay_ids",
        "replay_id",
        "market_event_ids",
        "objective_event_ids",
    ):
        value = evidence.get(key)
        if isinstance(value, list):
            ids.extend(str(item) for item in value if item)
        elif isinstance(value, str) and value:
            ids.append(value)
    return sorted(set(ids))


def evaluate_evidence_for_learning(evidence: dict[str, Any], requested_effect: str = "skill_patch") -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    source_type = str(evidence.get("source_type") or evidence.get("source") or "").lower()
    allowed_effect = str(evidence.get("allowed_effect") or "")
    taint_class = str(evidence.get("taint_class") or "")
    social_or_manual = (
        source_type in SOCIAL_SOURCE_TYPES
        or source_type in MANUAL_SOURCE_TYPES
        or source_type in {"llm", "model"}
        or allowed_effect in {"shadow_only", "annotation_only", "hypothesis_only"}
        or taint_class in {"external_social", "manual_claim", "llm_generated", "private_external"}
    )
    objective_ids = _objective_ids(evidence)
    source_ids = evidence.get("source_ids") if isinstance(evidence.get("source_ids"), list) else []
    independent_sources = int(evidence.get("independent_source_count") or len(set(source_ids)))
    source_quorum = bool(evidence.get("source_quorum_passed") or independent_sources >= 2)
    objective_backing = bool(evidence.get("ledger_backed") or evidence.get("market_confirmed") or objective_ids)
    if social_or_manual and not (source_quorum and objective_backing):
        errors.append("external_claim_lacks_objective_quorum")
    if evidence.get("panic_revenge") is True:
        errors.append("panic_revenge_feedback_rejected")
    if requested_effect in {"skill_patch", "memory_promotion"} and allowed_effect in {"shadow_only", "annotation_only", "hypothesis_only"}:
        if not (source_quorum and objective_backing):
            errors.append(f"{allowed_effect}_cannot_promote_without_quorum")
    if requested_effect == "memory_promotion" and not objective_backing:
        errors.append("missing_objective_evidence")
    if evidence.get("missing_provenance") is True:
        errors.append("missing_provenance")
    if not objective_ids and not social_or_manual:
        warnings.append("objective_ids_not_declared")
    return {
        "ok": not errors,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "objective_evidence_ids": objective_ids,
        "source_quorum_passed": source_quorum,
        "objective_backing": objective_backing,
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }


def prepare_llm_egress(payload: Any, purpose: str, allow_tainted_text: bool = False, allow_internal_strategy: bool = False) -> dict[str, Any]:
    redacted_fields: list[str] = []
    taint_classes: set[str] = set()
    secret_hits: list[str] = []

    def marker_taint_class(value: Any, marker_context: bool = False) -> str | None:
        if not isinstance(value, dict):
            return None
        rights = str(value.get("rights") or "").strip().lower()
        taint = str(value.get("taint_class") or "").strip().lower()
        if rights in PRIVATE_RIGHTS:
            return "private_external"
        if taint in LLM_BLOCKED_TEXT_TAINTS:
            return taint
        for key, item in value.items():
            key_marker = marker_context or str(key).lower() in PRIVATE_MARKER_KEYS
            if key_marker:
                found = marker_taint_class(item, True)
                if found:
                    return found
        return None

    def inferred_source_taint(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        source_value = value.get("source_type")
        if source_value is None and not isinstance(value.get("source"), (dict, list)):
            source_value = value.get("source")
        policy = source_policy(source_value, value.get("rights"), value.get("provider"))
        taint = str(policy.get("taint_class") or "").strip().lower()
        if taint in LLM_BLOCKED_TEXT_TAINTS:
            return taint
        for key, item in value.items():
            if str(key).lower() in PRIVATE_MARKER_KEYS:
                found = inferred_source_taint(item)
                if found:
                    return found
        return None

    def scrub(value: Any, path: str = "$", inherited_taint: str | None = None) -> Any:
        if isinstance(value, dict):
            local_taint = str(value.get("taint_class") or inherited_taint or "").strip().lower()
            if not local_taint:
                local_taint = marker_taint_class(value) or inferred_source_taint(value) or ""
            if local_taint:
                taint_classes.add(local_taint)
            out: dict[str, Any] = {}
            for key, item in value.items():
                child_path = f"{path}.{key}"
                if str(key) in INTERNAL_STRATEGY_KEYS and not allow_internal_strategy:
                    redacted_fields.append(child_path)
                    out[key] = {
                        "egress_class": "internal_strategy_redacted",
                        "content_hash": text_hash(item),
                        "item_count": len(item) if hasattr(item, "__len__") else None,
                    }
                    continue
                if SECRET_KEY_RE.search(str(key)):
                    redacted_fields.append(child_path)
                    secret_hits.append(child_path)
                    out[key] = "[REDACTED_SECRET]"
                    continue
                if local_taint in LLM_BLOCKED_TEXT_TAINTS and str(key).lower() in RAW_TEXT_KEYS and not allow_tainted_text:
                    redacted_fields.append(child_path)
                    out[key] = f"[TAINTED_TEXT_REDACTED:{text_hash(item)[:24]}]"
                    continue
                out[key] = scrub(item, child_path, local_taint)
            return out
        if isinstance(value, list):
            return [scrub(item, f"{path}[{idx}]", inherited_taint) for idx, item in enumerate(value)]
        if isinstance(value, str):
            if SECRET_VALUE_RE.search(value):
                redacted_fields.append(path)
                secret_hits.append(path)
                return "[REDACTED_SECRET]"
            if inherited_taint in LLM_BLOCKED_TEXT_TAINTS and not allow_tainted_text:
                redacted_fields.append(path)
                return f"[TAINTED_TEXT_REDACTED:{text_hash(value)[:24]}]"
            return value
        return value

    sanitized = scrub(payload)
    proof = {
        "schema_version": SCHEMA_VERSION,
        "egress_id": stable_id("egress", {"purpose": purpose, "payload": sanitized}),
        "purpose": purpose,
        "checked_at": utc_now(),
        "allowed": True,
        "redacted_field_count": len(redacted_fields),
        "redacted_fields": redacted_fields[:50],
        "secret_hit_count": len(secret_hits),
        "taint_classes": sorted(taint_classes),
        "allow_tainted_text": bool(allow_tainted_text),
        "allow_internal_strategy": bool(allow_internal_strategy),
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    return {"payload": sanitized, "proof": proof}
