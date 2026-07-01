"""HARNESS-4 tests: the overfit gate must reject noise and get stricter with N.

These are the guard rails that make the whole harness trustworthy: if pure noise
could pass, or if sweeping more configs didn't raise the bar, the harness would
manufacture fake edge.
"""
import math
import random

import overfit_gate as og


def test_norm_ppf_cdf_roundtrip():
    for p in (0.025, 0.1, 0.5, 0.9, 0.975, 0.99):
        x = og.norm_ppf(p)
        assert abs(og.norm_cdf(x) - p) < 1e-3


def test_expected_max_sharpe_increases_with_trials():
    v = 0.04
    a = og.expected_max_sharpe(10, v)
    b = og.expected_max_sharpe(1000, v)
    assert b > a > 0, "E[max SR] must grow with the number of trials"


def test_dsr_rejects_pure_noise():
    rng = random.Random(7)
    noise = [rng.gauss(0, 1) for _ in range(1000)]  # zero-mean noise, no edge
    res = og.deflated_sharpe_ratio(noise, n_trials=500, var_trial_sharpe=0.03)
    assert res["dsr"] < 0.95, f"noise should not be DSR-significant, got {res['dsr']}"


def test_dsr_gets_harder_with_more_trials():
    rng = random.Random(3)
    # a mild positive-drift series
    rs = [rng.gauss(0.05, 1.0) for _ in range(1000)]
    few = og.deflated_sharpe_ratio(rs, n_trials=5, var_trial_sharpe=0.03)
    many = og.deflated_sharpe_ratio(rs, n_trials=5000, var_trial_sharpe=0.03)
    assert many["sr0"] > few["sr0"]        # benchmark rises
    assert many["dsr"] <= few["dsr"]       # same series is less significant under more trials


def test_purge_overlaps_removes_codependent_trades():
    trades = [
        {"symbol": "A", "entry_ts": 0, "exit_ts": 100, "r_multiple": 1},
        {"symbol": "A", "entry_ts": 50, "exit_ts": 150, "r_multiple": 1},   # overlaps -> purged
        {"symbol": "A", "entry_ts": 200, "exit_ts": 300, "r_multiple": 1},
    ]
    kept = og.purge_overlaps(trades)
    assert len(kept) == 2
    assert [t["entry_ts"] for t in kept] == [0, 200]


def test_cross_consistency_counts_symbols_and_subperiods():
    trades = []
    for sym in ("A", "B", "C"):
        for k in range(4):
            trades.append({"symbol": sym, "entry_ts": k * 1000, "exit_ts": k * 1000 + 10,
                           "r_multiple": 1.0})  # all positive
    cc = og.cross_consistency(trades)
    assert cc["positive_symbols"] == 3
    assert cc["positive_subperiods"] >= 3


def test_pick_best_prefers_meaningful_sample_over_fluke():
    fluke = {"spec_id": "fluke", "in_sample": {"expectancy_r": 2.0, "profit_factor": 9.0, "trades": 3}}
    solid = {"spec_id": "solid", "in_sample": {"expectancy_r": 0.15, "profit_factor": 1.3, "trades": 500}}
    best = og.pick_best([fluke, solid])
    assert best["spec_id"] == "solid", "must not crown a 3-trade fluke over a 500-trade candidate"


def test_pick_best_falls_back_to_most_sampled_when_none_qualify():
    a = {"spec_id": "a", "in_sample": {"expectancy_r": 1.0, "trades": 10}}
    b = {"spec_id": "b", "in_sample": {"expectancy_r": -0.1, "trades": 120}}
    best = og.pick_best([a, b])
    assert best["spec_id"] == "b", "with no spec >=300 trades, report the best-sampled attempt"


def test_gate_kills_when_any_check_fails():
    # a candidate with great in-sample numbers but only 1 positive symbol
    best = {
        "spec_id": "x", "in_sample": {"expectancy_r": 0.5, "profit_factor": 2.0, "trades": 500},
        "trades": [{"symbol": "A", "entry_ts": i * 100, "exit_ts": i * 100 + 10, "r_multiple": 0.5}
                   for i in range(500)],  # all one symbol -> fails cross-consistency
    }
    sweep = [best, {"spec_id": "y", "in_sample": {"expectancy_r": -0.1, "profit_factor": 0.8}, "trades": []}]
    verdict = og.evaluate_candidate(best, sweep, n_trials=len(sweep))
    assert verdict["pre_holdout_pass"] is False
    assert verdict["checks"]["enough_symbols"] is False
