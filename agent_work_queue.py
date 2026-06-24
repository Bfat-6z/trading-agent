"""SQLite work queue for controlled parallel specialist agents."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from agent_job_registry import default_priority, llm_job_allowed, validate_job_type
from timebase import parse_utc, seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DEFAULT_DB = STATE_DIR / "agent_jobs.sqlite"


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            priority INTEGER NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            locked_by TEXT,
            locked_at TEXT,
            completed_at TEXT,
            error TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON jobs(status, priority DESC, created_at)")
    return conn


def stable_job_id(job_type: str, payload: dict[str, Any], explicit_id: str | None = None) -> str:
    if explicit_id:
        return explicit_id
    raw = json.dumps({"job_type": job_type, "payload": payload}, ensure_ascii=True, sort_keys=True)
    return "job_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def enqueue_job(job_type: str, payload: dict[str, Any], priority: int | None = None, job_id: str | None = None, db_path: Path = DEFAULT_DB, model_health: dict[str, Any] | None = None) -> dict[str, Any]:
    ok, error = validate_job_type(job_type)
    if not ok:
        return {"ok": False, "error": error, "job_id": None}
    llm_ok, llm_error = llm_job_allowed(job_type, model_health)
    if not llm_ok:
        return {"ok": False, "error": llm_error, "job_id": None}
    jid = stable_job_id(job_type, payload, job_id)
    row_priority = default_priority(job_type, priority)
    with connect(db_path) as conn:
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO jobs(job_id, job_type, priority, status, payload_json, created_at) VALUES (?, ?, ?, 'queued', ?, ?)",
            (jid, job_type, row_priority, json.dumps(payload, ensure_ascii=True, sort_keys=True), utc_now()),
        )
        inserted = conn.total_changes > before
    return {"ok": True, "job_id": jid, "inserted": inserted, "priority": row_priority}


def claim_next(worker_id: str, db_path: Path = DEFAULT_DB) -> dict[str, Any] | None:
    now = utc_now()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT job_id, job_type, priority, payload_json FROM jobs WHERE status = 'queued' ORDER BY priority DESC, created_at ASC LIMIT 1").fetchone()
        if not row:
            conn.commit()
            return None
        job_id, job_type, priority, payload_json = row
        conn.execute("UPDATE jobs SET status='running', locked_by=?, locked_at=? WHERE job_id=? AND status='queued'", (worker_id, now, job_id))
        conn.commit()
    return {"schema_version": SCHEMA_VERSION, "job_id": job_id, "job_type": job_type, "priority": priority, "payload": json.loads(payload_json), "locked_by": worker_id, "locked_at": now}

def claim_next_of_types(worker_id: str, job_types: list[str], db_path: Path = DEFAULT_DB) -> dict[str, Any] | None:
    allowed = [job_type for job_type in job_types if validate_job_type(job_type)[0]]
    if not allowed:
        return None
    placeholders = ",".join("?" for _ in allowed)
    now = utc_now()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT job_id, job_type, priority, payload_json FROM jobs WHERE status = 'queued' AND job_type IN ({placeholders}) ORDER BY priority DESC, created_at ASC LIMIT 1",
            tuple(allowed),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        job_id, job_type, priority, payload_json = row
        conn.execute("UPDATE jobs SET status='running', locked_by=?, locked_at=? WHERE job_id=? AND status='queued'", (worker_id, now, job_id))
        conn.commit()
    return {"schema_version": SCHEMA_VERSION, "job_id": job_id, "job_type": job_type, "priority": priority, "payload": json.loads(payload_json), "locked_by": worker_id, "locked_at": now}


def recover_stale_locks(max_lock_age_seconds: int = 900, db_path: Path = DEFAULT_DB) -> int:
    rows = []
    with connect(db_path) as conn:
        for job_id, locked_at in conn.execute("SELECT job_id, locked_at FROM jobs WHERE status='running'").fetchall():
            age = seconds_between(locked_at, utc_now()) if parse_utc(locked_at) else None
            if age is None or age > max_lock_age_seconds:
                rows.append(job_id)
        for job_id in rows:
            conn.execute("UPDATE jobs SET status='queued', locked_by=NULL, locked_at=NULL WHERE job_id=?", (job_id,))
    return len(rows)


def complete_job(job_id: str, ok: bool = True, error: str | None = None, db_path: Path = DEFAULT_DB) -> None:
    status = "done" if ok else "failed"
    with connect(db_path) as conn:
        conn.execute("UPDATE jobs SET status=?, completed_at=?, error=? WHERE job_id=?", (status, utc_now(), error, job_id))


def queue_summary(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
    return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "by_status": {status: count for status, count in rows}}
