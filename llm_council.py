"""LLM council synthesis with deterministic sanitizer gates."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from data_trust import prepare_llm_egress
from llm_output_quality_gate import sanitize_output
from model_usage_ledger import estimate_tokens, record_model_usage
from model_router import route_model
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
COUNCIL_HISTORY = MEMORY_DIR / "llm_council_history.jsonl"
COUNCIL_LATEST = MEMORY_DIR / "llm_council_latest.json"

ROLES = ["market_analyst", "risk_critic", "setup_engineer", "post_trade_reviewer", "memory_curator", "skill_forge_reviewer"]
REQUIRED_ROLES = {"risk_critic"}
MIN_ACCEPTED_ROLES = 2
ROLE_ALLOWED_FIELDS = {
    "role",
    "summary",
    "data_ids",
    "recommendation",
    "blindspot",
    "risk_proposal",
    "veto",
    "confidence",
    "output_id",
    "model_route",
    "egress_proof",
    "can_place_live_orders",
    "can_loosen_risk",
    "live_permission",
}

def _env_int(env: dict[str, str] | os._Environ[str], key: str) -> int:
    try:
        return max(0, int(str(env.get(key) or "0")))
    except Exception:
        return 0

def _role_budget_key(prefix: str, role: str) -> str:
    suffix = re.sub(r"[^A-Z0-9]+", "_", str(role or "UNKNOWN").upper()).strip("_")
    return f"{prefix}_{suffix}"

def model_budget_allowed(
    job_type: str,
    role: str,
    prompt: Any,
    *,
    max_response_tokens: int = 900,
    env: dict[str, str] | os._Environ[str] | None = None,
) -> dict[str, Any]:
    env = env or os.environ
    prompt_tokens = estimate_tokens(prompt)
    reserved_output_tokens = max(0, int(max_response_tokens or 0))
    required_tokens = prompt_tokens + reserved_output_tokens
    manual_block = str(env.get("MODEL_BUDGET_EXHAUSTED") or "").strip().lower() in {"1", "true", "yes"}
    daily_budget = _env_int(env, "MODEL_DAILY_TOKEN_BUDGET")
    daily_used = _env_int(env, "MODEL_DAILY_TOKENS_USED")
    role_budget = _env_int(env, _role_budget_key("MODEL_ROLE_TOKEN_BUDGET", role))
    role_used = _env_int(env, _role_budget_key("MODEL_ROLE_TOKENS_USED", role))
    reason = None
    if manual_block:
        reason = "budget_exhausted"
    elif daily_budget and daily_used + required_tokens > daily_budget:
        reason = "token_budget_exhausted"
    elif role_budget and role_used + required_tokens > role_budget:
        reason = "role_token_budget_exhausted"
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "job_type": job_type,
        "role": role,
        "allowed": reason is None,
        "reason": reason,
        "prompt_tokens_est": prompt_tokens,
        "reserved_output_tokens_est": reserved_output_tokens,
        "required_tokens_est": required_tokens,
        "daily_token_budget": daily_budget,
        "daily_tokens_used": daily_used,
        "role_token_budget": role_budget,
        "role_tokens_used": role_used,
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }


def role_output_id(role: str, data_ids: list[str]) -> str:
    return "council_" + hashlib.sha256(f"{role}:{','.join(data_ids)}".encode("utf-8")).hexdigest()[:20]


def accept_role_output(role: str, output: dict[str, Any], path: Path = COUNCIL_HISTORY) -> dict[str, Any]:
    row = {**output, "role": role, "output_id": output.get("output_id") or role_output_id(role, [str(x) for x in output.get("data_ids", [])])}
    schema_errors = []
    if role not in ROLES:
        schema_errors.append("unknown_role")
    extra = sorted(set(row) - ROLE_ALLOWED_FIELDS)
    if extra:
        schema_errors.append("unknown_fields:" + ",".join(extra))
    route = row.get("model_route") if isinstance(row.get("model_route"), dict) else {}
    if route.get("allowed") is False or route.get("degraded_reason"):
        schema_errors.append("model_route_degraded")
    if role == "risk_critic" and route.get("no_fallback") and route.get("allowed") is False:
        schema_errors.append("required_role_no_fallback_unavailable")
    if role == "risk_critic":
        required_risk_fields = {"recommendation", "blindspot", "risk_proposal", "model_route"}
        missing_risk = sorted(field for field in required_risk_fields if not row.get(field))
        if missing_risk:
            schema_errors.append("missing_risk_critic_fields:" + ",".join(missing_risk))
        risk_proposal = row.get("risk_proposal") if isinstance(row.get("risk_proposal"), dict) else {}
        if risk_proposal.get("can_place_live_orders") is not False or risk_proposal.get("can_loosen_risk") is not False:
            schema_errors.append("risk_critic_flags_not_explicitly_safe")
    gate = sanitize_output(row, "council_role")
    errors = sorted(set(list(gate["errors"]) + schema_errors))
    accepted = {"schema_version": SCHEMA_VERSION, "accepted_at": utc_now(), "role": role, "output_id": row["output_id"], "accepted": gate["ok"] and not schema_errors, "quality_score": gate["quality_score"], "errors": errors, "payload": gate["sanitized"]}
    append_jsonl_once(path, accepted, "output_id")
    return accepted

def _accepted_role_output_is_valid(row: dict[str, Any]) -> bool:
    if not row.get("accepted"):
        return False
    role = str(row.get("role") or "")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    if role not in ROLES or not payload:
        return False
    if row.get("schema_version") != SCHEMA_VERSION:
        return False
    if not row.get("accepted_at") or not row.get("output_id"):
        return False
    if not isinstance(row.get("errors"), list):
        return False
    if row.get("errors"):
        return False
    try:
        float(row.get("quality_score"))
    except Exception:
        return False
    expected_output_id = role_output_id(role, [str(x) for x in payload.get("data_ids", []) if x])
    if str(row.get("output_id")) != str(payload.get("output_id") or expected_output_id):
        return False
    if sorted(set(payload) - ROLE_ALLOWED_FIELDS):
        return False
    if any(not payload.get(field) for field in {"role", "summary", "data_ids"}):
        return False
    if role == "risk_critic":
        route = payload.get("model_route") if isinstance(payload.get("model_route"), dict) else {}
        risk_proposal = payload.get("risk_proposal") if isinstance(payload.get("risk_proposal"), dict) else {}
        required_risk_fields = {"recommendation", "blindspot", "risk_proposal", "model_route"}
        if any(not payload.get(field) for field in required_risk_fields):
            return False
        if route.get("allowed") is False or route.get("degraded_reason"):
            return False
        if risk_proposal.get("can_place_live_orders") is not False or risk_proposal.get("can_loosen_risk") is not False:
            return False
    return True

def build_role_prompt(role: str, context: dict[str, Any], data_ids: list[str]) -> tuple[str, str]:
    if role not in ROLES:
        raise ValueError("unknown_council_role")
    system = (
        f"You are the {role} in a paper-only crypto trading learning council. "
        "You are read-only. Never place live orders and never loosen risk. Return one JSON object only."
    )
    egress = prepare_llm_egress(context, f"llm_council:{role}")
    user = {
        "required_schema": {"role": role, "summary": "short", "data_ids": data_ids, "recommendation": "tighten/test/observe only", "blindspot": "short", "risk_proposal": {"can_place_live_orders": False, "can_loosen_risk": False}},
        "context": egress["payload"],
        "egress_proof": egress["proof"],
    }
    return system, json.dumps(user, ensure_ascii=True, sort_keys=True)

def parse_json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(text[start : end + 1])
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}
    return {}


def normalize_provider_response(raw: Any, fallback_model: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        text = str(raw.get("text") or raw.get("content") or raw.get("response") or "")
        latency = raw.get("latency_ms")
        return {
            "text": text,
            "actual_model": str(raw.get("model") or raw.get("actual_model") or raw.get("model_id") or fallback_model),
            "request_id": str(raw.get("request_id") or raw.get("id") or ""),
            "latency_ms": int(latency) if isinstance(latency, (int, float)) or str(latency).isdigit() else None,
        }
    return {"text": str(raw), "actual_model": fallback_model, "request_id": "", "latency_ms": None}

def run_role(role: str, context: dict[str, Any], data_ids: list[str], llm_call: Callable[[str, str, str], str] | None = None, history_path: Path = COUNCIL_HISTORY) -> dict[str, Any]:
    route = route_model("llm_council_role", role=role)
    egress = prepare_llm_egress(context, f"llm_council:{role}")
    system, user = build_role_prompt(role, context, data_ids)
    if not route.get("allowed", True):
        fallback = {"role": role, "summary": "role blocked by model governance", "data_ids": data_ids, "recommendation": "observe_only", "blindspot": str(route.get("degraded_reason") or "model_route_unavailable"), "risk_proposal": {"can_place_live_orders": False, "can_loosen_risk": False}, "model_route": route, "egress_proof": egress["proof"]}
        usage = record_model_usage("llm_council_role", str(route.get("model")), str(route.get("provider_redacted")), prompt="", response=fallback.get("blindspot"), status="degraded", route_reason=str(route.get("route_reason")), fallback_reason=str(route.get("degraded_reason") or "route_blocked"), quality_gate_ok=False)
        accepted = accept_role_output(role, fallback, path=history_path)
        return {**accepted, "status": "degraded", "model_usage": usage}
    budget_guard = model_budget_allowed("llm_council_role", role, system + user, max_response_tokens=900)
    if not budget_guard.get("allowed"):
        blocked_route = {
            **route,
            "allowed": False,
            "degraded_reason": budget_guard.get("reason") or "token_budget_exhausted",
            "degraded_action": "fail_closed",
            "budget_guard": budget_guard,
        }
        fallback = {"role": role, "summary": "role blocked by model token budget", "data_ids": data_ids, "recommendation": "observe_only", "blindspot": str(blocked_route.get("degraded_reason")), "risk_proposal": {"can_place_live_orders": False, "can_loosen_risk": False}, "model_route": blocked_route, "egress_proof": egress["proof"]}
        usage = record_model_usage("llm_council_role", str(blocked_route.get("model")), str(blocked_route.get("provider_redacted")), prompt=system + user, response=fallback.get("blindspot"), status="degraded", route_reason=str(blocked_route.get("route_reason")), fallback_reason=str(blocked_route.get("degraded_reason") or "token_budget_exhausted"), quality_gate_ok=False)
        accepted = accept_role_output(role, fallback, path=history_path)
        return {**accepted, "status": "degraded", "model_usage": usage, "budget_guard": budget_guard}
    try:
        started = time.perf_counter()
        if llm_call is None:
            from llm_reasoning_agent import call_large_model
            raw = call_large_model(system, user, model=route.get("model"), max_tokens=900)
        else:
            raw = llm_call(system, user, str(route.get("model")))
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        provider_response = normalize_provider_response(raw, str(route.get("model")))
        text = provider_response["text"]
        payload = parse_json_object(text)
        payload.setdefault("role", role)
        payload.setdefault("data_ids", data_ids)
        payload["model_route"] = route
        payload["egress_proof"] = egress["proof"]
        accepted = accept_role_output(role, payload, path=history_path)
        usage_status = "ok" if accepted.get("accepted") else "rejected"
        usage = record_model_usage(
            "llm_council_role",
            str(route.get("model")),
            str(route.get("provider_redacted")),
            prompt=system + user,
            response=text,
            status=usage_status,
            request_id=provider_response.get("request_id") or None,
            latency_ms=provider_response.get("latency_ms") or elapsed_ms,
            actual_response_model_id=str(provider_response.get("actual_model") or route.get("model")),
            route_reason=str(route.get("route_reason")),
            quality_gate_ok=bool(accepted.get("accepted")),
        )
        return {**accepted, "status": usage_status, "model_usage": usage}
    except Exception as exc:
        fallback = {"role": role, "summary": "role call failed", "data_ids": data_ids, "recommendation": "observe_only", "blindspot": "llm_role_unavailable", "risk_proposal": {"can_place_live_orders": False, "can_loosen_risk": False}, "error": str(exc)[:240], "model_route": route, "egress_proof": egress["proof"]}
        usage = record_model_usage("llm_council_role", str(route.get("model")), str(route.get("provider_redacted")), prompt=system + user, response=fallback.get("error"), status="degraded", route_reason=str(route.get("route_reason")), fallback_reason=type(exc).__name__, quality_gate_ok=False)
        accepted = accept_role_output(role, fallback, path=history_path)
        return {**accepted, "status": "degraded", "model_usage": usage}


def synthesize_council(role_outputs: list[dict[str, Any]], data_ids: list[str], output_path: Path = COUNCIL_LATEST) -> dict[str, Any]:
    invalid_accepted_roles = sorted(
        str(row.get("role") or "unknown")
        for row in role_outputs
        if row.get("accepted") and not _accepted_role_output_is_valid(row)
    )
    accepted = [row for row in role_outputs if _accepted_role_output_is_valid(row)]
    accepted_roles = {str(row.get("role")) for row in accepted}
    missing_required = sorted(REQUIRED_ROLES - accepted_roles)
    risk_rows = [row for row in accepted if row.get("role") == "risk_critic"]
    risk_veto = any(
        (row.get("payload") or {}).get("veto") is True
        or any(word in str((row.get("payload") or {}).get("recommendation") or "").lower() for word in ("veto", "block", "reject"))
        for row in risk_rows
        if isinstance(row.get("payload"), dict)
    )
    quorum_errors = []
    if missing_required:
        quorum_errors.append("missing_required_roles:" + ",".join(missing_required))
    if len(accepted) < MIN_ACCEPTED_ROLES:
        quorum_errors.append("insufficient_accepted_roles")
    if risk_veto:
        quorum_errors.append("risk_critic_veto")
    if invalid_accepted_roles:
        quorum_errors.append("invalid_accepted_roles:" + ",".join(invalid_accepted_roles))
    recommendations = []
    blindspots = []
    for row in accepted:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if payload.get("recommendation"):
            recommendations.append({"role": row.get("role"), "text": payload.get("recommendation")})
        if payload.get("blindspot"):
            blindspots.append({"role": row.get("role"), "text": payload.get("blindspot")})
    synthesis = {
        "schema_version": SCHEMA_VERSION,
        "synthesized_at": utc_now(),
        "summary": f"accepted {len(accepted)} of {len(role_outputs)} council roles",
        "data_ids": data_ids,
        "recommendations": recommendations,
        "blindspots": blindspots,
        "risk_proposal": {"can_place_live_orders": False, "can_loosen_risk": False},
        "model_route": route_model("council_synthesis"),
        "quorum": {"ok": not quorum_errors, "errors": quorum_errors, "required_roles": sorted(REQUIRED_ROLES), "accepted_roles": sorted(accepted_roles), "min_accepted_roles": MIN_ACCEPTED_ROLES, "risk_veto": risk_veto},
    }
    gate = sanitize_output(synthesis, "council_synthesis")
    result = {**synthesis, "accepted": gate["ok"] and not quorum_errors, "status": "ok" if gate["ok"] and not quorum_errors else "rejected", "quality_gate": {"ok": gate["ok"], "errors": gate["errors"], "quality_score": gate["quality_score"]}, "can_place_live_orders": False, "can_loosen_risk": False}
    write_json_atomic(output_path, result)
    return result


def summarize_history(path: Path = COUNCIL_HISTORY) -> dict[str, Any]:
    rows = read_jsonl(path)
    return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "role_output_count": len(rows), "accepted_count": sum(1 for row in rows if row.get("accepted"))}
