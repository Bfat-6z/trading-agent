import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import event_store as es


def test_append_event_infers_symbol_side_and_queries_recent(tmp_path: Path):
    db_path = tmp_path / "agent_state.db"
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    es.append_event(
        "scalp_autotrader",
        "paper_open",
        {"position": {"symbol": "hypeusdt", "side": "long"}, "net": "0"},
        ts=ts,
        db_path=db_path,
    )

    rows = es.query_recent_events(
        source="scalp_autotrader",
        events=["paper_open"],
        lookback_hours=1,
        limit=10,
        db_path=db_path,
    )

    assert len(rows) == 1
    assert rows[0]["event"] == "paper_open"
    assert rows[0]["symbol"] == "HYPEUSDT"
    assert rows[0]["side"] == "LONG"


def test_snapshot_and_heartbeat_are_written(tmp_path: Path):
    db_path = tmp_path / "agent_state.db"
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    es.append_snapshot("market_observer", "market_update", {"ts": ts, "hot": [{"symbol": "BTCUSDT"}]}, db_path=db_path)
    es.upsert_heartbeat("market_observer", "ok", {"ts": ts, "hot": "BTCUSDT"}, db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        snapshot_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        heartbeat = conn.execute("SELECT status, payload_json FROM heartbeats WHERE source = ?", ("market_observer",)).fetchone()

    assert snapshot_count == 1
    assert heartbeat[0] == "ok"
    assert json.loads(heartbeat[1])["hot"] == "BTCUSDT"


def test_backfill_jsonl_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "agent_state.db"
    jsonl_path = tmp_path / "events.jsonl"
    jsonl_path.write_text(
        '\n'.join(
            [
                '{"ts":"2026-06-20T00:00:00+00:00","event":"signal","signal":{"symbol":"SOLUSDT","side":"SHORT"}}',
                '{"ts":"2026-06-20T00:01:00+00:00","event":"paper_close","symbol":"SOLUSDT","side":"SHORT","net":"0.12"}',
                'not json',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    first = es.backfill_jsonl(jsonl_path, "scalp_autotrader", db_path=db_path)
    second = es.backfill_jsonl(jsonl_path, "scalp_autotrader", db_path=db_path)
    stats = es.stats(db_path)

    assert first == 2
    assert second == 0
    assert stats["events"] == 2
