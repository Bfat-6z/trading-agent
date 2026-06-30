"""SQLite FTS5 retrieval memory with time-safe active recall."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, read_jsonl, write_json_atomic
from data_trust import prepare_llm_egress, sanitize_external_text
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
DEFAULT_DB = STATE_DIR / "memory_retrieval.db"
RECALL_LATEST = MEMORY_DIR / "active_recall_latest.json"
RECALL_HISTORY = MEMORY_DIR / "active_recall_history.jsonl"

ID_FIELDS = (
    "memory_id",
    "episode_id",
    "review_id",
    "replay_id",
    "exam_id",
    "reasoning_id",
    "rule_id",
    "setup_id",
    "patch_id",
    "task_id",
)
TEXT_KEYS = (
    "lesson",
    "classification",
    "conclusion",
    "condition",
    "text",
    "claim",
    "statement",
    "goal",
    "next_action",
    "setup_id",
    "symbol",
    "side",
    "regime",
    "market_read",
    "summary",
    "critical_blindspots",
)
TIME_KEYS = (
    "outcome_known_at",
    "evidence_outcome_known_at",
    "memory_promoted_at",
    "promoted_at",
    "reviewed_at",
    "closed_at",
    "close_ts",
    "completed_at",
    "updated_at",
    "created_at",
    "ts",
)
BLOCK_TOKENS = ("avoid", "do not", "don't", "bad_loss", "chase", "too wide", "high risk", "block", "khong", "không")
TAINTED_SOURCES = {
    "news",
    "rss",
    "news_api",
    "external_news",
    "social",
    "telegram",
    "reddit",
    "x",
    "twitter",
    "discord",
    "forum",
    "external_social",
    "manual",
    "manual_text",
    "manual_screenshot",
    "screenshot",
    "manual_claim",
    "operator_text",
    "operator_feedback",
    "llm",
    "model",
    "llm_generated",
    "private_external",
}


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    migrate_schema(conn)
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.DatabaseError:
        return set()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT name FROM sqlite_master WHERE name = ?", (table,)).fetchone())


def migrate_schema(conn: sqlite3.Connection) -> None:
    if table_exists(conn, "memory_docs"):
        required = {"doc_id", "kind", "status", "text", "payload_json", "setup", "symbol", "side", "regime", "source", "event_ts", "promoted_at", "evidence_known_at", "retired_at", "readiness_holdout", "trial_partition_id", "allowed_effect", "updated_at"}
        if not required.issubset(table_columns(conn, "memory_docs")):
            conn.execute("DROP TABLE IF EXISTS memory_docs")
    if table_exists(conn, "memory_fts"):
        required_fts = {"doc_id", "kind", "status", "text", "payload_json"}
        if not required_fts.issubset(table_columns(conn, "memory_fts")):
            conn.execute("DROP TABLE IF EXISTS memory_fts")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_docs (
            doc_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            text TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            setup TEXT,
            symbol TEXT,
            side TEXT,
            regime TEXT,
            source TEXT,
            event_ts TEXT,
            promoted_at TEXT,
            evidence_known_at TEXT,
            retired_at TEXT,
            readiness_holdout INTEGER NOT NULL DEFAULT 0,
            trial_partition_id TEXT,
            allowed_effect TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(doc_id, kind, status, text, payload_json)")


def first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def doc_id_for(kind: str, row: dict[str, Any], fallback: str) -> str:
    for key in ID_FIELDS:
        value = row.get(key)
        if value:
            return str(value)
    return fallback


def flatten_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(flatten_text(item) for item in value.values())
    return str(value)


def row_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in TEXT_KEYS:
        if row.get(key):
            parts.append(flatten_text(row[key]))
    text = " ".join(part for part in parts if part).strip()
    if not text:
        text = json.dumps(row, ensure_ascii=True, sort_keys=True)[:1200]
    source = str(first_present(row, "source_type", "source", "_memory_source_type") or "").lower()
    if row.get("taint_class") in {"external_social", "external_news", "manual_claim", "private_external", "operator_feedback", "llm_generated"} or source in TAINTED_SOURCES:
        return sanitize_external_text(text)["text"]
    return " ".join(text.split())[:2000]


def source_ts(row: dict[str, Any]) -> str:
    return str(first_present(row, *TIME_KEYS) or utc_now())


def metadata_for(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or ("retired" if row.get("retired_at") else "active")).lower()
    return {
        "setup": first_present(row, "setup_id", "setup"),
        "symbol": str(first_present(row, "symbol") or "").upper() or None,
        "side": str(first_present(row, "side") or "").upper() or None,
        "regime": first_present(row, "regime", "market_regime"),
        "source": first_present(row, "source_type", "source", "_memory_source_type") or kind,
        "event_ts": source_ts(row),
        "promoted_at": first_present(row, "memory_promoted_at", "promoted_at"),
        "evidence_known_at": first_present(row, "evidence_outcome_known_at", "outcome_known_at", "closed_at", "reviewed_at"),
        "retired_at": row.get("retired_at"),
        "readiness_holdout": 1 if row.get("readiness_holdout") or row.get("frozen_readiness_holdout_id") else 0,
        "trial_partition_id": row.get("trial_partition_id"),
        "allowed_effect": row.get("allowed_effect"),
        "status": status,
    }


def upsert_document(conn: sqlite3.Connection, doc_id: str, kind: str, row: dict[str, Any], status: str | None = None) -> None:
    text = row_text(row)
    metadata = metadata_for(kind, row)
    if status is not None:
        metadata["status"] = str(status).lower()
    payload_json = json.dumps(row, ensure_ascii=True, sort_keys=True)
    conn.execute("DELETE FROM memory_fts WHERE doc_id = ?", (doc_id,))
    conn.execute(
        "INSERT INTO memory_fts(doc_id, kind, status, text, payload_json) VALUES (?, ?, ?, ?, ?)",
        (doc_id, kind, metadata["status"], text, payload_json),
    )
    conn.execute(
        """
        INSERT INTO memory_docs(
            doc_id, kind, status, text, payload_json, setup, symbol, side, regime, source,
            event_ts, promoted_at, evidence_known_at, retired_at, readiness_holdout,
            trial_partition_id, allowed_effect, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(doc_id) DO UPDATE SET
            kind=excluded.kind,
            status=excluded.status,
            text=excluded.text,
            payload_json=excluded.payload_json,
            setup=excluded.setup,
            symbol=excluded.symbol,
            side=excluded.side,
            regime=excluded.regime,
            source=excluded.source,
            event_ts=excluded.event_ts,
            promoted_at=excluded.promoted_at,
            evidence_known_at=excluded.evidence_known_at,
            retired_at=excluded.retired_at,
            readiness_holdout=excluded.readiness_holdout,
            trial_partition_id=excluded.trial_partition_id,
            allowed_effect=excluded.allowed_effect,
            updated_at=excluded.updated_at
        """,
        (
            doc_id,
            kind,
            metadata["status"],
            text,
            payload_json,
            metadata["setup"],
            metadata["symbol"],
            metadata["side"],
            metadata["regime"],
            metadata["source"],
            metadata["event_ts"],
            metadata["promoted_at"],
            metadata["evidence_known_at"],
            metadata["retired_at"],
            metadata["readiness_holdout"],
            metadata["trial_partition_id"],
            metadata["allowed_effect"],
            utc_now(),
        ),
    )


def iter_sources() -> list[tuple[str, Path, str]]:
    return [
        ("episode", MEMORY_DIR / "episodes.jsonl", "episode_id"),
        ("post_trade_review", MEMORY_DIR / "post_trade_reviews.jsonl", "review_id"),
        ("counterfactual", MEMORY_DIR / "counterfactual_replays.jsonl", "replay_id"),
        ("daily_exam", MEMORY_DIR / "daily_exam_history.jsonl", "exam_id"),
        ("test_result", MEMORY_DIR / "test_result_memory_history.jsonl", "test_id"),
        ("llm_reasoning", MEMORY_DIR / "llm_reasoning_history.jsonl", "reasoning_id"),
        ("promoted_memory", MEMORY_DIR / "memory_promoted.jsonl", "memory_id"),
        ("skill_forge_task", MEMORY_DIR / "memory_skill_forge_queue.jsonl", "task_id"),
        ("skill_patch_review", MEMORY_DIR / "skill_patch_review_history.jsonl", "patch_id"),
    ]


def rebuild_index(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    count = 0
    with connect(db_path) as conn:
        conn.execute("DELETE FROM memory_fts")
        conn.execute("DELETE FROM memory_docs")
        for kind, path, id_field in iter_sources():
            for row in read_jsonl(path):
                if not isinstance(row, dict):
                    continue
                doc_id = str(row.get(id_field) or doc_id_for(kind, row, f"{kind}_{count}"))
                upsert_document(conn, doc_id, kind, row, status=str(row.get("status") or "active"))
                count += 1
        setup_skills = read_json(MEMORY_DIR / "setup_skills.json", default={})
        skills = setup_skills.get("skills") if isinstance(setup_skills.get("skills"), dict) else {}
        for setup_id, row in sorted(skills.items()):
            if isinstance(row, dict):
                upsert_document(conn, str(setup_id), "setup_skill", {**row, "setup_id": setup_id}, status=str(row.get("status") or "active"))
                count += 1
        dont_do = read_json(MEMORY_DIR / "dont_do_memory.json", default={})
        for row in dont_do.get("rules", []) if isinstance(dont_do.get("rules"), list) else []:
            if isinstance(row, dict):
                upsert_document(conn, str(row.get("rule_id")), "dont_do", row, status="active" if not row.get("retired_at") else "retired")
                count += 1
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('optimize')")
    return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "indexed": count, "db_path": str(db_path)}


def fts_query(text: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]{2,}", str(text or "").lower())
    terms = list(dict.fromkeys(terms))[:12]
    return " OR ".join(f'"{term}"' for term in terms)


def ts_lte(value: Any, cutoff: Any) -> bool:
    if not cutoff:
        return True
    parsed = parse_utc(value)
    cutoff_dt = parse_utc(cutoff)
    if not parsed or not cutoff_dt:
        return False
    return parsed <= cutoff_dt


def doc_time_safe(row: dict[str, Any], decision_cutoff: str | None = None, exclude_trial_partition_id: str | None = None) -> bool:
    if row.get("readiness_holdout"):
        return False
    if exclude_trial_partition_id and row.get("trial_partition_id") == exclude_trial_partition_id:
        return False
    status = str(row.get("status") or "").lower()
    if status in {"retired", "expired", "tombstoned"} or row.get("retired_at"):
        return False
    if not decision_cutoff:
        return True
    kind = str(row.get("kind") or "")
    event_ts = row.get("event_ts")
    if event_ts and not ts_lte(event_ts, decision_cutoff):
        return False
    if kind == "promoted_memory":
        if not row.get("promoted_at") or not row.get("evidence_known_at"):
            return False
        if not ts_lte(row.get("promoted_at"), decision_cutoff):
            return False
        if not ts_lte(row.get("evidence_known_at"), decision_cutoff):
            return False
    return True


def search_memory(
    query: str,
    db_path: Path = DEFAULT_DB,
    limit: int = 10,
    include_retired: bool = False,
    filters: dict[str, Any] | None = None,
    decision_cutoff: str | None = None,
    exclude_trial_partition_id: str | None = None,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    match = fts_query(query)
    if not match:
        return []
    sql = """
        SELECT d.doc_id, d.kind, d.status, d.text, d.payload_json, d.setup, d.symbol, d.side, d.regime,
               d.source, d.event_ts, d.promoted_at, d.evidence_known_at, d.retired_at,
               d.readiness_holdout, d.trial_partition_id, d.allowed_effect, f.rank
        FROM memory_fts f
        JOIN memory_docs d ON d.doc_id = f.doc_id
        WHERE memory_fts MATCH ?
    """
    params: list[Any] = [match]
    filters = filters or {}
    if not include_retired:
        sql += " AND lower(d.status) NOT IN ('retired', 'expired', 'tombstoned') AND d.retired_at IS NULL"
    for field in ("setup", "symbol", "side", "regime", "source", "kind"):
        value = filters.get(field)
        if value:
            sql += f" AND d.{field} = ?"
            params.append(str(value).upper() if field in {"symbol", "side"} else str(value))
    sql += " ORDER BY f.rank LIMIT ?"
    params.append(max(1, int(limit) * 3))
    rows = []
    start = time.perf_counter()
    with connect(db_path) as conn:
        result = conn.execute(sql, params).fetchall()
    latency_ms = round((time.perf_counter() - start) * 1000, 4)
    for raw in result:
        (
            doc_id,
            kind,
            status,
            text,
            payload_json,
            setup,
            symbol,
            side,
            regime,
            source,
            event_ts,
            promoted_at,
            evidence_known_at,
            retired_at,
            readiness_holdout,
            trial_partition_id,
            allowed_effect,
            rank,
        ) = raw
        doc = {
            "doc_id": doc_id,
            "kind": kind,
            "status": status,
            "text": text,
            "setup": setup,
            "symbol": symbol,
            "side": side,
            "regime": regime,
            "source": source,
            "event_ts": event_ts,
            "promoted_at": promoted_at,
            "evidence_known_at": evidence_known_at,
            "retired_at": retired_at,
            "readiness_holdout": bool(readiness_holdout),
            "trial_partition_id": trial_partition_id,
            "allowed_effect": allowed_effect,
        }
        if not doc_time_safe(doc, decision_cutoff=decision_cutoff, exclude_trial_partition_id=exclude_trial_partition_id):
            continue
        try:
            payload = json.loads(payload_json)
        except Exception:
            payload = {}
        if str(doc.get("source") or "").lower() in TAINTED_SOURCES or str(payload.get("source_type") or payload.get("source") or "").lower() in TAINTED_SOURCES:
            payload = {**payload, "taint_class": payload.get("taint_class") or "external_news"}
        egress = prepare_llm_egress(payload, "memory_retrieval")
        rows.append(
            {
                **doc,
                "rank": rank,
                "latency_ms": latency_ms,
                "payload": egress["payload"],
                "egress_proof": egress["proof"],
            }
        )
        if len(rows) >= limit:
            break
    return rows


def recall_query_for_signal(signal: dict[str, Any]) -> str:
    parts = [
        signal.get("setup_id"),
        signal.get("symbol"),
        signal.get("side"),
        signal.get("regime"),
        signal.get("market_regime"),
    ]
    reasons = signal.get("reasons") if isinstance(signal.get("reasons"), list) else []
    parts.extend(reasons[:5])
    return " ".join(str(part) for part in parts if part)


def recall_filters_for_signal(signal: dict[str, Any]) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for source, target in (("setup_id", "setup"), ("symbol", "symbol"), ("side", "side"), ("regime", "regime")):
        value = signal.get(source)
        if value:
            filters[target] = str(value).upper() if target in {"symbol", "side"} else str(value)
    return filters


def recall_decision_effect(hits: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: list[str] = []
    blockers: list[str] = []
    for hit in hits:
        text = str(hit.get("text") or "").lower()
        kind = str(hit.get("kind") or "")
        payload = hit.get("payload") if isinstance(hit.get("payload"), dict) else {}
        severity = str(payload.get("severity") or "")
        if kind == "dont_do" and severity == "high":
            blockers.append(str(hit.get("doc_id")))
        elif kind in {"dont_do", "promoted_memory"} and any(token in text for token in BLOCK_TOKENS):
            reasons.append(str(hit.get("doc_id")))
    if blockers:
        return {"action": "block", "reason": "active_recall_dont_do_block", "memory_ids": blockers, "can_loosen": False}
    if reasons:
        return {"action": "tighten", "reason": "active_recall_risk_memory", "memory_ids": reasons[:5], "can_loosen": False}
    return {"action": "none", "reason": "no_recall_delta", "memory_ids": [], "can_loosen": False}


def active_recall_for_decision(
    signal: dict[str, Any],
    db_path: Path = DEFAULT_DB,
    decision_cutoff: str | None = None,
    limit: int = 8,
    exclude_trial_partition_id: str | None = None,
    write_state: bool = False,
) -> dict[str, Any]:
    cutoff = decision_cutoff or str(signal.get("decision_cutoff") or signal.get("market_snapshot_ts") or utc_now())
    if exclude_trial_partition_id is None and signal.get("trial_partition_id"):
        exclude_trial_partition_id = str(signal.get("trial_partition_id"))
    query = recall_query_for_signal(signal)
    base_filters = recall_filters_for_signal(signal)
    hits = search_memory(query, db_path=db_path, limit=limit, filters={}, decision_cutoff=cutoff, exclude_trial_partition_id=exclude_trial_partition_id)
    filtered_hits = []
    for hit in hits:
        if base_filters.get("symbol") and hit.get("symbol") and hit.get("symbol") != base_filters["symbol"]:
            continue
        if base_filters.get("side") and hit.get("side") and hit.get("side") != base_filters["side"]:
            continue
        if base_filters.get("setup") and hit.get("setup") and hit.get("setup") != base_filters["setup"]:
            continue
        if base_filters.get("regime") and hit.get("regime") and hit.get("regime") != base_filters["regime"]:
            continue
        filtered_hits.append(hit)
    effect = recall_decision_effect(filtered_hits)
    report = {
        "schema_version": SCHEMA_VERSION,
        "recalled_at": utc_now(),
        "decision_cutoff": cutoff,
        "query": query,
        "filters": base_filters,
        "hit_count": len(filtered_hits),
        "active_recall_hit_rate": 1.0 if filtered_hits else 0.0,
        "memory_ids_used": [str(hit.get("doc_id")) for hit in filtered_hits],
        "dont_do_hits": [str(hit.get("doc_id")) for hit in filtered_hits if hit.get("kind") == "dont_do"],
        "hits": filtered_hits[:limit],
        "decision_delta": effect,
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    if write_state:
        write_json_atomic(RECALL_LATEST, report)
        append_jsonl(RECALL_HISTORY, report)
    return report
