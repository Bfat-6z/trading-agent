from curiosity_scheduler import choose_focus
from setup_skill_library import default_library


def test_chooses_confusing_loss_over_random_hot_symbol():
    events = [
        {"event": "signal", "symbol": "HOTUSDT", "side": "LONG", "score": 9},
        {"event": "paper_close", "symbol": "SOLUSDT", "side": "LONG", "net": "-0.42", "ts": "2026-06-20T00:00:00+00:00"},
    ]

    focus = choose_focus(events=events, library=default_library(), ledger={"beliefs": {}}, market_model={"regime_counts": {"risk_on": 10}})

    assert focus["focus_type"] == "confusing_loss"
    assert focus["symbol"] == "SOLUSDT"
    assert focus["side"] == "LONG"
    assert "recent_paper_loss" in focus["reasons"]


def test_chooses_under_sampled_setup_when_no_losses():
    library = default_library()
    for skill in library["skills"].values():
        skill["stats"]["trades"] = 10
        skill["stats"]["wins"] = 7
        skill["stats"]["win_rate"] = 0.7
        skill["stats"]["expectancy"] = 0.01
        skill["stats"]["net"] = 1.0
    library["skills"]["range_breakout"]["stats"].update({"trades": 0, "wins": 0, "win_rate": 0.0, "expectancy": 0.0, "net": 0.0})

    focus = choose_focus(events=[], library=library, ledger={"beliefs": {}}, market_model={"regime_counts": {"risk_on": 10}})

    assert focus["focus_type"] == "under_sampled_setup"
    assert focus["setup_id"] == "range_breakout"
    assert "setup_has_too_few_samples" in focus["reasons"]


def test_contradictory_belief_beats_under_sampled_regime():
    ledger = {
        "beliefs": {
            "belief-test": {
                "belief_id": "belief-test",
                "confidence": 0.35,
                "evidence_for": [],
                "evidence_against": [{"summary": "failed"}],
            }
        }
    }

    focus = choose_focus(events=[], library={"skills": {}}, ledger=ledger, market_model={"regime_counts": {"mixed": 1}})

    assert focus["focus_type"] == "contradictory_belief"
    assert focus["belief_id"] == "belief-test"
