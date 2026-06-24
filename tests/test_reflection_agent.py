from argparse import Namespace
from pathlib import Path

import reflection_agent as ra


def test_summarize_trades_counts_losses_and_risk_block():
    events = [
        {"event": "signal", "signal": {"symbol": "HYPEUSDT", "side": "SHORT"}},
        {"event": "paper_open", "position": {"symbol": "HYPEUSDT", "qty": "1"}},
        {"event": "paper_close", "net": "-0.12"},
        {"event": "paper_close", "net": "0.05"},
        {"event": "risk_block", "reason": "max_consecutive_losses", "count": 2},
    ]

    stats = ra.summarize_trades(events)

    assert stats.paper_opens == 1
    assert stats.paper_closes == 2
    assert stats.losses == 1
    assert stats.wins == 1
    assert stats.net == -0.06999999999999999
    assert stats.last_risk_block["reason"] == "max_consecutive_losses"
    assert stats.signal_counts["HYPEUSDT:SHORT"] == 1


def test_derive_lessons_detects_two_loss_and_weak_majors():
    stats = ra.TradeStats(
        paper_opens=2,
        paper_closes=2,
        wins=0,
        losses=2,
        net=-0.2,
        last_risk_block={"reason": "max_consecutive_losses"},
        signal_counts={},
        symbols_seen={},
    )
    market = {"major_24h_pct": {"BTCUSDT": -0.5, "ETHUSDT": -2.5}, "hot_symbols": ["REUSDT"]}

    lessons = ra.derive_lessons(stats, market)

    assert any("Two-loss sequence" in lesson for lesson in lessons)
    assert any("Majors are broadly red" in lesson for lesson in lessons)


def test_run_once_writes_memory_files(tmp_path: Path, monkeypatch):
    memory_dir = tmp_path / "memory"
    scalp_log = tmp_path / "scalp.jsonl"
    market_latest = tmp_path / "market.json"
    scalp_log.write_text(
        '\n'.join([
            '{"event":"paper_close","net":"-0.1"}',
            '{"event":"paper_close","net":"-0.2"}',
            '{"event":"risk_block","reason":"max_consecutive_losses","count":2}',
        ])
        + "\n",
        encoding="utf-8",
    )
    market_latest.write_text(
        '{"ts":"now","universe_count":2,"majors":[{"symbol":"ETHUSDT","change_pct":-3}],"hot":[{"symbol":"REUSDT"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(ra, "PROFILE_PATH", memory_dir / "profile.json")
    monkeypatch.setattr(ra, "BIAS_PATH", memory_dir / "execution_bias.json")
    monkeypatch.setattr(ra, "REFLECTION_LATEST_MD", memory_dir / "daily_reflection_latest.md")
    monkeypatch.setattr(ra, "DREAM_JOURNAL_MD", memory_dir / "dream_journal.md")
    monkeypatch.setattr(ra, "LESSONS_JSONL", memory_dir / "lessons.jsonl")
    monkeypatch.setattr(ra, "SCALP_LOG", scalp_log)
    monkeypatch.setattr(ra, "MARKET_LATEST", market_latest)

    result = ra.run_once(Namespace(trade_events=100, dreams=2))

    assert result["bias"]["risk_posture"] == "defensive"
    assert result["bias"]["min_signal_score"] == 7
    assert result["bias"]["market_learning"]["regime"] is not None
    assert (memory_dir / "market_model.json").exists()
    assert (memory_dir / "market_learning_latest.md").exists()
    assert (memory_dir / "profile.json").exists()
    assert "Daily Reflection" in (memory_dir / "daily_reflection_latest.md").read_text(encoding="utf-8")
