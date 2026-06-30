"""Model routing metadata without making API calls."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from live_permission_firewall import redact_secrets
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MODEL_HEALTH_LATEST = ROOT / "state" / "agent_memory" / "model_router_latest.json"

DEFAULT_MODEL = "cx/gpt-5.5"
QUICK_MODEL = "gpt-5-mini"
DEEP_JOBS = {"blindspot", "skill_forge", "daily_exam", "post_trade_explanation", "council_synthesis"}
COUNCIL_ROLE_ROUTES = {
    "risk_critic": {"quality": "deep", "required": True, "no_fallback": True},
    "market_analyst": {"quality": "deep", "required": False, "no_fallback": False},
    "setup_engineer": {"quality": "deep", "required": False, "no_fallback": False},
    "post_trade_reviewer": {"quality": "quick", "required": False, "no_fallback": False},
    "memory_curator": {"quality": "quick", "required": False, "no_fallback": False},
    "skill_forge_reviewer": {"quality": "deep", "required": False, "no_fallback": False},
}


def _env_value(env: dict[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        value = env.get(key)
        if value:
            return value
    return default


def route_model(job_type: str, env: dict[str, str] | None = None, role: str | None = None) -> dict[str, Any]:
    env = env or os.environ
    deep = _env_value(env, "NINEROUTER_MODEL", "NINE_ROUTER_MODEL", "OPENAI_MODEL", default=DEFAULT_MODEL)
    quick = _env_value(env, "NINEROUTER_QUICK_MODEL", "NINE_ROUTER_QUICK_MODEL", default=QUICK_MODEL)
    provider = _env_value(env, "NINEROUTER_BASE_URL", "NINE_ROUTER_BASE_URL", "OPENAI_BASE_URL", default="9router")
    role_policy = COUNCIL_ROLE_ROUTES.get(str(role or ""))
    use_deep = bool(role_policy and role_policy.get("quality") == "deep") or job_type in DEEP_JOBS
    budget_exhausted = str(env.get("MODEL_BUDGET_EXHAUSTED") or "").lower() in {"1", "true", "yes"}
    route_reason = "role_policy" if role_policy else "job_policy_deep" if use_deep else "job_policy_quick"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "routed_at": utc_now(),
        "job_type": job_type,
        "role": role,
        "provider_redacted": redact_secrets(provider),
        "provider_retention_policy": "external_provider_no_training_claim_unverified",
        "egress_policy": "redact_secrets_tainted_text_and_internal_strategy_by_default",
        "model": deep if use_deep else quick,
        "deep_model": deep,
        "quick_model": quick,
        "route_reason": route_reason,
        "required": bool(role_policy and role_policy.get("required")),
        "no_fallback": bool(role_policy and role_policy.get("no_fallback")),
        "fallback_allowed": not bool(role_policy and role_policy.get("no_fallback")),
        "allowed": not budget_exhausted,
        "degraded_reason": "budget_exhausted" if budget_exhausted else None,
        "degraded_action": "fail_closed" if budget_exhausted else "allow",
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    write_json_atomic(MODEL_HEALTH_LATEST, payload)
    return payload
