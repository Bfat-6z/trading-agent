"""Registered job types and simple rate/degraded policy for agent work queue."""
from __future__ import annotations

from typing import Any

REGISTERED_JOB_TYPES = {
    "market_scan": {"priority": 50, "llm": False},
    "news_macro_research": {"priority": 45, "llm": True},
    "setup_review": {"priority": 60, "llm": True},
    "post_trade_review": {"priority": 80, "llm": False},
    "replay_batch": {"priority": 70, "llm": False},
    "experiment_replay": {"priority": 68, "llm": False},
    "skill_patch_review": {"priority": 55, "llm": True},
    "daily_exam_task": {"priority": 65, "llm": True},
    "llm_council_role": {"priority": 40, "llm": True},
}


def validate_job_type(job_type: str) -> tuple[bool, str | None]:
    if job_type not in REGISTERED_JOB_TYPES:
        return False, "unknown_job_type"
    return True, None


def default_priority(job_type: str, priority: int | None = None) -> int:
    if priority is not None:
        return int(priority)
    return int(REGISTERED_JOB_TYPES.get(job_type, {}).get("priority", 10))


def llm_job_allowed(job_type: str, model_health: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    if not REGISTERED_JOB_TYPES.get(job_type, {}).get("llm"):
        return True, None
    health = model_health or {"status": "ok"}
    if health.get("status") in {"rate_limited", "outage", "disabled"}:
        return False, "llm_degraded"
    return True, None
