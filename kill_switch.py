"""Local kill switch for autonomous paper/live-capable daemons."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from alert_manager import emit_alert
from atomic_state import append_jsonl, read_json, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
KILL_SWITCH_FILE = STATE_DIR / "KILL_SWITCH_ACTIVE.json"
INCIDENT_HISTORY = STATE_DIR / "incident_history.jsonl"


def activate_kill_switch(reason: str, operator: str = "operator", path: Path = KILL_SWITCH_FILE) -> dict[str, Any]:
    incident = {"schema_version": SCHEMA_VERSION, "activated_at": utc_now(), "active": True, "reason": reason, "operator": operator, "paper_brain_allowed": False, "live_capable_daemons_allowed": False, "dashboard_readable": True}
    write_json_atomic(path, incident)
    append_jsonl(INCIDENT_HISTORY, {"event": "kill_switch_activated", **incident})
    emit_alert("critical", "kill switch activated", reason, source="kill_switch", runbook_id="RUNBOOK-KILL-SWITCH")
    return incident


def clear_kill_switch(operator: str = "operator", path: Path = KILL_SWITCH_FILE) -> dict[str, Any]:
    previous = read_json(path, default={})
    incident = {"schema_version": SCHEMA_VERSION, "cleared_at": utc_now(), "active": False, "operator": operator, "previous_reason": previous.get("reason")}
    write_json_atomic(path, incident)
    append_jsonl(INCIDENT_HISTORY, {"event": "kill_switch_cleared", **incident})
    return incident


def kill_switch_active(path: Path = KILL_SWITCH_FILE) -> bool:
    # fail-CLOSED (bughunt 2026-07-08): read_json can't distinguish "file missing" from
    # "corrupt/locked/half-written", so `default={}` would silently return active=False and
    # BYPASS the emergency stop during exactly the incident it exists for. A present-but-
    # unreadable kill-switch file must be treated as ACTIVE.
    if not path.exists():
        return False                       # no kill-switch set -> inactive (normal)
    try:                                    # read+parse DIRECTLY, not via read_json — read_json
        data = json.loads(path.read_text(encoding="utf-8"))   # returns {} for BOTH missing and
    except Exception:                       # corrupt (root R1), which would re-open this hole.
        return True                        # exists but unparseable -> assume ACTIVE (safety)
    return bool(isinstance(data, dict) and data.get("active"))
