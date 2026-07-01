"""HARNESS-A tests: CVD/funding blocks are no-lookahead on an enriched df and
safe (all-False) on a df without order-flow columns."""
import backtest_chart_signal as cs
import orderflow_data as of
import strategy_blocks as sb

_BASE_MS = 1_700_000_000_000  # a real-ish epoch base so ISO round-trips cleanly


def _flow_bars(n):
    # Mirror production fetch_klines_with_flow: ISO close_time (ms precision) so
    # compute_indicators derives ts_ms identical to the flow ts_ms field -> the
    # fail-closed enrich join matches every bar.
    out = []
    px = 100.0
    for i in range(n):
        vol = 1000.0 + (i % 10) * 50
        tbb = 300.0 + (i % 9) * 60   # varying buy pressure -> varying cvd
        c = px + (0.4 if (i // 6) % 2 == 0 else -0.4)
        ot = _BASE_MS + i * 900_000
        ct = _BASE_MS + (i + 1) * 900_000
        out.append({"open_time": of._iso_ms(ot), "close_time": of._iso_ms(ct),
                    "ts_ms": ct, "open": px, "high": max(px, c) + 0.5,
                    "low": min(px, c) - 0.5, "close": c, "volume": vol,
                    "quote_volume": vol * c, "taker_buy_base": tbb, "taker_buy_quote": tbb * c,
                    "is_final": True, "available_at": of._iso_ms(ct),
                    "known_at": of._iso_ms(ct), "ingested_at": of._iso_ms(ct),
                    "finalized_at": of._iso_ms(ct)})
        px = c
    return out


def _enriched(n):
    bars = _flow_bars(n)
    ind = cs.compute_indicators(bars)
    funding = [{"fundingTime": _BASE_MS + 10 * 900_000, "fundingRate": 0.0005},
               {"fundingTime": _BASE_MS + 40 * 900_000, "fundingRate": -0.0005}]
    return of.enrich_indicator_df(ind, bars, funding)


CVD_BLOCKS = ["cvd_aggression", "cvd_reversal", "funding_extreme_contrarian", "buy_frac_extreme",
              "funding_zscore_fade"]


def test_cvd_blocks_safe_on_unenriched_df():
    df = cs.compute_indicators(_flow_bars(60))  # no cvd/funding columns merged
    for name in CVD_BLOCKS:
        s = sb.evaluate_block(name, df, "LONG")
        assert not s.any(), f"{name} must be all-False when columns absent"


def test_cvd_blocks_no_lookahead_on_enriched_df():
    df_full = _enriched(120)
    for name in CVD_BLOCKS:
        for direction in ("LONG", "SHORT"):
            full = sb.evaluate_block(name, df_full, direction)
            for cut in (80, 100, 115):
                # rebuild enriched df truncated to 0..cut
                bars = _flow_bars(cut + 1)
                ind = cs.compute_indicators(bars)
                funding = [{"fundingTime": 10 * 900_000, "fundingRate": 0.0005},
                           {"fundingTime": 40 * 900_000, "fundingRate": -0.0005}]
                df_t = of.enrich_indicator_df(ind, bars, funding)
                trunc = sb.evaluate_block(name, df_t, direction)
                assert bool(trunc.iloc[cut]) == bool(full.iloc[cut]), (
                    f"{name}/{direction} at {cut} changed when future removed -> leak")


def test_cvd_columns_present_after_enrich():
    df = _enriched(60)
    for col in ("cvd_delta", "buy_frac", "cvd_delta_norm", "funding_rate"):
        assert col in df.columns
    # funding joined via the fail-closed ts_ms match; some bars carry the rate.
    assert (df["funding_rate"] != 0).any()
    assert df["cvd_delta"].notna().any()


def test_enrich_fails_closed_on_ts_mismatch():
    # a flow set whose ts_ms cannot match the indicator df must RAISE, never
    # silently NaN (that was the latent-lookahead vector).
    import pytest
    bars = _flow_bars(30)
    ind = cs.compute_indicators(bars)
    bad_flow = _flow_bars(30)
    for b in bad_flow:
        b["ts_ms"] = int(b["ts_ms"]) + 7   # shift so no ts_ms matches
    with pytest.raises(ValueError):
        of.enrich_indicator_df(ind, bad_flow, [])
