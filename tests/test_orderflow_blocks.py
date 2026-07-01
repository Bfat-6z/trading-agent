"""HARNESS-A tests: CVD/funding blocks are no-lookahead on an enriched df and
safe (all-False) on a df without order-flow columns."""
import backtest_chart_signal as cs
import orderflow_data as of
import strategy_blocks as sb


def _flow_bars(n):
    out = []
    px = 100.0
    for i in range(n):
        vol = 1000.0 + (i % 10) * 50
        tbb = 300.0 + (i % 9) * 60   # varying buy pressure -> varying cvd
        c = px + (0.4 if (i // 6) % 2 == 0 else -0.4)
        out.append({"open_time": i * 900_000, "close_time": (i + 1) * 900_000,
                    "ts_ms": (i + 1) * 900_000, "open": px, "high": max(px, c) + 0.5,
                    "low": min(px, c) - 0.5, "close": c, "volume": vol,
                    "quote_volume": vol * c, "taker_buy_base": tbb, "taker_buy_quote": tbb * c,
                    "is_final": True, "available_at": str((i + 1) * 900_000),
                    "known_at": str((i + 1) * 900_000), "ingested_at": str((i + 1) * 900_000),
                    "finalized_at": str((i + 1) * 900_000)})
        px = c
    return out


def _enriched(n):
    bars = _flow_bars(n)
    ind = cs.compute_indicators(bars)
    funding = [{"fundingTime": 10 * 900_000, "fundingRate": 0.0005},
               {"fundingTime": 40 * 900_000, "fundingRate": -0.0005}]
    return of.enrich_indicator_df(ind, bars, funding)


CVD_BLOCKS = ["cvd_aggression", "cvd_reversal", "funding_extreme_contrarian", "buy_frac_extreme"]


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
    # funding was joined (some bars carry the non-zero rate). Positional alignment
    # copies flow columns onto the indicator df, so the values are present even
    # though the synthetic int close_time makes compute_indicators' ts_ms unusable.
    assert (df["funding_rate"] != 0).any()
    assert df["cvd_delta"].notna().any()
