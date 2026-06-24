from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import belief_ledger as bl


def test_upsert_belief_creates_stable_id_and_merges_duplicate():
    ledger = bl.default_ledger()

    first = bl.upsert_belief(
        ledger,
        "Crowded funding makes late longs lower quality",
        scope="futures",
        topic="crowding",
        confidence=0.55,
    )
    second = bl.upsert_belief(
        ledger,
        " crowded   funding makes late longs lower quality ",
        scope="FUTURES",
        topic="CROWDING",
        metadata={"source": "test"},
    )

    assert first["belief_id"] == second["belief_id"]
    assert len(ledger["beliefs"]) == 1
    assert second["metadata"]["source"] == "test"


def test_evidence_for_and_against_updates_confidence_and_status():
    ledger = bl.default_ledger()
    belief = bl.upsert_belief(ledger, "Funding squeeze setups improve after forced shorts", confidence=0.6)

    strengthened = bl.add_evidence(
        ledger,
        belief["belief_id"],
        "for",
        2.0,
        "paper_trade",
        "Three paper trades hit TP before SL in same regime.",
    )
    strengthened_confidence = strengthened["confidence"]
    weakened = bl.add_evidence(
        ledger,
        belief["belief_id"],
        "against",
        1.0,
        "paper_trade",
        "Latest trade reversed before confirmation.",
    )

    assert strengthened_confidence > 0.6
    assert weakened["confidence"] < strengthened_confidence
    assert weakened["status"] in {"candidate", "active", "weakened"}
    assert len(weakened["evidence_for"]) == 1
    assert len(weakened["evidence_against"]) == 1


def test_invalid_evidence_inputs_are_rejected():
    ledger = bl.default_ledger()
    belief = bl.upsert_belief(ledger, "A test belief")

    with pytest.raises(ValueError):
        bl.add_evidence(ledger, belief["belief_id"], "maybe", 1, "test", "bad side")
    with pytest.raises(KeyError):
        bl.add_evidence(ledger, "missing", "for", 1, "test", "missing belief")
    with pytest.raises(ValueError):
        bl.upsert_belief(ledger, "   ")


def test_decay_stale_beliefs_reduces_old_confidence_only():
    ledger = bl.default_ledger()
    old_ts = "2026-06-01T00:00:00+00:00"
    fresh_ts = "2026-06-20T00:00:00+00:00"
    old = bl.upsert_belief(ledger, "Old high confidence belief", confidence=0.9, ts=old_ts)
    fresh = bl.upsert_belief(ledger, "Fresh high confidence belief", confidence=0.9, ts=fresh_ts)
    now = datetime(2026, 6, 20, 12, tzinfo=timezone.utc)

    bl.decay_stale_beliefs(ledger, max_age_hours=72, decay=0.05, now=now)

    assert ledger["beliefs"][old["belief_id"]]["confidence"] == 0.85
    assert ledger["beliefs"][fresh["belief_id"]]["confidence"] == 0.9


def test_load_save_and_malformed_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bl, "safe_append_snapshot", lambda *args, **kwargs: None)
    path = tmp_path / "belief_ledger.json"
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("not json", encoding="utf-8")

    malformed = bl.load_ledger(bad_path)
    assert malformed["beliefs"] == {}

    ledger = bl.default_ledger()
    belief = bl.upsert_belief(ledger, "Persist this belief", confidence=0.7)
    bl.save_ledger(ledger, path=path)
    loaded = bl.load_ledger(path)

    assert belief["belief_id"] in loaded["beliefs"]
    assert path.exists()
    assert path.with_suffix(".md").exists()
    assert "Belief Ledger" in path.with_suffix(".md").read_text(encoding="utf-8")


def test_compact_ledger_counts_statuses():
    ledger = bl.default_ledger()
    active = bl.upsert_belief(ledger, "Active belief", confidence=0.7)
    bl.add_evidence(ledger, active["belief_id"], "for", 1, "test", "enough evidence")
    bl.upsert_belief(ledger, "Candidate belief", confidence=0.5)

    compact = bl.compact_ledger(ledger)

    assert compact["belief_count"] == 2
    assert compact["by_status"]["active"] == 1
    assert compact["by_status"]["candidate"] == 1
