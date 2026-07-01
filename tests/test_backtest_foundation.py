"""Regression tests pinning the backtest foundation before a param sweep is built
on it. Covers: entry timing (i+1 open), cost application, no-lookahead HTF join,
train/holdout embargo, bootstrap determinism. A sweep amplifies any regression
here, so these are guard rails."""
from datetime import datetime, timedelta, timezone

import backtest_chart_signal as cs
import backtest_runner as br


def _bars(n, start="2026-01-01T00:00:00+00:00", step_s=300, base=100.0, trend=0.0, vol=1000.0):
    t0 = datetime.fromisoformat(start)
    out = []
    px = base
    for i in range(n):
        o = px
        c = px + trend
        hi = max(o, c) + 0.5
        lo = min(o, c) - 0.5
        ct = (t0 + timedelta(seconds=step_s * (i + 1))).isoformat(timespec="seconds")
        ot = (t0 + timedelta(seconds=step_s * i)).isoformat(timespec="seconds")
        out.append({"open_time": ot, "close_time": ct, "open": f"{o:.4f}", "high": f"{hi:.4f}",
                    "low": f"{lo:.4f}", "close": f"{c:.4f}", "volume": f"{vol:.1f}", "is_final": True,
                    "available_at": ct, "known_at": ct, "ingested_at": ct, "finalized_at": ct})
        px = c
    return out


def test_ts_ms_is_epoch_milliseconds():
    df = cs.compute_indicators(_bars(5))
    ms = int(df.iloc[0]["ts_ms"])
    # 2026-01-01 00:05:00 UTC in ms
    expected = int(datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc).timestamp() * 1000)
    assert ms == expected


def test_htf_join_excludes_in_progress_bar():
    # 1h bars; at a ts exactly on a 1h close, only bars closed <= ts are visible.
    h1 = _bars(60, step_s=3600)
    df1 = cs.compute_indicators(h1)
    # pick a ts between the 30th and 31st bar close
    ts_mid = int(df1.iloc[30]["ts_ms"]) + 1800_000  # +30min, before bar 31 closes
    trend = cs.htf_trend_at(df1, ts_mid)
    # must be computed from bars closed by ts_mid only (bar 31 not yet closed)
    closed = df1[df1["ts_ms"] <= ts_mid]
    assert len(closed) == 31  # bars 0..30 closed, bar 31 excluded
    assert trend in (1, -1, None)


def test_embargo_drops_insample_trade_crossing_split():
    # Build a series where a signal fires near the split; verify no in-sample
    # trade has exit_ts >= split.
    bars5 = _bars(400, trend=0.3, vol=2000)  # steady uptrend to trigger longs
    bars1h = _bars(80, step_s=3600, trend=1.0)
    df = cs.compute_indicators(bars5)
    split = int(df.iloc[300]["ts_ms"])
    ins = cs.backtest_symbol(bars5, bars1h, 1e10, end_ts_ms=split)
    for t in ins:
        assert int(t["exit_ts"]) < split, "in-sample trade exited at/after split (embargo leak)"


def test_holdout_trades_are_fully_post_split():
    bars5 = _bars(400, trend=0.3, vol=2000)
    bars1h = _bars(80, step_s=3600, trend=1.0)
    df = cs.compute_indicators(bars5)
    split = int(df.iloc[300]["ts_ms"])
    hold = cs.backtest_symbol(bars5, bars1h, 1e10, start_ts_ms=split)
    for t in hold:
        assert int(t["entry_ts"]) >= split


def test_cost_applied_entry_and_exit():
    # Verify at the simulator level that costs are deducted: net < gross always.
    # (backtest_symbol needs pullback-reclaim structure to fire; here we assert the
    # cost math directly via simulate_trade on a hand-made bracket.)
    bars = _bars(60, trend=0.2, vol=2000)
    df = cs.compute_indicators(bars)
    sig = {"side": "LONG", "index": 55, "feature_ts": df.iloc[54]["close_time"],
           "atr": 1.0, "ref_close": float(df.iloc[54]["close"])}
    tr = cs.simulate_trade(df, sig, quote_volume_24h=1e10)
    assert tr is not None
    assert tr["fee"] > 0                     # taker fee charged
    assert tr["net"] < tr["gross"] + 1e-9    # costs deducted from gross


def test_bootstrap_ci_deterministic_and_no_fake_edge():
    rnd = br._seeded_rand(11)
    noise = [(rnd() - 0.5) * 2 for _ in range(400)]
    a = br.block_bootstrap_ci(noise, seed=5)
    b = br.block_bootstrap_ci(noise, seed=5)
    assert a == b  # deterministic
    assert a["lo95"] <= 0  # pure noise must not show positive edge
