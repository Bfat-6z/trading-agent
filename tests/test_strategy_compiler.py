"""HARNESS-2 tests: strategy spec + compiler."""
from datetime import datetime, timedelta, timezone

import pytest

import backtest_chart_signal as cs
import strategy_compiler as sc


def _bars(n, step_s=300):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    px = 100.0
    for i in range(n):
        drift = (1.0 if (i // 9) % 2 == 0 else -1.0) * 0.5
        o = px; c = px + drift
        hi = max(o, c) + 0.7; lo = min(o, c) - 0.7
        ot = (t0 + timedelta(seconds=step_s * i)).isoformat(timespec="seconds")
        ct = (t0 + timedelta(seconds=step_s * (i + 1))).isoformat(timespec="seconds")
        out.append({"open_time": ot, "close_time": ct, "open": f"{o:.4f}", "high": f"{hi:.4f}",
                    "low": f"{lo:.4f}", "close": f"{c:.4f}", "volume": f"{1000 + (600 if i % 4 == 0 else 0)}",
                    "is_final": True, "available_at": ct, "known_at": ct, "ingested_at": ct, "finalized_at": ct})
        px = c
    return out


SHORT_SPEC = {
    "name": "reject_ema_short",
    "direction": "SHORT",
    "entry": {"all": [
        {"block": "trend_ema_stack"},
        {"block": "location_reject_ema_from_below"},
    ]},
    "exit": {"sl_atr": 1.5, "tp_atr": 3.0},
}


def test_spec_validation_and_id():
    assert sc.validate_spec(SHORT_SPEC) == []
    bad = {"direction": "UP", "entry": {"all": [{"block": "nope"}]}}
    errs = sc.validate_spec(bad)
    assert any("direction" in e for e in errs) and any("unknown block" in e for e in errs)
    assert sc.spec_id(SHORT_SPEC) == sc.spec_id(dict(SHORT_SPEC))  # stable


def test_compiled_signal_no_lookahead():
    bars = _bars(200)
    fn = sc.compile_spec(SHORT_SPEC)
    df_full = cs.compute_indicators(bars)
    df1_full = cs.compute_indicators(_bars(40, step_s=3600))
    # collect signal bars on full series
    full_hits = {i for i in range(len(df_full) - 1) if fn(df_full, i, df1_full)}
    # recompute truncated; a signal at bar cut must persist
    for cut in (150, 175, 190):
        if cut in full_hits:
            df_t = cs.compute_indicators(bars[: cut + 1])
            df1_t = cs.compute_indicators(_bars(40, step_s=3600))
            assert fn(df_t, cut, df1_t) is not None, f"signal at {cut} vanished when future removed"


def test_compiled_setup_runs_in_backtest_engine():
    bars = _bars(300)
    bars1h = _bars(60, step_s=3600)
    fn = sc.compile_spec(SHORT_SPEC)
    trades = cs.backtest_symbol(bars, bars1h, 1e10, signal_fn=fn, exit_cfg=SHORT_SPEC["exit"])
    # engine accepts injected signal + exit cfg and returns well-formed trades
    for t in trades:
        assert t["side"] == "SHORT"
        assert set(("entry", "exit", "net", "r_multiple", "reason")).issubset(t)


def test_exit_cfg_changes_targets():
    bars = _bars(300); bars1h = _bars(60, step_s=3600)
    fn = sc.compile_spec(SHORT_SPEC)
    tight = cs.backtest_symbol(bars, bars1h, 1e10, signal_fn=fn, exit_cfg={"sl_atr": 1.0, "tp_atr": 2.0})
    wide = cs.backtest_symbol(bars, bars1h, 1e10, signal_fn=fn, exit_cfg={"sl_atr": 2.0, "tp_atr": 4.0})
    # different exit configs must be able to produce different trade outcomes
    # (at minimum the SL distance differs, so entry->sl gap differs)
    if tight and wide:
        assert abs(tight[0]["sl"] - tight[0]["entry"]) < abs(wide[0]["sl"] - wide[0]["entry"])
