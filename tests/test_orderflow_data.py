"""HARNESS-A tests: CVD + funding data path is no-lookahead."""
import orderflow_data as of


def _flow_bars(n):
    out = []
    for i in range(n):
        vol = 1000.0 + i
        tbb = 400.0 + (i % 7) * 30  # varying buy pressure
        out.append({"open_time": i * 3600_000, "close_time": (i + 1) * 3600_000,
                    "ts_ms": (i + 1) * 3600_000, "open": 100.0, "high": 101.0, "low": 99.0,
                    "close": 100.0 + (i % 5) * 0.1, "volume": vol, "quote_volume": vol * 100,
                    "taker_buy_base": tbb, "taker_buy_quote": tbb * 100, "is_final": True})
    return out


def test_cvd_delta_sign_and_formula():
    bars = _flow_bars(30)
    df = of.compute_cvd_columns(bars)
    # cvd_delta = 2*taker_buy - volume
    row = bars[10]
    expected = 2 * row["taker_buy_base"] - row["volume"]
    assert abs(df.iloc[10]["cvd_delta"] - expected) < 1e-9
    # buy_frac in [0,1]
    assert (df["buy_frac"] >= 0).all() and (df["buy_frac"] <= 1).all()


def test_cvd_columns_no_lookahead():
    bars = _flow_bars(60)
    df_full = of.compute_cvd_columns(bars)
    for cut in (40, 50, 55):
        df_trunc = of.compute_cvd_columns(bars[: cut + 1])
        for col in ("cvd_delta", "buy_frac", "cvd_roll20", "cvd_delta_norm"):
            a = df_full.iloc[cut][col]; b = df_trunc.iloc[cut][col]
            if a != a:  # nan
                assert b != b
            else:
                assert abs(a - b) < 1e-9, f"{col} at {cut} changed when future removed"


def test_funding_join_is_point_in_time():
    bars = _flow_bars(20)
    df = of.compute_cvd_columns(bars)
    # funding events at t=5h and t=13h
    funding = [{"fundingTime": 5 * 3600_000, "fundingRate": 0.001},
               {"fundingTime": 13 * 3600_000, "fundingRate": -0.002}]
    joined = of.join_funding_point_in_time(df, funding)
    # bar closing at 4h (ts_ms=4h... actually ts_ms=(i+1)*3600) -> before first funding => 0
    # find bar with ts_ms just after 5h and before 13h -> rate 0.001
    # a bar closing exactly at a funding time already knows that funding, so use
    # strict boundaries: (5h,13h) -> 0.001 ; >=13h -> -0.002 ; <5h -> 0.0
    r_mid = joined[(joined["ts_ms"] >= 5 * 3600_000) & (joined["ts_ms"] < 13 * 3600_000)]["funding_rate"]
    assert (r_mid == 0.001).all()
    r_late = joined[joined["ts_ms"] >= 13 * 3600_000]["funding_rate"]
    assert (r_late == -0.002).all()
    r_early = joined[joined["ts_ms"] < 5 * 3600_000]["funding_rate"]
    assert (r_early == 0.0).all()
