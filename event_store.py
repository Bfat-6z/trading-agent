"""SQLite event store for trading-agent runtime state.

JSONL remains the compatibility log. This module mirrors important runtime
events into SQLite so memory/replay queries do not depend on scanning large
text files forever.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from agent_data_contracts import ENVELOPE_SCHEMA_VERSION, EVENT_SCHEMA_REGISTRY, schema_digest, validate_event_envelope

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DEFAULT_DB = STATE_DIR / "agent_state.db"
BUS_HEALTH_LATEST = STATE_DIR / "agent_memory" / "event_bus_health_latest.json"
BUS_HEALTH_HISTORY = STATE_DIR / "agent_memory" / "event_bus_health_history.jsonl"
ERASURE_RECEIPTS = STATE_DIR / "agent_memory" / "erasure_receipts.jsonl"

REPLAY_MANIFEST_REQUIRED_FIELDS = {
    "manifest_id",
    "schema_digest",
    "code_version",
    "config_digest",
    "source_snapshot_hashes",
    "fixture_ids",
}
RETENTION_MATRIX = {
    "event_envelope": {"ttl_days": None, "archive": True, "payload_prunable": False},
    "raw_social_text": {"ttl_days": 30, "archive": False, "payload_prunable": True},
    "screenshot_original": {"ttl_days": 30, "archive": False, "payload_prunable": True},
    "strategy_note": {"ttl_days": 365, "archive": True, "payload_prunable": True},
    "prompt_trace": {"ttl_days": 90, "archive": True, "payload_prunable": True},
    "feature": {"ttl_days": 365, "archive": True, "payload_prunable": False},
    "backup": {"ttl_days": 365, "archive": True, "payload_prunable": True},
}
WINDOWS_IO_LOCK_POLICY = {
    "journal_mode": "WAL",
    "busy_timeout_ms": 5000,
    "write_transaction": "short",
    "lock_order": ["writer_pause", "drain", "wal_checkpoint", "backup", "resume"],
    "stuck_lock_incident_seconds": 30,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            source TEXT NOT NULL,
            event TEXT NOT NULL,
            symbol TEXT,
            side TEXT,
            event_hash TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_source_ts ON events(source, ts);
        CREATE INDEX IF NOT EXISTS idx_events_event_ts ON events(event, ts);
        CREATE INDEX IF NOT EXISTS idx_events_symbol_ts ON events(symbol, ts);

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            source TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_source_kind_ts ON snapshots(source, kind, ts);

        CREATE TABLE IF NOT EXISTS heartbeats (
            source TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS event_envelopes (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            schema_digest TEXT NOT NULL,
            producer_id TEXT NOT NULL,
            producer_version TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_hash TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            available_at TEXT NOT NULL,
            known_at TEXT NOT NULL,
            effective_at TEXT,
            ingested_at TEXT NOT NULL,
            processed_at TEXT,
            source_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            causation_id TEXT,
            priority INTEGER NOT NULL,
            provenance_id TEXT,
            envelope_hash TEXT NOT NULL UNIQUE,
            previous_audit_hash TEXT,
            audit_hash TEXT,
            payload_json TEXT NOT NULL,
            envelope_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_event_envelopes_type_known ON event_envelopes(event_type, known_at);
        CREATE INDEX IF NOT EXISTS idx_event_envelopes_source_known ON event_envelopes(source_id, known_at);

        CREATE TABLE IF NOT EXISTS ledger_transactions (
            transaction_key TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_hash_chain (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            previous_hash TEXT,
            audit_hash TEXT NOT NULL,
            envelope_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS consumer_subscriptions (
            subscription_id TEXT PRIMARY KEY,
            consumer_id TEXT NOT NULL,
            event_types_json TEXT NOT NULL,
            max_unacked INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subscription_offsets (
            subscription_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            last_acked_seq INTEGER NOT NULL DEFAULT 0,
            acked_at TEXT,
            PRIMARY KEY(subscription_id, event_type)
        );

        CREATE TABLE IF NOT EXISTS event_attempts (
            attempt_id TEXT PRIMARY KEY,
            subscription_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            event_seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            lease_token TEXT NOT NULL,
            state TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            leased_at TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            acked_at TEXT,
            error TEXT,
            retry_cause_hash TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_event_attempt_active ON event_attempts(subscription_id, event_id, state);

        CREATE TABLE IF NOT EXISTS event_dlq (
            dlq_id TEXT PRIMARY KEY,
            subscription_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            event_seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            reason TEXT NOT NULL,
            retry_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(subscription_id, event_id)
        );
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "event_hash" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN event_hash TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_hash ON events(event_hash)")
    conn.commit()

def canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

def event_hash(source: str, event: str, ts: str, payload: dict) -> str:
    raw = canonical_json({"source": source, "event": event, "ts": ts, "payload": payload})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def sha256_json(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

def parse_utc_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def normalize_utc(value: str | None) -> str:
    if not value:
        return utc_now()
    return parse_utc_ts(value).isoformat(timespec="seconds")

def build_idempotency_key(source_id: str, event_type: str, correlation_id: str, sequence: str | int | None = None) -> str:
    return f"{source_id}:{event_type}:{correlation_id}:{sequence if sequence is not None else '0'}"

def envelope_hash(envelope: dict[str, Any]) -> str:
    material = {k: v for k, v in envelope.items() if k not in {"envelope_hash", "audit_hash", "previous_audit_hash"}}
    return sha256_json(material)

def build_event_envelope(
    event_type: str,
    payload: dict[str, Any],
    producer_id: str,
    source_id: str,
    correlation_id: str,
    *,
    producer_version: str = "local-dev",
    idempotency_key: str | None = None,
    occurred_at: str | None = None,
    available_at: str | None = None,
    known_at: str | None = None,
    effective_at: str | None = None,
    causation_id: str | None = None,
    priority: int = 50,
    provenance_id: str | None = None,
    sequence: str | int | None = None,
) -> dict[str, Any]:
    occurred = normalize_utc(occurred_at)
    available = normalize_utc(available_at or occurred)
    known = normalize_utc(known_at or available)
    ingested = utc_now()
    payload_hash = sha256_json(payload)
    idem = idempotency_key or build_idempotency_key(source_id, event_type, correlation_id, sequence)
    base = {
        "event_type": event_type,
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "schema_digest": schema_digest(event_type),
        "producer_id": producer_id,
        "producer_version": producer_version,
        "idempotency_key": idem,
        "payload_hash": payload_hash,
        "occurred_at": occurred,
        "available_at": available,
        "known_at": known,
        "effective_at": normalize_utc(effective_at) if effective_at else None,
        "ingested_at": ingested,
        "processed_at": None,
        "source_id": source_id,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "priority": int(priority),
        "provenance_id": provenance_id,
        "payload": payload,
    }
    base["event_id"] = "evt_" + hashlib.sha256(canonical_json({k: v for k, v in base.items() if k != "payload"} | {"payload_hash": payload_hash}).encode("utf-8")).hexdigest()[:24]
    base["envelope_hash"] = envelope_hash(base)
    return base

def validate_time_order(envelope: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        occurred = parse_utc_ts(envelope["occurred_at"])
        available = parse_utc_ts(envelope["available_at"])
        known = parse_utc_ts(envelope["known_at"])
        ingested = parse_utc_ts(envelope["ingested_at"])
    except Exception:
        return ["invalid_event_time"]
    if available < occurred:
        errors.append("available_before_occurred")
    if known < available:
        errors.append("known_before_available")
    if ingested < known:
        errors.append("ingested_before_known")
    return errors

def ledger_transaction_key(payload: dict[str, Any]) -> str | None:
    keys = ["venue", "account_mode", "order_id", "fill_id", "execution_id", "side", "qty", "price"]
    if not payload.get("order_id") or not (payload.get("fill_id") or payload.get("execution_id")):
        return None
    parts = [str(payload.get(key) or "") for key in keys]
    return "ledger_txn:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

def previous_audit_hash(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT audit_hash FROM audit_hash_chain ORDER BY seq DESC LIMIT 1").fetchone()
    return row[0] if row else None

def audit_hash(previous_hash: str | None, envelope: dict[str, Any]) -> str:
    return sha256_json({"previous_hash": previous_hash, "envelope_hash": envelope.get("envelope_hash"), "event_id": envelope.get("event_id"), "event_type": envelope.get("event_type")})

def append_enveloped_event(envelope: dict[str, Any], db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    validation = validate_event_envelope(envelope)
    errors = list(validation.errors)
    errors.extend(validate_time_order(envelope))
    if envelope.get("payload_hash") != sha256_json(envelope.get("payload") or {}):
        errors.append("payload_hash_mismatch")
    if envelope.get("schema_digest") != schema_digest(str(envelope.get("event_type") or "")):
        errors.append("schema_digest_mismatch")
    if errors:
        if envelope.get("event_type") != "event.contract_rejected":
            try:
                reason = ";".join(sorted(set(errors)))
                rejected = build_event_envelope(
                    "event.contract_rejected",
                    {
                        "rejection_id": "reject_" + hashlib.sha256(canonical_json({"event_type": envelope.get("event_type"), "errors": sorted(set(errors)), "payload_hash": envelope.get("payload_hash")}).encode("utf-8")).hexdigest()[:20],
                        "event_type": str(envelope.get("event_type") or "unknown"),
                        "reason": reason[:300],
                        "errors": sorted(set(errors)),
                    },
                    "event_store",
                    "event_store",
                    str(envelope.get("event_id") or envelope.get("idempotency_key") or utc_now()),
                )
                append_enveloped_event(rejected, db_path=db_path)
            except Exception:
                pass
        return {"ok": False, "inserted": False, "errors": sorted(set(errors)), "can_place_live_orders": False, "live_permission": False}
    with connect(db_path) as conn:
        existing = conn.execute("SELECT payload_hash, event_id FROM event_envelopes WHERE idempotency_key = ?", (envelope["idempotency_key"],)).fetchone()
        if existing:
            if existing[0] == envelope["payload_hash"]:
                return {"ok": True, "inserted": False, "deduped": True, "event_id": existing[1], "can_place_live_orders": False, "live_permission": False}
            return {"ok": False, "inserted": False, "errors": ["idempotency_payload_conflict"], "can_place_live_orders": False, "live_permission": False}
        schema = EVENT_SCHEMA_REGISTRY.get(envelope["event_type"], {})
        txn_key = ledger_transaction_key(envelope.get("payload") or {}) if schema.get("ledger_transaction") else None
        if txn_key:
            existing_txn = conn.execute("SELECT event_id FROM ledger_transactions WHERE transaction_key = ?", (txn_key,)).fetchone()
            if existing_txn:
                return {"ok": True, "inserted": False, "deduped": True, "event_id": existing_txn[0], "ledger_transaction_deduped": True, "can_place_live_orders": False, "live_permission": False}
        previous_hash = previous_audit_hash(conn) if schema.get("audit_chain") else None
        current_audit_hash = audit_hash(previous_hash, envelope) if schema.get("audit_chain") else None
        conn.execute(
            """
            INSERT INTO event_envelopes(event_id,event_type,schema_version,schema_digest,producer_id,producer_version,idempotency_key,payload_hash,occurred_at,available_at,known_at,effective_at,ingested_at,processed_at,source_id,correlation_id,causation_id,priority,provenance_id,envelope_hash,previous_audit_hash,audit_hash,payload_json,envelope_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                envelope["event_id"], envelope["event_type"], envelope["schema_version"], envelope["schema_digest"], envelope["producer_id"], envelope["producer_version"], envelope["idempotency_key"], envelope["payload_hash"], envelope["occurred_at"], envelope["available_at"], envelope["known_at"], envelope.get("effective_at"), envelope["ingested_at"], envelope.get("processed_at"), envelope["source_id"], envelope["correlation_id"], envelope.get("causation_id"), int(envelope["priority"]), envelope.get("provenance_id"), envelope["envelope_hash"], previous_hash, current_audit_hash, canonical_json(envelope.get("payload") or {}), canonical_json(envelope),
            ),
        )
        if txn_key:
            conn.execute("INSERT INTO ledger_transactions(transaction_key,event_id,event_type,payload_hash,created_at) VALUES (?,?,?,?,?)", (txn_key, envelope["event_id"], envelope["event_type"], envelope["payload_hash"], utc_now()))
        if current_audit_hash:
            conn.execute("INSERT INTO audit_hash_chain(event_id,event_type,previous_hash,audit_hash,envelope_hash,created_at) VALUES (?,?,?,?,?,?)", (envelope["event_id"], envelope["event_type"], previous_hash, current_audit_hash, envelope["envelope_hash"], utc_now()))
    return {"ok": True, "inserted": True, "event_id": envelope["event_id"], "envelope_hash": envelope["envelope_hash"], "audit_hash": current_audit_hash, "can_place_live_orders": False, "live_permission": False}

def append_event_envelope(event_type: str, payload: dict[str, Any], producer_id: str, source_id: str, correlation_id: str, db_path: Path = DEFAULT_DB, **kwargs: Any) -> dict[str, Any]:
    envelope = build_event_envelope(event_type, payload, producer_id, source_id, correlation_id, **kwargs)
    return append_enveloped_event(envelope, db_path=db_path)

def validate_cutoff_proof(envelope_ids: list[str], cutoff: dict[str, Any], db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    errors: list[str] = []
    decision_cutoff = parse_utc_ts(str(cutoff.get("decision_cutoff") or cutoff.get("max_known_at") or cutoff.get("max_available_at") or utc_now()))
    latency_buffer = int(cutoff.get("latency_buffer_seconds") or 0)
    usable_deadline = decision_cutoff - timedelta(seconds=max(0, latency_buffer))
    field_deadlines = {
        "known_at": parse_utc_ts(str(cutoff.get("max_known_at"))) if cutoff.get("max_known_at") else usable_deadline,
        "available_at": parse_utc_ts(str(cutoff.get("max_available_at"))) if cutoff.get("max_available_at") else usable_deadline,
        "ingested_at": usable_deadline,
        "finalized_at": usable_deadline,
    }
    with connect(db_path) as conn:
        for event_id in envelope_ids:
            row = conn.execute("SELECT known_at, available_at, ingested_at, payload_json FROM event_envelopes WHERE event_id = ?", (event_id,)).fetchone()
            if not row:
                errors.append(f"missing_input_event:{event_id}")
                continue
            field_values = {"known_at": row[0], "available_at": row[1], "ingested_at": row[2]}
            try:
                payload = json.loads(row[3]) if row[3] else {}
            except Exception:
                payload = {}
            if payload.get("finalized_at"):
                field_values["finalized_at"] = payload.get("finalized_at")
            for field, value in field_values.items():
                deadline = field_deadlines.get(field, usable_deadline)
                if parse_utc_ts(str(value)) > deadline:
                    errors.append(f"{field}_after_cutoff:{event_id}")
                    if field == "known_at":
                        errors.append(f"known_after_cutoff:{event_id}")
                    elif field == "available_at":
                        errors.append(f"available_after_cutoff:{event_id}")
    return {"ok": not errors, "errors": errors, "decision_cutoff": decision_cutoff.isoformat(timespec="seconds"), "latency_buffer_seconds": latency_buffer, "usable_input_deadline": usable_deadline.isoformat(timespec="seconds")}

def verify_audit_chain(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    previous = None
    errors: list[str] = []
    with connect(db_path) as conn:
        rows = conn.execute("SELECT event_id,event_type,previous_hash,audit_hash,envelope_hash FROM audit_hash_chain ORDER BY seq ASC").fetchall()
    for event_id, event_type, previous_hash_value, audit_hash_value, envelope_hash_value in rows:
        if previous_hash_value != previous:
            errors.append(f"previous_hash_mismatch:{event_id}")
        expected = sha256_json({"previous_hash": previous, "envelope_hash": envelope_hash_value, "event_id": event_id, "event_type": event_type})
        if audit_hash_value != expected:
            errors.append(f"audit_hash_mismatch:{event_id}")
        previous = audit_hash_value
    return {"ok": not errors, "errors": errors, "count": len(rows), "root": previous}

def upcast_legacy_row(row: dict[str, Any], event_type: str, producer_id: str, source_id: str, sequence: int = 0) -> dict[str, Any]:
    correlation = str(row.get("trade_id") or row.get("event_id") or row.get("id") or sequence)
    occurred = str(row.get("ts") or row.get("close_ts") or row.get("open_ts") or utc_now())
    return build_event_envelope(event_type, row, producer_id, source_id, correlation, occurred_at=occurred, sequence=sequence)

def dry_run_backfill_manifest(paths: list[Path], event_type: str, producer_id: str, source_id: str) -> dict[str, Any]:
    mapped = 0
    quarantined = 0
    files = []
    for path in paths:
        rows = []
        if path.exists():
            for idx, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines()):
                try:
                    payload = json.loads(line)
                    envelope = upcast_legacy_row(payload, event_type, producer_id, source_id, idx)
                    result = validate_event_envelope(envelope)
                    mapped += 1 if result.ok else 0
                    quarantined += 0 if result.ok else 1
                except Exception:
                    quarantined += 1
                rows.append(line)
        files.append({"path": str(path), "line_count": len(rows), "sha256": hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest() if rows else None})
    return {"schema_version": ENVELOPE_SCHEMA_VERSION, "event_type": event_type, "producer_id": producer_id, "source_id": source_id, "mapped_count": mapped, "quarantined_count": quarantined, "files": files, "can_place_live_orders": False, "live_permission": False}

def subscription_id_for(consumer_id: str, event_types: list[str]) -> str:
    return "sub_" + hashlib.sha256(canonical_json({"consumer_id": consumer_id, "event_types": sorted(event_types)}).encode("utf-8")).hexdigest()[:20]

def create_subscription(consumer_id: str, event_types: list[str], db_path: Path = DEFAULT_DB, max_unacked: int = 100, subscription_id: str | None = None) -> dict[str, Any]:
    clean_types = sorted(set(event_types))
    sid = subscription_id or subscription_id_for(consumer_id, clean_types)
    now = utc_now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO consumer_subscriptions(subscription_id,consumer_id,event_types_json,max_unacked,created_at,updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(subscription_id) DO UPDATE SET event_types_json=excluded.event_types_json,max_unacked=excluded.max_unacked,updated_at=excluded.updated_at
            """,
            (sid, consumer_id, canonical_json(clean_types), int(max_unacked), now, now),
        )
        for event_type in clean_types:
            conn.execute(
                "INSERT OR IGNORE INTO subscription_offsets(subscription_id,event_type,last_acked_seq) VALUES (?,?,0)",
                (sid, event_type),
            )
    return {"subscription_id": sid, "consumer_id": consumer_id, "event_types": clean_types, "max_unacked": int(max_unacked), "can_place_live_orders": False, "live_permission": False}

def _subscription(conn: sqlite3.Connection, subscription_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT subscription_id,consumer_id,event_types_json,max_unacked FROM consumer_subscriptions WHERE subscription_id=?", (subscription_id,)).fetchone()
    if not row:
        return None
    return {"subscription_id": row[0], "consumer_id": row[1], "event_types": json.loads(row[2]), "max_unacked": int(row[3])}

def _expire_leases(conn: sqlite3.Connection, subscription_id: str, now: str) -> None:
    conn.execute("UPDATE event_attempts SET state='expired' WHERE subscription_id=? AND state='leased' AND lease_expires_at < ?", (subscription_id, now))

def _active_unacked_count(conn: sqlite3.Connection, subscription_id: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM event_attempts WHERE subscription_id=? AND state='leased'", (subscription_id,)).fetchone()[0])

def read_events(subscription_id: str, limit: int = 50, db_path: Path = DEFAULT_DB, lease_seconds: int = 60) -> dict[str, Any]:
    now = utc_now()
    lease_token = "lease_" + hashlib.sha256(f"{subscription_id}:{now}".encode("utf-8")).hexdigest()[:20]
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        sub = _subscription(conn, subscription_id)
        if not sub:
            conn.rollback()
            return {"ok": False, "errors": ["unknown_subscription"], "events": []}
        _expire_leases(conn, subscription_id, now)
        capacity = max(0, int(sub["max_unacked"]) - _active_unacked_count(conn, subscription_id))
        take = min(max(0, int(limit)), capacity)
        events: list[dict[str, Any]] = []
        if take:
            placeholders = ",".join("?" for _ in sub["event_types"])
            rows = conn.execute(
                f"""
                SELECT e.seq,e.event_id,e.event_type,e.priority,e.envelope_json,COALESCE(o.last_acked_seq,0)
                FROM event_envelopes e
                JOIN subscription_offsets o ON o.subscription_id=? AND o.event_type=e.event_type
                WHERE e.event_type IN ({placeholders})
                  AND e.seq > o.last_acked_seq
                  AND NOT EXISTS (SELECT 1 FROM event_attempts a WHERE a.subscription_id=? AND a.event_id=e.event_id AND a.state='leased')
                  AND NOT EXISTS (SELECT 1 FROM event_dlq d WHERE d.subscription_id=? AND d.event_id=e.event_id)
                ORDER BY e.priority DESC, e.seq ASC
                LIMIT ?
                """,
                (subscription_id, *sub["event_types"], subscription_id, subscription_id, take),
            ).fetchall()
            for seq, event_id, event_type, _priority, envelope_json, _offset in rows:
                attempt_no = int(conn.execute("SELECT COUNT(*) FROM event_attempts WHERE subscription_id=? AND event_id=?", (subscription_id, event_id)).fetchone()[0]) + 1
                attempt_id = "attempt_" + hashlib.sha256(f"{subscription_id}:{event_id}:{attempt_no}:{now}".encode("utf-8")).hexdigest()[:20]
                expires = (parse_utc_ts(now) + timedelta(seconds=max(1, int(lease_seconds)))).isoformat(timespec="seconds")
                conn.execute(
                    "INSERT INTO event_attempts(attempt_id,subscription_id,event_id,event_seq,event_type,lease_token,state,attempt_no,leased_at,lease_expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (attempt_id, subscription_id, event_id, int(seq), event_type, lease_token, "leased", attempt_no, now, expires),
                )
                envelope = json.loads(envelope_json)
                envelope["bus"] = {"seq": int(seq), "attempt_id": attempt_id, "lease_token": lease_token, "lease_expires_at": expires}
                events.append(envelope)
        conn.commit()
    return {"ok": True, "subscription_id": subscription_id, "lease_token": lease_token, "events": events, "count": len(events), "can_place_live_orders": False, "live_permission": False}

def _event_seq_type(conn: sqlite3.Connection, event_id: str) -> tuple[int, str] | None:
    row = conn.execute("SELECT seq,event_type FROM event_envelopes WHERE event_id=?", (event_id,)).fetchone()
    return (int(row[0]), str(row[1])) if row else None

def ack_events(subscription_id: str, event_ids: list[str], lease_token: str, attempt_id: str, expected_seq: int | None = None, db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    now = utc_now()
    errors: list[str] = []
    acked: list[str] = []
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _expire_leases(conn, subscription_id, now)
        for event_id in event_ids:
            row = conn.execute(
                "SELECT attempt_id,event_seq,event_type,state FROM event_attempts WHERE subscription_id=? AND event_id=? AND lease_token=? AND attempt_id=?",
                (subscription_id, event_id, lease_token, attempt_id),
            ).fetchone()
            if not row:
                errors.append(f"attempt_not_found:{event_id}")
                continue
            _attempt_id, seq, event_type, state = row
            if state != "leased":
                errors.append(f"attempt_not_leased:{event_id}")
                continue
            if expected_seq is not None and int(seq) != int(expected_seq):
                errors.append(f"expected_seq_mismatch:{event_id}")
                continue
            offset = int(conn.execute("SELECT last_acked_seq FROM subscription_offsets WHERE subscription_id=? AND event_type=?", (subscription_id, event_type)).fetchone()[0])
            skipped = conn.execute(
                """
                SELECT e.event_id FROM event_envelopes e
                WHERE e.event_type=? AND e.seq>? AND e.seq<?
                  AND NOT EXISTS (SELECT 1 FROM event_attempts a WHERE a.subscription_id=? AND a.event_id=e.event_id AND a.state='processed')
                  AND NOT EXISTS (SELECT 1 FROM event_dlq d WHERE d.subscription_id=? AND d.event_id=e.event_id)
                LIMIT 1
                """,
                (event_type, offset, int(seq), subscription_id, subscription_id),
            ).fetchone()
            if skipped:
                errors.append(f"ack_would_skip_unacked_event:{event_id}")
                continue
            conn.execute("UPDATE event_attempts SET state='processed',acked_at=? WHERE attempt_id=? AND state='leased'", (now, attempt_id))
            conn.execute("UPDATE subscription_offsets SET last_acked_seq=?,acked_at=? WHERE subscription_id=? AND event_type=?", (int(seq), now, subscription_id, event_type))
            acked.append(event_id)
        conn.commit()
    return {"ok": not errors, "acked": acked, "errors": errors, "can_place_live_orders": False, "live_permission": False}

def fail_event(subscription_id: str, event_id: str, lease_token: str, attempt_id: str, error: str, db_path: Path = DEFAULT_DB, max_retries: int = 3) -> dict[str, Any]:
    now = utc_now()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _expire_leases(conn, subscription_id, now)
        row = conn.execute("SELECT event_seq,event_type,state,attempt_no FROM event_attempts WHERE subscription_id=? AND event_id=? AND lease_token=? AND attempt_id=?", (subscription_id, event_id, lease_token, attempt_id)).fetchone()
        if not row:
            conn.rollback()
            return {"ok": False, "errors": ["attempt_not_found"]}
        seq, event_type, state, attempt_no = int(row[0]), str(row[1]), str(row[2]), int(row[3])
        if state != "leased":
            conn.rollback()
            return {"ok": False, "errors": ["attempt_not_leased"]}
        cause = hashlib.sha256(str(error).encode("utf-8")).hexdigest()[:20]
        if attempt_no >= max_retries:
            payload_json = conn.execute("SELECT envelope_json FROM event_envelopes WHERE event_id=?", (event_id,)).fetchone()[0]
            dlq_id = "dlq_" + hashlib.sha256(f"{subscription_id}:{event_id}".encode("utf-8")).hexdigest()[:20]
            conn.execute("UPDATE event_attempts SET state='dlq',error=?,retry_cause_hash=? WHERE attempt_id=?", (error[:500], cause, attempt_id))
            conn.execute("INSERT OR IGNORE INTO event_dlq(dlq_id,subscription_id,event_id,event_seq,event_type,reason,retry_count,created_at,payload_json) VALUES (?,?,?,?,?,?,?,?,?)", (dlq_id, subscription_id, event_id, seq, event_type, error[:500], attempt_no, now, payload_json))
            conn.commit()
            return {"ok": True, "state": "dlq", "dlq_id": dlq_id, "retry_count": attempt_no, "can_place_live_orders": False, "live_permission": False}
        conn.execute("UPDATE event_attempts SET state='failed',error=?,retry_cause_hash=? WHERE attempt_id=?", (error[:500], cause, attempt_id))
        conn.commit()
    return {"ok": True, "state": "retry", "retry_count": attempt_no, "can_place_live_orders": False, "live_permission": False}

def replay_events(db_path: Path = DEFAULT_DB, event_ids: list[str] | None = None, event_types: list[str] | None = None, source_id: str | None = None, after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
    clauses = ["seq > ?"]
    params: list[Any] = [int(after_seq)]
    if event_ids:
        clauses.append("event_id IN (" + ",".join("?" for _ in event_ids) + ")")
        params.extend(event_ids)
    if event_types:
        clauses.append("event_type IN (" + ",".join("?" for _ in event_types) + ")")
        params.extend(event_types)
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    params.append(int(limit))
    with connect(db_path) as conn:
        rows = conn.execute(f"SELECT seq,envelope_json FROM event_envelopes WHERE {' AND '.join(clauses)} ORDER BY seq ASC LIMIT ?", params).fetchall()
    events = []
    for seq, envelope_json in rows:
        envelope = json.loads(envelope_json)
        envelope["bus"] = {"seq": int(seq)}
        events.append(envelope)
    return {"ok": True, "events": events, "count": len(events), "can_place_live_orders": False, "live_permission": False}

def bus_health(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    with connect(db_path) as conn:
        event_count = conn.execute("SELECT COUNT(*) FROM event_envelopes").fetchone()[0]
        dlq_count = conn.execute("SELECT COUNT(*) FROM event_dlq").fetchone()[0]
        unacked = conn.execute("SELECT COUNT(*) FROM event_attempts WHERE state='leased'").fetchone()[0]
        subs = conn.execute("SELECT COUNT(*) FROM consumer_subscriptions").fetchone()[0]
    return {"schema_version": ENVELOPE_SCHEMA_VERSION, "updated_at": utc_now(), "status": "ok" if dlq_count == 0 else "degraded", "event_count": event_count, "dlq_count": dlq_count, "unacked_count": unacked, "subscription_count": subs, "can_place_live_orders": False, "live_permission": False}

def write_bus_health(db_path: Path = DEFAULT_DB, latest_path: Path = BUS_HEALTH_LATEST, history_path: Path = BUS_HEALTH_HISTORY) -> dict[str, Any]:
    from atomic_state import append_jsonl, write_json_atomic

    payload = bus_health(db_path)
    write_json_atomic(latest_path, payload)
    append_jsonl(history_path, payload)
    return payload

def validate_replay_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    errors = [f"missing_manifest:{field}" for field in sorted(REPLAY_MANIFEST_REQUIRED_FIELDS) if not manifest.get(field)]
    if not manifest.get("event_seq_start") and manifest.get("event_seq_start") != 0:
        errors.append("missing_manifest:event_seq_start")
    if not manifest.get("event_seq_end") and manifest.get("event_seq_end") != 0:
        errors.append("missing_manifest:event_seq_end")
    if manifest.get("event_seq_start") is not None and manifest.get("event_seq_end") is not None and int(manifest["event_seq_end"]) < int(manifest["event_seq_start"]):
        errors.append("manifest_seq_range_invalid")
    return {"ok": not errors, "errors": errors, "can_place_live_orders": False, "live_permission": False}

def replay_with_manifest(manifest: dict[str, Any], db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    validation = validate_replay_manifest(manifest)
    if not validation["ok"]:
        return {**validation, "events": [], "non_replayable_reason": ";".join(validation["errors"])}
    return replay_events(db_path=db_path, after_seq=int(manifest["event_seq_start"]) - 1, limit=max(1, int(manifest["event_seq_end"]) - int(manifest["event_seq_start"]) + 1))

def dual_write_shadow_counts(old_count: int, new_count: int) -> dict[str, Any]:
    return {"ok": int(old_count) == int(new_count), "old_count": int(old_count), "new_count": int(new_count), "delta": int(new_count) - int(old_count)}

def cutover_checklist_status(items: dict[str, bool]) -> dict[str, Any]:
    required = ["snapshot_backup", "drain_unacked", "compatibility_adapter", "rollback_command"]
    missing = [item for item in required if not items.get(item)]
    return {"ok": not missing, "missing": missing, "required": required}

def evaluate_backpressure(health: dict[str, Any], thresholds: dict[str, int] | None = None) -> dict[str, Any]:
    thresholds = thresholds or {"max_unacked": 1000, "max_dlq": 0}
    pause_low_priority = int(health.get("unacked_count") or 0) > thresholds["max_unacked"] or int(health.get("dlq_count") or 0) > thresholds["max_dlq"]
    return {"pause_low_priority_producers": pause_low_priority, "can_accept_core_events": True, "thresholds": thresholds}

def create_live_backup(db_path: Path, backup_path: Path, manifest_path: Path, owner: str, checker: str, restore_approver: str, runbook_id: str = "RUNBOOK-EVENT-BUS-BACKUP") -> dict[str, Any]:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = backup_path.with_name(f".{backup_path.name}.tmp")
    with connect(db_path) as source:
        source.execute("PRAGMA wal_checkpoint(FULL)")
        dest = sqlite3.connect(str(tmp_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    tmp_path.replace(backup_path)
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "created_at": utc_now(),
        "db_path": str(db_path),
        "backup_path": str(backup_path),
        "sha256": digest,
        "owner": owner,
        "checker": checker,
        "restore_approver": restore_approver,
        "runbook_id": runbook_id,
        "windows_io_lock_policy": WINDOWS_IO_LOCK_POLICY,
        "can_place_live_orders": False,
        "live_permission": False,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest

def append_erasure_receipt(payload_id: str, reason: str, key_id: str, receipt_path: Path = ERASURE_RECEIPTS) -> dict[str, Any]:
    from atomic_state import append_jsonl

    row = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "receipt_id": "erase_" + hashlib.sha256(f"{payload_id}:{key_id}".encode("utf-8")).hexdigest()[:20],
        "payload_id": payload_id,
        "reason": reason,
        "key_id": key_id,
        "erased_at": utc_now(),
        "metadata_hash": sha256_json({"payload_id": payload_id, "reason": reason, "key_id": key_id}),
        "payload_recoverable": False,
        "can_place_live_orders": False,
        "live_permission": False,
    }
    append_jsonl(receipt_path, row)
    return row

def validate_restore_replay(source_db: Path, restored_db: Path) -> dict[str, Any]:
    source = bus_health(source_db)
    restored = bus_health(restored_db)
    errors = []
    if source["event_count"] != restored["event_count"]:
        errors.append("event_count_mismatch")
    if source["dlq_count"] != restored["dlq_count"]:
        errors.append("dlq_count_mismatch")
    return {"ok": not errors, "errors": errors, "source": source, "restored": restored}


def infer_symbol(payload: dict) -> str | None:
    for key in ("symbol", "token_symbol"):
        if payload.get(key):
            return str(payload[key]).upper()
    for nested_key in ("signal", "position"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict) and nested.get("symbol"):
            return str(nested["symbol"]).upper()
    return None


def infer_side(payload: dict) -> str | None:
    for key in ("side",):
        if payload.get(key):
            return str(payload[key]).upper()
    for nested_key in ("signal", "position"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict) and nested.get("side"):
            return str(nested["side"]).upper()
    return None


def append_event(source: str, event: str, payload: dict, ts: str | None = None, db_path: Path = DEFAULT_DB) -> None:
    row_ts = ts or str(payload.get("ts") or utc_now())
    payload_clean = {k: v for k, v in payload.items() if k != "ts"}
    payload_json = canonical_json(payload_clean)
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO events(ts, source, event, symbol, side, event_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row_ts,
                source,
                event,
                infer_symbol(payload_clean),
                infer_side(payload_clean),
                event_hash(source, event, row_ts, payload_clean),
                payload_json,
            ),
        )


def append_snapshot(source: str, kind: str, payload: dict, ts: str | None = None, db_path: Path = DEFAULT_DB) -> None:
    row_ts = ts or str(payload.get("ts") or utc_now())
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO snapshots(ts, source, kind, payload_json) VALUES (?, ?, ?, ?)",
            (row_ts, source, kind, canonical_json(payload)),
        )


def upsert_heartbeat(source: str, status: str, payload: dict, ts: str | None = None, db_path: Path = DEFAULT_DB) -> None:
    row_ts = ts or str(payload.get("ts") or utc_now())
    payload_clean = {k: v for k, v in payload.items() if k != "ts"}
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO heartbeats(source, ts, status, payload_json) VALUES (?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET ts=excluded.ts, status=excluded.status, payload_json=excluded.payload_json
            """,
            (source, row_ts, status, canonical_json(payload_clean)),
        )


def safe_append_event(source: str, event: str, payload: dict, ts: str | None = None) -> None:
    try:
        append_event(source, event, payload, ts)
    except Exception:
        pass


def safe_append_snapshot(source: str, kind: str, payload: dict, ts: str | None = None) -> None:
    try:
        append_snapshot(source, kind, payload, ts)
    except Exception:
        pass


def safe_upsert_heartbeat(source: str, status: str, payload: dict, ts: str | None = None) -> None:
    try:
        upsert_heartbeat(source, status, payload, ts)
    except Exception:
        pass


def query_recent_events(
    source: str | None = None,
    events: Sequence[str] | None = None,
    lookback_hours: float = 24.0,
    limit: int = 500,
    db_path: Path = DEFAULT_DB,
) -> list[dict]:
    clauses = ["ts >= ?"]
    params: list[object] = [(datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat(timespec="seconds")]
    if source:
        clauses.append("source = ?")
        params.append(source)
    if events:
        placeholders = ",".join("?" for _ in events)
        clauses.append(f"event IN ({placeholders})")
        params.extend(events)
    params.append(max(1, int(limit)))
    sql = f"""
        SELECT ts, source, event, symbol, side, payload_json
        FROM events
        WHERE {' AND '.join(clauses)}
        ORDER BY id DESC
        LIMIT ?
    """
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    result: list[dict] = []
    for ts, row_source, event, symbol, side, payload_json in reversed(rows):
        try:
            payload = json.loads(payload_json)
        except Exception:
            payload = {"payload_json": payload_json}
        row = {"ts": ts, "source": row_source, "event": event, **payload}
        if symbol and "symbol" not in row:
            row["symbol"] = symbol
        if side and "side" not in row:
            row["side"] = side
        result.append(row)
    return result

def backfill_jsonl(path: Path, source: str, default_event: str = "event", db_path: Path = DEFAULT_DB) -> int:
    if not path.exists():
        return 0
    count = 0
    with connect(db_path) as conn:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            event = str(row.get("event") or default_event)
            ts = str(row.get("ts") or utc_now())
            payload = {k: v for k, v in row.items() if k not in {"ts", "event"}}
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO events(ts, source, event, symbol, side, event_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, source, event, infer_symbol(payload), infer_side(payload), event_hash(source, event, ts, payload), canonical_json(payload)),
            )
            if conn.total_changes > before:
                count += 1
    return count


def stats(db_path: Path = DEFAULT_DB) -> dict:
    with connect(db_path) as conn:
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        snapshot_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        heartbeat_count = conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0]
        recent = conn.execute(
            "SELECT ts, source, event, symbol, side FROM events ORDER BY id DESC LIMIT 5"
        ).fetchall()
    return {
        "db": str(db_path),
        "events": event_count,
        "snapshots": snapshot_count,
        "heartbeats": heartbeat_count,
        "recent_events": recent,
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SQLite event store utilities")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.init:
        with connect(DEFAULT_DB):
            pass
        print(f"initialized {DEFAULT_DB}")
    if args.backfill:
        files = [
            (STATE_DIR / "scalp_autotrader.jsonl", "scalp_autotrader", "event"),
            (STATE_DIR / "scalp_watchdog.jsonl", "scalp_watchdog", "event"),
            (STATE_DIR / "market_updates.jsonl", "market_observer", "market_update"),
            (STATE_DIR / "agent_memory" / "lessons.jsonl", "reflection_agent", "lesson"),
        ]
        for path, source, default_event in files:
            print(f"backfilled {backfill_jsonl(path, source, default_event)} rows from {path}")
    if args.stats or (not args.init and not args.backfill):
        print(json.dumps(stats(), ensure_ascii=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
