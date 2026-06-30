from pathlib import Path

import paper_candidate_feeder as feeder
import whale_flow_observer as wfo

SAMPLE_HTML = """
<div class="tgme_widget_message_text js-message_text" dir="auto">
BTCUSDT long liquidation $2.5M on Binance Futures<br/>Whale alert
</div>
<div class="tgme_widget_message_text js-message_text" dir="auto">
$ETH short position $1.2M opened by whale
</div>
"""

def test_whale_flow_parses_public_telegram_html():
    messages = wfo.parse_telegram_messages("BinanceLiquidations", SAMPLE_HTML)

    assert len(messages) == 2
    assert "BTCUSDT long liquidation" in messages[0]["text"]

def test_default_channels_include_checked_public_telegram_links():
    channels = wfo.configured_channels(env={})

    assert "BinanceLiquidations" in channels
    assert "WhaleBotAlerts" in channels
    assert "kpbtcsignal" in channels
    assert "WhaleSniper" in channels
    assert "whale_alert_io" in channels
    assert "cointrendz_whalehunter" in channels
    assert "cointrendz_pumpdetector" in channels

def test_whale_flow_classifies_and_aggregates_pressure():
    messages = wfo.parse_telegram_messages("BinanceLiquidations", SAMPLE_HTML)
    events = [event for message in messages for event in wfo.classify_message(message, observed_at="now")]
    aggregate = wfo.aggregate_events(events)

    btc = aggregate["by_symbol"]["BTCUSDT"]
    eth = aggregate["by_symbol"]["ETHUSDT"]
    assert btc["long_liquidation_notional"] == 2_500_000
    assert btc["pressure_side"] == "SHORT"
    assert btc["squeeze_risk"] == "kill_longs"
    assert eth["short_flow_notional"] == 1_200_000

def test_whale_flow_run_once_writes_latest(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(wfo, "MEMORY_DIR", tmp_path / "agent_memory")
    monkeypatch.setattr(wfo, "LATEST_PATH", tmp_path / "agent_memory" / "whale_flow_latest.json")
    monkeypatch.setattr(wfo, "HISTORY_PATH", tmp_path / "agent_memory" / "whale_flow_history.jsonl")
    monkeypatch.setattr(wfo, "EVENTS_PATH", tmp_path / "agent_memory" / "whale_flow_events.jsonl")
    monkeypatch.setattr(wfo, "HEARTBEAT_PATH", tmp_path / "whale_flow_observer_heartbeat.json")

    result = wfo.run_once(fetcher=lambda channel: SAMPLE_HTML, channels=["BinanceLiquidations"])

    assert result["status"] == "ok"
    assert result["event_count"] >= 2
    assert result["can_place_live_orders"] is False
    assert wfo.LATEST_PATH.exists()
    assert wfo.HEARTBEAT_PATH.exists()

def test_candidate_feeder_penalizes_whale_flow_conflict():
    row = {"symbol": "ABCUSDT", "price": 10, "high": 11, "low": 6, "change_pct": 25, "range_pos": 0.9, "quote_volume": 100_000_000, "funding_pct": 0.01}
    neutral = feeder.build_candidates({"ts": "now", "hot": [row]})[0]
    conflict = feeder.build_candidates(
        {"ts": "now", "hot": [row]},
        whale_flow={"updated_at": feeder.utc_now(), "by_symbol": {"ABCUSDT": {"symbol": "ABCUSDT", "pressure_side": "LONG", "pressure_score": 0.7, "event_count": 3, "squeeze_risk": "squeeze_shorts"}}},
    )[0]

    assert conflict["side"] == "SHORT"
    assert conflict["score"] < neutral["score"]
    assert "whale_flow_conflict" in conflict["reason"]
    assert conflict["external_flow"]["alignment"] == "conflict"
