from datetime import datetime, timedelta, timezone

from news_signal_model import extract_symbols, freshness_score, normalize_event, parse_ts, score_events, source_quality, stable_event_id

def test_stable_event_id_dedupes_same_headline_source_and_url():
    first = stable_event_id("SEC sues exchange over crypto tokens", "Reuters", "https://example.test/a", "2026-06-21T00:00:00+00:00")
    second = stable_event_id(" SEC  sues exchange over crypto tokens ", "reuters", "https://example.test/a", "2026-06-21T00:00:00+00:00")

    assert first == second

def test_extract_symbols_finds_dollar_and_plain_mentions():
    symbols = extract_symbols("BTC and $ETH rally while SUI pauses", [])

    assert symbols == ["BTC", "ETH", "SUI"]

def test_source_quality_weights_official_and_social_sources():
    assert source_quality("sec.gov", "news") > source_quality("reddit", "social")

def test_normalize_event_classifies_topics_and_symbols():
    event = normalize_event(
        {
            "title": "SEC sues exchange while BTC ETF outflow rises",
            "source": "Reuters",
            "published_at": "2026-06-21T00:00:00+00:00",
        },
        now="2026-06-21T00:05:00+00:00",
    )

    assert "regulation" in event["topics"]
    assert "liquidity" in event["topics"]
    assert "BTC" in event["symbols"]

def test_high_risk_regulatory_headline_scores_risk():
    now = datetime.now(timezone.utc)
    result = score_events(
        [
            {
                "title": "SEC sues Binance as crypto crackdown expands",
                "source": "Reuters",
                "published_at": now.isoformat(timespec="seconds"),
                "symbols": ["BNB"],
            }
        ],
        now=now,
    )

    assert result["crypto_regulatory_risk"] > 0.2
    assert result["headline_chaos"] >= 0
    assert result["symbol_impacts"]["BNB"]["risk"] > 0
    assert result["can_place_orders"] is False
    assert result["can_loosen_risk"] is False

def test_listing_catalyst_scores_catalyst_but_not_macro_risk():
    now = datetime.now(timezone.utc)
    result = score_events(
        [
            {
                "title": "Coinbase announces new SUI perpetual futures listing",
                "source": "CoinDesk",
                "published_at": now.isoformat(timespec="seconds"),
                "symbols": ["SUI"],
            }
        ],
        now=now,
    )

    assert result["catalyst_score"] > 0
    assert result["macro_risk_score"] == 0
    assert result["symbol_impacts"]["SUI"]["bullish"] > 0

def test_stale_headline_has_lower_freshness_than_recent_headline():
    now = datetime.now(timezone.utc)
    recent = score_events([
        {"title": "BTC ETF inflow rises", "source": "CoinDesk", "published_at": now.isoformat(timespec="seconds")}
    ], now=now)
    stale = score_events([
        {"title": "BTC ETF inflow rises", "source": "CoinDesk", "published_at": (now - timedelta(days=5)).isoformat(timespec="seconds")}
    ], now=now)

    assert recent["freshness_score"] > stale["freshness_score"]

def test_rfc822_pubdate_parses_for_rss_freshness():
    now = datetime(2026, 6, 21, tzinfo=timezone.utc)
    parsed = parse_ts("Fri, 19 Jun 2026 17:51:12 +0000")

    assert parsed is not None
    assert parsed.tzinfo is not None
    assert freshness_score("Fri, 19 Jun 2026 17:51:12 +0000", now=now) < 0.6
