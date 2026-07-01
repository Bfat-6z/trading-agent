"""Backbone tests: global cumulative trial count, holdout peek-once budget, and
the runtime no-lookahead guard (must catch a repainting spec)."""
from datetime import datetime, timedelta, timezone

import backtest_chart_signal as cs
import research_governance as rg
import research_ledger as rl
import strategy_compiler as sc


def _bars(n, step_s=300):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    px = 100.0
    for i in range(n):
        drift = (1.0 if (i // 9) % 2 == 0 else -1.0) * 0.5
        o = px; c = px + drift
        ct = (t0 + timedelta(seconds=step_s * (i + 1))).isoformat(timespec="seconds")
        ot = (t0 + timedelta(seconds=step_s * i)).isoformat(timespec="seconds")
        out.append({"open_time": ot, "close_time": ct, "open": f"{o:.4f}", "high": f"{max(o,c)+0.7:.4f}",
                    "low": f"{min(o,c)-0.7:.4f}", "close": f"{c:.4f}", "volume": "1500",
                    "is_final": True, "available_at": ct, "known_at": ct, "ingested_at": ct, "finalized_at": ct})
        px = c
    return out


SPEC = {"name": "t", "direction": "SHORT",
        "entry": {"all": [{"block": "trend_ema_stack"}, {"block": "location_reject_ema_from_below"}]},
        "exit": {"sl_atr": 1.5, "tp_atr": 3.0}}


def test_global_trial_count_sums_ledger(tmp_path):
    lp = tmp_path / "led.jsonl"
    rl.append_row({"family": "a", "n_trials": 128, "verdict": "KILL"}, ledger_path=lp)
    rl.append_row({"family": "b", "n_trials": 320, "verdict": "KILL"}, ledger_path=lp)
    rl.append_row({"family": "c", "verdict": "KILL"}, ledger_path=lp)  # missing n_trials -> 0
    assert rg.global_trial_count(ledger_path=lp) == 448


def test_holdout_budget_peek_once(tmp_path):
    bp = tmp_path / "budget.jsonl"
    assert rg.can_peek_holdout("spec_x", path=bp) is True
    rg.record_holdout_peek("spec_x", "hold_1", "2026-07-01T00:00:00Z", path=bp)
    assert rg.can_peek_holdout("spec_x", path=bp) is False   # second peek refused
    assert rg.can_peek_holdout("spec_y", path=bp) is True    # other spec still ok


def test_no_lookahead_guard_passes_causal_spec():
    df = cs.compute_indicators(_bars(200))
    df1 = cs.compute_indicators(_bars(40, step_s=3600))
    res = rg.assert_no_lookahead(SPEC, df, df1)
    assert res["clean"] is True and res["checks"] >= 1


def test_no_lookahead_guard_catches_repaint(monkeypatch):
    # simulate a repainting spec: mask's last bar flips True only on the FULL
    # series (depends on future length) -> guard must flag a mismatch.
    df = cs.compute_indicators(_bars(200))
    df1 = cs.compute_indicators(_bars(40, step_s=3600))
    import pandas as pd

    def leaky_mask(spec, d, d1):
        m = pd.Series(False, index=d.index)
        if len(d) >= 200:      # only "fires" when it can see the whole series
            m.iloc[len(d) - 2] = True
        return m

    monkeypatch.setattr(sc, "compute_mask", leaky_mask)
    res = rg.assert_no_lookahead(SPEC, df, df1, cuts=(198,))
    assert res["clean"] is False and res["mismatches"] >= 1


def test_spec_has_order_flow():
    assert rg.spec_has_order_flow({"entry": {"all": [{"block": "cvd_reversal"}]}}) is True
    assert rg.spec_has_order_flow({"entry": {"all": [{"block": "trend_ema_stack"}]}}) is False
