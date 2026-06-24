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


def route_model(job_type: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    deep = env.get("NINE_ROUTER_MODEL") or env.get("OPENAI_MODEL") or DEFAULT_MODEL
    quick = env.get("NINE_ROUTER_QUICK_MODEL") or "gpt-5-mini"
    provider = env.get("OPENAI_BASE_URL") or env.get("NINE_ROUTER_BASE_URL") or "9router"
    use_deep = job_type in {"blindspot", "skill_forge", "daily_exam", "post_trade_explanation", "council_synthesis"}
    payload = {"schema_version": SCHEMA_VERSION, "routed_at": utc_now(), "job_type": job_type, "provider_redacted": redact_secrets(provider), "model": deep if use_deep else quick, "deep_model": deep, "quick_model": quick, "can_place_live_orders": False, "can_loosen_risk": False}
    write_json_atomic(MODEL_HEALTH_LATEST, payload)
    return payload
