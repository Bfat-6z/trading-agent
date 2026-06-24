"""SQLite FTS5 retrieval memory for episodes, reviews, replays, and rules."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, read_jsonl
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
DEFAULT_DB = STATE_DIR / "memory_retrieval.db"


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(doc_id, kind, status, text, payload_json)")
    return conn


def row_text(row: dict[str, Any]) -> str:
    parts = []
    for key in ("lesson", "classification", "conclusion", "condition", "text", "goal", "next_action", "setup_id", "symbol", "regime"):
        if row.get(key):
            parts.append(str(row[key]))
    return " ".join(parts) or json.dumps(row, ensure_ascii=True, sort_keys=True)[:1000]


def upsert_document(conn: sqlite3.Connection, doc_id: str, kind: str, row: dict[str, Any], status: str = "active") -> None:
    conn.execute("DELETE FROM memory_fts WHERE doc_id = ?", (doc_id,))
    conn.execute("INSERT INTO memory_fts(doc_id, kind, status, text, payload_json) VALUES (?, ?, ?, ?, ?)", (doc_id, kind, status, row_text(row), json.dumps(row, ensure_ascii=True, sort_keys=True)))


def rebuild_index(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    sources = [
        ("episode", MEMORY_DIR / "episodes.jsonl", "episode_id"),
        ("post_trade_review", MEMORY_DIR / "post_trade_reviews.jsonl", "review_id"),
        ("counterfactual", MEMORY_DIR / "counterfactual_replays.jsonl", "replay_id"),
        ("promoted_memory", MEMORY_DIR / "memory_promoted.jsonl", "memory_id"),
    ]
    count = 0
    with connect(db_path) as conn:
        conn.execute("DELETE FROM memory_fts")
        for kind, path, id_field in sources:
            for row in read_jsonl(path):
                doc_id = str(row.get(id_field) or f"{kind}_{count}")
                upsert_document(conn, doc_id, kind, row, status=str(row.get("status") or "active"))
                count += 1
        dont_do = read_json(MEMORY_DIR / "dont_do_memory.json", default={})
        for row in dont_do.get("rules", []) if isinstance(dont_do.get("rules"), list) else []:
            upsert_document(conn, str(row.get("rule_id")), "dont_do", row, status="active")
            count += 1
    return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "indexed": count, "db_path": str(db_path)}


def search_memory(query: str, db_path: Path = DEFAULT_DB, limit: int = 10, include_retired: bool = False) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    sql = "SELECT doc_id, kind, status, text, payload_json, rank FROM memory_fts WHERE memory_fts MATCH ?"
    params: list[Any] = [query]
    if not include_retired:
        sql += " AND status NOT IN ('retired', 'expired')"
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    rows = []
    with connect(db_path) as conn:
        for doc_id, kind, status, text, payload_json, rank in conn.execute(sql, params).fetchall():
            try:
                payload = json.loads(payload_json)
            except Exception:
                payload = {}
            rows.append({"doc_id": doc_id, "kind": kind, "status": status, "text": text, "rank": rank, "payload": payload})
    return rows
