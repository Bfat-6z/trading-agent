import json
from pathlib import Path

from memory_compactor import compact_memory, summarize_events


def test_compacts_event_rows_into_deterministic_summary():
    rows = [
        {"source": "scalp_autotrader", "event": "paper_open", "symbol": "BTCUSDT", "side": "LONG"},
        {"source": "scalp_autotrader", "event": "paper_close", "symbol": "BTCUSDT", "side": "LONG", "net": "0.15"},
        {"source": "scalp_autotrader", "event": "paper_close", "symbol": "ETHUSDT", "side": "SHORT", "net": "-0.05"},
        {"source": "scalp_autotrader", "event": "risk_block", "reason": "memory_sleep"},
        {"source": "reflection_agent", "event": "lesson", "lesson": "Keep live trading disabled until expectancy improves."},
    ]

    summary = summarize_events(rows, ts="2026-06-20T00:00:00+00:00")

    assert summary["event_count"] == 5
    assert summary["counts_by_event"] == {"lesson": 1, "paper_close": 2, "paper_open": 1, "risk_block": 1}
    assert summary["paper"]["closes"] == 2
    assert summary["paper"]["wins"] == 1
    assert summary["paper"]["losses"] == 1
    assert summary["paper"]["net"] == 0.1
    assert summary["risk_blocks"] == {"memory_sleep": 1}
    assert summary["top_symbols"][0] == {"symbol": "BTCUSDT", "count": 2}


def test_does_not_duplicate_promoted_beliefs(tmp_path: Path):
    rows = [
        {"source": "reflection_agent", "event": "lesson", "lesson": "Repeated lesson should become one belief."},
        {"source": "reflection_agent", "event": "lesson", "lesson": "Repeated lesson should become one belief."},
    ]
    semantic_path = tmp_path / "semantic_memory.json"
    ledger_path = tmp_path / "belief_ledger.json"

    first = compact_memory(events=rows, semantic_path=semantic_path, ledger_path=ledger_path)
    second = compact_memory(events=rows, semantic_path=semantic_path, ledger_path=ledger_path)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))

    assert len(ledger["beliefs"]) == 1
    assert len(first["latest"]["promoted_beliefs"]) == 1
    assert len(second["latest"]["promoted_beliefs"]) == 1
    assert len(second["entries"]) == 2
