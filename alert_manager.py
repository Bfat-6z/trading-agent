"""Local alert manager with redaction and dedupe."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, append_jsonl_once, read_jsonl, write_json_atomic
from live_permission_firewall import redact_secrets
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
ALERTS_LATEST = STATE_DIR / "alerts_latest.json"
ALERTS_HISTORY = STATE_DIR / "alerts_history.jsonl"
ALERT_OUTBOX = STATE_DIR / "alert_outbox.jsonl"
WEBHOOK_OUTBOX = STATE_DIR / "webhook_outbox.jsonl"
INCIDENTS_LATEST = STATE_DIR / "incidents_latest.json"
INCIDENTS_HISTORY = STATE_DIR / "incidents_history.jsonl"
ALERT_CATALOG = {
    "dashboard_healthz_failed": {"severity": "Sev2", "threshold": 1, "runbook_id": "runbook_dashboard_healthz", "owner": "dashboard_security_owner"},
    "heartbeat_stale": {"severity": "Sev2", "threshold": 1, "runbook_id": "runbook_restart_circuit_breaker", "owner": "primary_operator"},
    "event_bus_lag_high": {"severity": "Sev3", "threshold": 500, "runbook_id": "runbook_event_bus_lag", "owner": "primary_operator"},
    "backup_restore_failed": {"severity": "Sev1", "threshold": 1, "runbook_id": "runbook_restore_drill", "owner": "backup_key_custodian"},
}


def alert_id(level: str, title: str, source: str) -> str:
    return "alert_" + hashlib.sha256(f"{level}:{source}:{title}".encode("utf-8")).hexdigest()[:18]

def incident_id(severity: str, title: str, source: str, dedupe_key: str | None = None) -> str:
    raw = f"{severity}:{source}:{dedupe_key or title}"
    return "inc_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]


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

def open_incident(
    severity: str,
    title: str,
    detail: Any,
    *,
    source: str = "system",
    owner: str = "operator",
    runbook_id: str = "runbook_unassigned",
    dedupe_key: str | None = None,
    action_required: str = "review",
    history_path: Path = INCIDENTS_HISTORY,
    latest_path: Path = INCIDENTS_LATEST,
) -> dict[str, Any]:
    row = {
        "schema_version": SCHEMA_VERSION,
        "incident_id": incident_id(severity, title, source, dedupe_key),
        "severity": severity,
        "status": "open",
        "title": redact_secrets(title),
        "detail": redact_secrets(detail),
        "source": source,
        "owner": owner,
        "opened_at": utc_now(),
        "acked_at": None,
        "resolved_at": None,
        "closed_at": None,
        "action_required": action_required,
        "runbook_id": runbook_id,
        "dedupe_key": dedupe_key or title,
    }
    append_jsonl(history_path, row)
    rows = read_jsonl(history_path, limit=200)
    latest_snapshot = write_incident_latest(history_path, latest_path)
    open_rows = latest_snapshot.get("open_rows", [])
    latest = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "open_count": len(open_rows),
        "last_inserted": True,
        "latest": row,
        "recent": rows[-20:],
    }
    write_json_atomic(latest_path, latest)
    return latest

def incident_complete(row: dict[str, Any]) -> bool:
    return bool(row.get("status") == "closed" and row.get("incident_id") and row.get("severity") and row.get("owner") and row.get("opened_at") and row.get("acked_at") and row.get("resolved_at") and row.get("closed_at"))

def incident_event_ts(row: dict[str, Any]) -> str:
    for key in ("updated_at", "closed_at", "resolved_at", "acked_at", "opened_at", "ts"):
        value = row.get(key)
        if value:
            return str(value)
    return ""

def write_incident_latest(history_path: Path = INCIDENTS_HISTORY, latest_path: Path = INCIDENTS_LATEST) -> dict[str, Any]:
    rows = read_jsonl(history_path, limit=500)
    by_id = {}
    for row in rows:
        if row.get("incident_id"):
            by_id[str(row["incident_id"])] = row
    latest_rows = sorted(by_id.values(), key=incident_event_ts)
    open_rows = [item for item in latest_rows if item.get("status") in {"open", "acked"}]
    latest = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "open_count": len(open_rows),
        "latest": latest_rows[-1] if latest_rows else {},
        "recent": latest_rows[-20:],
        "open_rows": open_rows[-20:],
    }
    write_json_atomic(latest_path, latest)
    return latest

def update_incident_status(
    incident: dict[str, Any],
    status: str,
    *,
    actor: str = "operator",
    detail: Any | None = None,
    history_path: Path = INCIDENTS_HISTORY,
    latest_path: Path = INCIDENTS_LATEST,
    persist: bool = True,
) -> dict[str, Any]:
    now = utc_now()
    updated = dict(incident)
    updated["status"] = status
    updated["updated_at"] = now
    updated["updated_by"] = actor
    if detail is not None:
        updated["update_detail"] = redact_secrets(detail)
    if status == "acked":
        updated["acked_at"] = updated.get("acked_at") or now
    elif status == "resolved":
        updated["acked_at"] = updated.get("acked_at") or now
        updated["resolved_at"] = updated.get("resolved_at") or now
    elif status == "closed":
        updated["acked_at"] = updated.get("acked_at") or now
        updated["resolved_at"] = updated.get("resolved_at") or now
        updated["closed_at"] = updated.get("closed_at") or now
    if persist:
        append_jsonl(history_path, updated)
        write_incident_latest(history_path, latest_path)
    return updated

def evaluate_slo_burn(metrics: dict[str, Any], catalog: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    catalog = catalog or ALERT_CATALOG
    incidents = []
    for signal, rule in catalog.items():
        value = metrics.get(signal)
        if value is None:
            continue
        try:
            breached = float(value) >= float(rule.get("threshold", 1))
        except Exception:
            breached = bool(value)
        if breached:
            incidents.append(
                {
                    "signal": signal,
                    "severity": rule.get("severity", "Sev3"),
                    "owner": rule.get("owner", "operator"),
                    "runbook_id": rule.get("runbook_id", "runbook_unassigned"),
                    "value": value,
                    "threshold": rule.get("threshold"),
                    "dedupe_key": f"slo:{signal}",
                }
            )
    return {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "status": "breached" if incidents else "ok", "incident_count": len(incidents), "incidents": incidents}
