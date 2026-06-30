from datetime import datetime, timedelta, timezone

import pytest

import chart_candle_service as ccs
import chart_indicator_engine as cie
import chart_snapshot_renderer as csr


def ms(dt):
    return int(dt.timestamp() * 1000)


def kline(open_dt, price, volume=100):
    return [
        ms(open_dt),
        str(price),
        str(price + 1),
        str(price - 1),
        str(price),
        str(volume),
        ms(open_dt) + 59_999,
        str(price * volume),
        10,
    ]


def batch():
    base = datetime(2026, 6, 21, tzinfo=timezone.utc)
    rows = [kline(base + timedelta(minutes=idx), 100 + idx, 100 + idx * 10) for idx in range(40)]
    server_time = base + timedelta(minutes=len(rows))
    cutoff = server_time + timedelta(seconds=5)
    return ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        rows,
        server_time=server_time.isoformat(timespec="seconds"),
        ingested_at=cutoff.isoformat(timespec="seconds"),
        decision_cutoff=cutoff.isoformat(timespec="seconds"),
    )


def score():
    return {"score_id": "score_1", "score": 8.8, "tier": "A+", "source_ids": ["chart"], "input_event_ids": ["e1"], "decision_cutoff": "2026-06-21T00:40:05+00:00", "cutoff_proof": {"ok": True}}


def risk():
    return {"risk_plan_id": "risk_1", "sl": 98, "tp_ladder": [{"price": 110, "rr": 1.5}]}


def zones():
    return {"structures": {"zones": [{"zone_id": "zone_1", "zone_type": "support", "lower": 99, "upper": 101}]}}


def test_snapshot_exists_and_non_empty_png(tmp_path, monkeypatch):
    monkeypatch.setattr(csr, "SNAPSHOT_DIR", tmp_path)
    candle_batch = batch()

    metadata = csr.render_snapshot(candle_batch, indicator_bundle=cie.compute_indicator_bundle(candle_batch), score=score(), risk_plan=risk(), zone_bundle=zones())
    image = tmp_path / f"{metadata['snapshot_id']}.png"

    assert image.exists()
    assert image.stat().st_size > 1000
    assert (tmp_path / f"{metadata['snapshot_id']}.json").exists()


def test_metadata_hash_matches_same_input(tmp_path, monkeypatch):
    monkeypatch.setattr(csr, "SNAPSHOT_DIR", tmp_path)
    candle_batch = batch()
    indicators = cie.compute_indicator_bundle(candle_batch)

    first = csr.render_snapshot(candle_batch, indicator_bundle=indicators, score=score(), risk_plan=risk(), zone_bundle=zones())
    second = csr.render_snapshot(candle_batch, indicator_bundle=indicators, score=score(), risk_plan=risk(), zone_bundle=zones())

    assert first["data_hash"] == second["data_hash"]
    assert first["snapshot_id"] == second["snapshot_id"]


def test_overlay_ids_match_score_and_risk_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(csr, "SNAPSHOT_DIR", tmp_path)

    metadata = csr.render_snapshot(batch(), score=score(), risk_plan=risk(), zone_bundle=zones())

    assert "score_1" in metadata["point_ids"]
    assert "risk_1" in metadata["point_ids"]
    assert "zone_1" in metadata["point_ids"]


def test_missing_data_creates_warning_overlay(tmp_path, monkeypatch):
    monkeypatch.setattr(csr, "SNAPSHOT_DIR", tmp_path)
    candle_batch = batch()
    candle_batch["bars"] = candle_batch["bars"][:1]

    metadata = csr.render_snapshot(candle_batch)

    assert "missing_or_insufficient_candles" in metadata["warnings"]
    assert metadata["degradation_state"] in {"partial", "quarantined"}


def test_renderer_uses_agg_without_display_server():
    assert csr.matplotlib.get_backend().lower() == "agg"


def test_path_traversal_attempt_cannot_serve_outside(tmp_path, monkeypatch):
    monkeypatch.setattr(csr, "SNAPSHOT_DIR", tmp_path)

    with pytest.raises(ValueError, match="path_traversal_blocked"):
        csr.safe_artifact_path("../evil.png")


def test_retention_pruning_preserves_metadata_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(csr, "SNAPSHOT_DIR", tmp_path)
    first = csr.render_snapshot(batch(), score=score())
    second_score = score()
    second_score["score_id"] = "score_2"
    second = csr.render_snapshot(batch(), score=second_score)

    result = csr.prune_snapshot_images(max_png_files=0, directory=tmp_path)

    assert first["metadata_path"].endswith(".json")
    assert second["metadata_path"].endswith(".json")
    assert len(result["deleted"]) >= 1
    assert (tmp_path / f"{first['snapshot_id']}.json").exists()
    assert (tmp_path / f"{second['snapshot_id']}.json").exists()
    assert not list(tmp_path.glob("*.png"))
