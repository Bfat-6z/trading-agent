import json
import logging
from pathlib import Path

import news_observer as observer

def test_parse_symbols_strips_usdt_and_dedupes():
    assert observer.parse_symbols("btcusdt, ETH BTC") == ["BTC", "ETH"]

def test_parse_markdown_news_extracts_titles_sources_and_links():
    text = """
## Global Market News

### BTC ETF inflows rise (source: CoinDesk)
Summary line
Link: https://example.test/btc
"""

    rows = observer.parse_markdown_news(text, "yfinance", 10)

    assert rows[0]["title"] == "BTC ETF inflows rise"
    assert rows[0]["source"] == "CoinDesk"
    assert rows[0]["url"] == "https://example.test/btc"

def test_write_events_dedupes_existing_ids(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(observer, "safe_append_event", lambda *args, **kwargs: None)
    path = tmp_path / "news_events.jsonl"
    raw = {"title": "SEC sues exchange", "source": "Reuters", "published_at": "2026-06-21T00:00:00+00:00"}

    first = observer.write_events([raw], path)
    second = observer.write_events([raw], path)

    assert len(first) == 1
    assert second == []
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

def test_build_snapshot_uses_fetchers_and_scores(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(observer, "EVENTS_JSONL", tmp_path / "events.jsonl")
    monkeypatch.setattr(observer, "safe_append_event", lambda *args, **kwargs: None)

    def fake_fetch(symbols, max_items):
        return [
            {"title": "SEC sues exchange over BTC token listing", "source": "Reuters", "published_at": "2026-06-21T00:00:00+00:00", "symbols": ["BTC"]}
        ], [{"source": "fake", "status": "ok", "count": 1}]

    monkeypatch.setattr(observer, "fetch_all", fake_fetch)

    snapshot = observer.build_snapshot(["BTC"], 5)

    assert snapshot["event_count"] == 1
    assert snapshot["new_event_count"] == 1
    assert snapshot["crypto_regulatory_risk"] > 0
    assert snapshot["source_health"] == [{"source": "fake", "status": "ok", "count": 1}]

def test_run_once_writes_latest_and_heartbeat(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(observer, "EVENTS_JSONL", tmp_path / "events.jsonl")
    monkeypatch.setattr(observer, "LATEST_JSON", tmp_path / "news_latest.json")
    monkeypatch.setattr(observer, "LATEST_MD", tmp_path / "news_latest.md")
    monkeypatch.setattr(observer, "HEARTBEAT_PATH", tmp_path / "news_heartbeat.json")
    monkeypatch.setattr(observer, "safe_append_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(observer, "safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(observer, "safe_upsert_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        observer,
        "fetch_all",
        lambda symbols, max_items: (
            [{"title": "Coinbase announces SUI listing", "source": "CoinDesk", "published_at": "2026-06-21T00:00:00+00:00", "symbols": ["SUI"]}],
            [{"source": "fake", "status": "ok", "count": 1}],
        ),
    )

    snapshot = observer.run_once(["SUI"], 5)

    assert snapshot["new_event_count"] == 1
    assert json.loads((tmp_path / "news_latest.json").read_text(encoding="utf-8"))["catalyst_score"] > 0
    assert "# News Macro State" in (tmp_path / "news_latest.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "news_heartbeat.json").read_text(encoding="utf-8"))["status"] == "ok"

def test_fetch_reddit_suppresses_dataflow_warning(monkeypatch, caplog):
    observer.ensure_tradingagents_path()
    from tradingagents.dataflows import reddit

    def fake_fetch(*args, **kwargs):
        logging.getLogger(reddit.__name__).warning("Reddit fetch failed for test")
        return "<no Reddit posts found mentioning BTC>"

    monkeypatch.setattr(reddit, "fetch_reddit_posts", fake_fetch)

    with caplog.at_level(logging.WARNING):
        rows, status = observer.fetch_reddit(["BTC"], 5)

    assert rows == []
    assert status["status"] == "empty"
    assert "Reddit fetch failed" not in caplog.text
