from pathlib import Path

import pytest

import setup_skill_library as ssl


def sample_snapshot() -> dict:
    return {
        "majors": [
            {"symbol": "BTCUSDT", "change_pct": 1.5, "range_pos": 0.7},
            {"symbol": "ETHUSDT", "change_pct": 1.2, "range_pos": 0.72},
        ],
        "hot": [
            {"symbol": "SOLUSDT", "change_pct": 8.0, "range_pos": 0.62, "funding_pct": 0.02, "quote_volume": 900_000_000},
            {"symbol": "HYPEUSDT", "change_pct": 44.0, "range_pos": 0.95, "funding_pct": 0.24, "quote_volume": 700_000_000},
            {"symbol": "REUSDT", "change_pct": 30.0, "range_pos": 0.82, "funding_pct": -0.31, "quote_volume": 400_000_000},
        ],
        "funding_extremes": [
            {"symbol": "HYPEUSDT", "change_pct": 44.0, "range_pos": 0.95, "funding_pct": 0.24, "quote_volume": 700_000_000},
            {"symbol": "REUSDT", "change_pct": 30.0, "range_pos": 0.82, "funding_pct": -0.31, "quote_volume": 400_000_000},
        ],
    }


def test_default_library_has_named_setup_skills():
    library = ssl.default_library()

    assert len(library["skills"]) == 7
    assert "momentum_continuation" in library["skills"]
    assert "funding_squeeze" in library["skills"]
    assert library["skills"]["momentum_continuation"]["enabled"] is True


def test_match_setup_detects_momentum_continuation():
    matches = ssl.match_setup(
        {"symbol": "SOLUSDT", "side": "LONG"},
        sample_snapshot(),
        context={"tags": ["risk_on"]},
        library=ssl.default_library(),
    )

    setup_ids = [match["setup_id"] for match in matches]
    assert "momentum_continuation" in setup_ids
    assert matches[0]["confidence"] > 0


def test_match_setup_detects_exhaustion_and_funding_squeeze():
    matches = ssl.match_setup(
        {"symbol": "HYPEUSDT", "side": "SHORT"},
        sample_snapshot(),
        context={"tags": ["risk_on"]},
        library=ssl.default_library(),
    )

    setup_ids = {match["setup_id"] for match in matches}
    assert "exhaustion_fade" in setup_ids
    assert "funding_squeeze" in setup_ids


def test_disabled_setup_does_not_match():
    library = ssl.default_library()
    library["skills"]["exhaustion_fade"]["enabled"] = False

    matches = ssl.match_setup(
        {"symbol": "HYPEUSDT", "side": "SHORT"},
        sample_snapshot(),
        context={"tags": ["risk_on"]},
        library=library,
    )

    assert "exhaustion_fade" not in {match["setup_id"] for match in matches}


def test_record_setup_outcome_updates_global_and_regime_stats():
    library = ssl.default_library()

    ssl.record_setup_outcome(library, "momentum_continuation", 0.12, "risk_on", "SOLUSDT", "LONG", evidence_id="paper_close_1")
    skill = ssl.record_setup_outcome(library, "momentum_continuation", -0.04, "risk_on", "SOLUSDT", "LONG", evidence_id="paper_close_2")

    stats = skill["stats"]
    assert stats["trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["net"] == 0.08
    assert stats["win_rate"] == 0.5
    assert stats["expectancy"] == 0.04
    assert stats["by_regime"]["risk_on"]["trades"] == 2

def test_manual_setup_outcome_without_evidence_does_not_change_stats():
    library = ssl.default_library()

    skill = ssl.record_setup_outcome(library, "momentum_continuation", 0.12, "risk_on", "SOLUSDT", "LONG")

    assert skill["stats"]["trades"] == 0
    assert library["history"][-1]["event"] == "setup_outcome_rejected"


def test_record_unknown_setup_raises():
    with pytest.raises(KeyError):
        ssl.record_setup_outcome(ssl.default_library(), "missing_setup", 0.1)


def test_load_save_merges_defaults_with_persisted_stats(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ssl, "safe_append_snapshot", lambda *args, **kwargs: None)
    path = tmp_path / "setup_skills.json"
    library = ssl.default_library()
    ssl.record_setup_outcome(library, "funding_squeeze", 0.2, "mixed", "REUSDT", "LONG", evidence_id="paper_close_1")
    library["skills"]["false_breakout"]["enabled"] = False
    ssl.save_library(library, path=path)

    loaded = ssl.load_library(path)

    assert loaded["skills"]["funding_squeeze"]["stats"]["trades"] == 1
    assert loaded["skills"]["false_breakout"]["enabled"] is False
    assert "momentum_continuation" in loaded["skills"]
    assert path.with_suffix(".md").exists()


def test_invalid_signal_returns_no_matches():
    assert ssl.match_setup({"symbol": "SOLUSDT", "side": "BAD"}, sample_snapshot(), library=ssl.default_library()) == []
