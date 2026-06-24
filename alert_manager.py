"""Local alert manager with redaction and dedupe."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from live_permission_firewall import redact_secrets
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
ALERTS_LATEST = STATE_DIR / "alerts_latest.json"
ALERTS_HISTORY = STATE_DIR / "alerts_history.jsonl"
ALERT_OUTBOX = STATE_DIR / "alert_outbox.jsonl"
WEBHOOK_OUTBOX = STATE_DIR / "webhook_outbox.jsonl"


def alert_id(level: str, title: str, source: str) -> str:
    return "alert_" + hashlib.sha256(f"{level}:{source}:{title}".encode("utf-8")).hexdigest()[:18]


def emit_alert(level: str, title: str, detail: Any, source: str = "system", runbook_id: str | None = None, history_path: Path = ALERTS_HISTORY, latest_path: Path = ALERTS_LATEST) -> dict[str, Any]:
    row = {"schema_version": SCHEMA_VERSION, "alert_id": alert_id(level, title, source), "ts": utc_now(), "level": level, "title": redact_secrets(title), "detail": redact_secrets(detail), "source": source, "runbook_id": runbook_id, "status": "open"}
    inserted = append_jsonl_once(history_path, row, "alert_id")
    rows = read_jsonl(history_path, limit=100)
    latest = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "open_count": len(rows), "last_inserted": inserted, "latest": row, "recent": rows[-20:]}
    write_json_atomic(latest_path, latest)
    return latest

def queue_local_notification(alert: dict[str, Any], channel: str = "local") -> dict[str, Any]:
    row = {"schema_version": SCHEMA_VERSION, "queued_at": utc_now(), "channel": channel, "alert_id": alert.get("alert_id"), "level": alert.get("level"), "title": redact_secrets(alert.get("title")), "detail": redact_secrets(alert.get("detail")), "status": "queued"}
    append_jsonl_once(ALERT_OUTBOX, row, "alert_id")
    return row

def queue_webhook_payload(alert: dict[str, Any], endpoint_name: str = "operator_webhook") -> dict[str, Any]:
    row = {"schema_version": SCHEMA_VERSION, "queued_at": utc_now(), "endpoint_name": endpoint_name, "alert_id": alert.get("alert_id"), "payload": {"level": alert.get("level"), "title": redact_secrets(alert.get("title")), "detail": redact_secrets(alert.get("detail")), "source": alert.get("source"), "runbook_id": alert.get("runbook_id")}, "status": "queued_not_sent"}
    append_jsonl_once(WEBHOOK_OUTBOX, row, "alert_id")
    return row

def emit_and_queue_alert(level: str, title: str, detail: Any, source: str = "system", runbook_id: str | None = None, channels: list[str] | None = None) -> dict[str, Any]:
    latest = emit_alert(level, title, detail, source=source, runbook_id=runbook_id)
    alert = latest.get("latest") if isinstance(latest.get("latest"), dict) else {}
    queued = []
    for channel in channels or ["local"]:
        if channel == "webhook":
            queued.append(queue_webhook_payload(alert))
        else:
            queued.append(queue_local_notification(alert, channel=channel))
    return {**latest, "queued_channels": queued}
