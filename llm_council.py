"""LLM council synthesis with deterministic sanitizer gates."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from llm_output_quality_gate import sanitize_output
from model_usage_ledger import record_model_usage
from model_router import route_model
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
COUNCIL_HISTORY = MEMORY_DIR / "llm_council_history.jsonl"
COUNCIL_LATEST = MEMORY_DIR / "llm_council_latest.json"

ROLES = ["market_analyst", "risk_critic", "setup_engineer", "post_trade_reviewer", "memory_curator", "skill_forge_reviewer"]


def role_output_id(role: str, data_ids: list[str]) -> str:
    return "council_" + hashlib.sha256(f"{role}:{','.join(data_ids)}".encode("utf-8")).hexdigest()[:20]


def accept_role_output(role: str, output: dict[str, Any], path: Path = COUNCIL_HISTORY) -> dict[str, Any]:
    row = {**output, "role": role, "output_id": output.get("output_id") or role_output_id(role, [str(x) for x in output.get("data_ids", [])])}
    gate = sanitize_output(row, "council_role")
    accepted = {"schema_version": SCHEMA_VERSION, "accepted_at": utc_now(), "role": role, "output_id": row["output_id"], "accepted": gate["ok"], "quality_score": gate["quality_score"], "errors": gate["errors"], "payload": gate["sanitized"]}
    append_jsonl_once(path, accepted, "output_id")
    return accepted

def build_role_prompt(role: str, context: dict[str, Any], data_ids: list[str]) -> tuple[str, str]:
    if role not in ROLES:
        raise ValueError("unknown_council_role")
    system = (
        f"You are the {role} in a paper-only crypto trading learning council. "
        "You are read-only. Never place live orders and never loosen risk. Return one JSON object only."
    )
    user = {
        "required_schema": {"role": role, "summary": "short", "data_ids": data_ids, "recommendation": "tighten/test/observe only", "blindspot": "short", "risk_proposal": {"can_place_live_orders": False, "can_loosen_risk": False}},
        "context": context,
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

def run_role(role: str, context: dict[str, Any], data_ids: list[str], llm_call: Callable[[str, str, str], str] | None = None, history_path: Path = COUNCIL_HISTORY) -> dict[str, Any]:
    route = route_model("llm_council_role")
    system, user = build_role_prompt(role, context, data_ids)
    try:
        if llm_call is None:
            from llm_reasoning_agent import call_large_model
            raw = call_large_model(system, user, model=route.get("model"), max_tokens=900)
        else:
            raw = llm_call(system, user, str(route.get("model")))
        usage = record_model_usage("llm_council_role", str(route.get("model")), str(route.get("provider_redacted")), prompt=system + user, response=raw)
        payload = parse_json_object(raw)
        payload.setdefault("role", role)
        payload.setdefault("data_ids", data_ids)
        payload["model_route"] = route
        accepted = accept_role_output(role, payload, path=history_path)
        return {**accepted, "status": "ok" if accepted.get("accepted") else "rejected", "model_usage": usage}
    except Exception as exc:
        fallback = {"role": role, "summary": "role call failed", "data_ids": data_ids, "recommendation": "observe_only", "blindspot": "llm_role_unavailable", "risk_proposal": {"can_place_live_orders": False, "can_loosen_risk": False}, "error": str(exc)[:240], "model_route": route}
        usage = record_model_usage("llm_council_role", str(route.get("model")), str(route.get("provider_redacted")), prompt=system + user, response=fallback.get("error"), status="degraded")
        accepted = accept_role_output(role, fallback, path=history_path)
        return {**accepted, "status": "degraded", "model_usage": usage}


def synthesize_council(role_outputs: list[dict[str, Any]], data_ids: list[str], output_path: Path = COUNCIL_LATEST) -> dict[str, Any]:
    accepted = [row for row in role_outputs if row.get("accepted")]
    recommendations = []
    blindspots = []
    for row in accepted:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if payload.get("recommendation"):
            recommendations.append({"role": row.get("role"), "text": payload.get("recommendation")})
        if payload.get("blindspot"):
            blindspots.append({"role": row.get("role"), "text": payload.get("blindspot")})
    synthesis = {"schema_version": SCHEMA_VERSION, "synthesized_at": utc_now(), "summary": f"accepted {len(accepted)} of {len(role_outputs)} council roles", "data_ids": data_ids, "recommendations": recommendations, "blindspots": blindspots, "risk_proposal": {"can_place_live_orders": False, "can_loosen_risk": False}, "model_route": route_model("council_synthesis")}
    gate = sanitize_output(synthesis, "council_synthesis")
    result = {**synthesis, "quality_gate": {"ok": gate["ok"], "errors": gate["errors"], "quality_score": gate["quality_score"]}, "can_place_live_orders": False, "can_loosen_risk": False}
    write_json_atomic(output_path, result)
    return result


def summarize_history(path: Path = COUNCIL_HISTORY) -> dict[str, Any]:
    rows = read_jsonl(path)
    return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "role_output_count": len(rows), "accepted_count": sum(1 for row in rows if row.get("accepted"))}
