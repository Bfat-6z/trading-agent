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
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DEFAULT_DB = STATE_DIR / "agent_state.db"


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
