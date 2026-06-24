from market_observer import FuturesTicker, hot_score, parse_ticker, render_markdown, ticker_payload


def test_parse_ticker_accepts_usdt_futures_row():
    row = {
        "symbol": "HYPEUSDT",
        "lastPrice": "68.1",
        "priceChangePercent": "2.5",
        "quoteVolume": "123000000",
        "count": 123456,
        "highPrice": "70",
        "lowPrice": "65",
    }

    ticker = parse_ticker(row)

    assert ticker is not None
    assert ticker.symbol == "HYPEUSDT"
    assert ticker.base == "HYPE"
    assert 0 < ticker.range_pos < 1


def test_parse_ticker_rejects_excluded_symbol():
    row = {
        "symbol": "LINKUSDT",
        "lastPrice": "10",
        "priceChangePercent": "1",
        "quoteVolume": "1000000",
        "count": 1,
        "highPrice": "11",
        "lowPrice": "9",
    }

    assert parse_ticker(row) is None


def test_hot_score_prefers_larger_move_and_volume():
    quiet = FuturesTicker("AAAUSDT", 1, 0.5, 1_000_000, 1_000, 1.1, 0.9)
    hot = FuturesTicker("BBBUSDT", 1, 8.0, 200_000_000, 200_000, 1.2, 0.8)

    assert hot_score(hot) > hot_score(quiet)


def test_render_markdown_contains_executor_and_tables():
    ticker = FuturesTicker("BTCUSDT", 100_000, 1.2, 10_000_000_000, 1_000_000, 101_000, 99_000)
    payload = ticker_payload(ticker, {"BTCUSDT": 0.01})
    snapshot = {
        "ts": "2026-06-19T00:00:00+00:00",
        "universe_count": 1,
        "majors": [payload],
        "hot": [payload],
        "top_volume": [payload],
        "top_gainers": [payload],
        "top_losers": [payload],
        "funding_extremes": [payload],
        "executor": {"recent_risk_block": {"reason": "max_consecutive_losses", "count": 2, "ts": "now"}},
    }

    md = render_markdown(snapshot)

    assert "# Market Update" in md
    assert "BTCUSDT" in md
    assert "Risk block" in md
