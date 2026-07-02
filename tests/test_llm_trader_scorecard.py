"""Tests for llm_trader_scorecard — determinism, hand-computed metrics, verdict ladder.

Covers plan acceptance criterion #5 (scorecard deterministic; all-positive rs
-> p < 0.01; n=5 -> INSUFFICIENT_DATA) plus hand-checked metric math on a
small known array and CI-contains-sample-mean sanity.
"""
from __future__ import annotations

import math

import llm_trader_scorecard as sc


def _trade(net: float, r: float, reason: str = "tp") -> dict:
    """Minimal closed-trade record matching llm_trader.resolve output keys."""
    return {
        "symbol": "TESTUSDT", "side": "LONG", "regime": "trend", "hour_utc": 3,
        "entry": 1.0, "exit": 1.0, "reason": reason, "net": net, "r": r,
        "leverage": 5, "rationale": "test", "closed_ts": 1_700_000_000_000,
    }


# hand-computed fixture: nets [10, -5, 20, -10, 5], rs [1.0, -0.5, 2.0, -1.0, 0.5]
KNOWN = [
    _trade(10.0, 1.0),
    _trade(-5.0, -0.5, reason="sl"),
    _trade(20.0, 2.0),
    _trade(-10.0, -1.0, reason="liquidation"),
    _trade(5.0, 0.5),
]


# ---------------------------------------------------------------------------
# basic_metrics — hand-computed values
# ---------------------------------------------------------------------------
def test_basic_metrics_hand_computed():
    m = sc.basic_metrics(KNOWN)
    assert m["n"] == 5
    assert m["wins"] == 3
    assert m["win_rate"] == 0.6
    assert m["mean_r"] == 0.4                      # (1 -0.5 +2 -1 +0.5)/5
    assert m["expectancy_usd"] == 4.0              # (10-5+20-10+5)/5
    assert m["profit_factor"] == round(35.0 / 15.0, 4)
    # population std of rs: sqrt(5.70/5) = sqrt(1.14)
    assert m["sharpe_trade"] == round(0.4 / math.sqrt(1.14), 4)
    # cumulative net: 10, 5, 25, 15, 20 -> worst peak-to-trough = 25 - 15
    assert m["max_dd_usd"] == 10.0
    assert m["max_win_streak"] == 1
    assert m["max_loss_streak"] == 1
    assert m["liq_count"] == 1


def test_basic_metrics_streaks():
    trades = [_trade(n, n) for n in [1.0, 2.0, -1.0, -2.0, -3.0, 4.0]]
    m = sc.basic_metrics(trades)
    assert m["max_win_streak"] == 2
    assert m["max_loss_streak"] == 3


def test_profit_factor_inf_safe():
    all_wins = [_trade(5.0, 0.5), _trade(3.0, 0.3)]
    assert sc.basic_metrics(all_wins)["profit_factor"] == float("inf")
    all_losses = [_trade(-5.0, -0.5), _trade(-3.0, -0.3)]
    assert sc.basic_metrics(all_losses)["profit_factor"] == 0.0
    assert sc.basic_metrics([])["profit_factor"] == 0.0


def test_empty_and_degenerate_inputs():
    m = sc.basic_metrics([])
    assert m["n"] == 0 and m["win_rate"] == 0.0 and m["max_dd_usd"] == 0.0
    # n < 2 -> sharpe 0
    assert sc.basic_metrics([_trade(5.0, 0.5)])["sharpe_trade"] == 0.0
    # constant rs -> std 0 -> sharpe 0
    const = [_trade(1.0, 0.5) for _ in range(4)]
    assert sc.basic_metrics(const)["sharpe_trade"] == 0.0


def test_malformed_rows_do_not_crash_and_do_not_count():
    """Fail-closed: junk rows are DROPPED (n excludes them), not coerced to 0.0
    trades. The old behavior (n counted junk) let garbage satisfy min_trades."""
    bad = [{"net": "oops", "r": None, "reason": 42}, _trade(3.0, 0.3)]
    m = sc.basic_metrics(bad)
    assert m["n"] == 1 and m["wins"] == 1
    assert m["n_dropped"] == 1


def test_regression_malformed_rows_cannot_satisfy_min_trades_gate():
    """Regression (fail-open n gate): 25 real wins + 5 junk rows used to reach
    n=30 and mint PROMISING. Junk must not count toward the 30-trade minimum."""
    real = [_trade(5.0, 0.5) for _ in range(25)]
    junk = [{"net": "oops", "r": None, "reason": 42} for _ in range(5)]
    card = sc.scorecard(real + junk)
    assert card["metrics"]["n"] == 25          # junk excluded from n
    assert card["metrics"]["n_dropped"] == 5   # ...and audited
    assert card["verdict"]["code"] == "INSUFFICIENT_DATA"


def test_regression_nan_rows_cannot_disarm_negative_verdict():
    """Regression (NaN poisoning): one NaN r in a losing 30-trade book used to
    flip the permutation p-value to its minimum (0.0002) and disarm the
    NEGATIVE gate (nan < 0 is False), grading a loser INCONCLUSIVE."""
    losing = ([_trade(-5.0, -0.5, reason="sl") for _ in range(20)]
              + [_trade(5.0, 0.5) for _ in range(10)])  # mean r = -1/6 < 0
    nan_row = _trade(float("nan"), float("nan"))
    card = sc.scorecard(losing + [nan_row])
    assert card["metrics"]["n"] == 30           # NaN row dropped, real n kept
    assert card["metrics"]["n_dropped"] == 1
    assert card["metrics"]["mean_r"] < 0
    assert card["verdict"]["code"] == "NEGATIVE"
    assert card["pvalue"] > 0.5                 # losing mean is easy for luck to beat


def test_f_rejects_non_finite():
    """_f contract: non-finite floats (NaN/inf, incl. the 'nan' string, which
    float() happily parses) coerce to default instead of passing through."""
    assert sc._f(float("nan")) == 0.0
    assert sc._f(float("inf")) == 0.0
    assert sc._f(float("-inf")) == 0.0
    assert sc._f("nan") == 0.0
    assert sc._f("inf", default=0.5) == 0.5


def test_stats_fail_closed_on_non_finite_input():
    """Direct API: any NaN/inf element makes bootstrap_ci return (0,0) and
    permutation_pvalue return 1.0 — never a fake-significant number."""
    for poison in (float("nan"), float("inf"), float("-inf")):
        rs = [0.5] * 29 + [poison]
        assert sc.permutation_pvalue(rs) == 1.0
        assert sc.bootstrap_ci(rs) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# bootstrap_ci
# ---------------------------------------------------------------------------
def test_bootstrap_ci_contains_sample_mean():
    rs = [0.5, -1.0, 2.0, 1.5, -0.3, 0.8, -0.6, 1.1, 0.2, -1.4] * 3  # n=30
    mean = sum(rs) / len(rs)
    lo, hi = sc.bootstrap_ci(rs)
    assert lo <= mean <= hi
    assert lo < hi


def test_bootstrap_ci_edges():
    assert sc.bootstrap_ci([]) == (0.0, 0.0)
    lo, hi = sc.bootstrap_ci([0.7])
    assert lo == hi == 0.7  # only one value to resample


def test_bootstrap_ci_deterministic():
    rs = [0.5, -0.2, 1.1, -0.7, 0.9, 0.3]
    assert sc.bootstrap_ci(rs) == sc.bootstrap_ci(rs)
    assert sc.bootstrap_ci(rs, seed=7) == sc.bootstrap_ci(rs, seed=7)


# ---------------------------------------------------------------------------
# permutation_pvalue
# ---------------------------------------------------------------------------
def test_permutation_all_positive_rs_small_p():
    rs = [0.5] * 20 + [1.0] * 10  # 30 strictly positive R outcomes
    p = sc.permutation_pvalue(rs)
    assert p < 0.01  # acceptance criterion #5


def test_permutation_negative_mean_high_p():
    rs = [-0.5] * 15 + [-1.0] * 15
    assert sc.permutation_pvalue(rs) > 0.5  # luck beats a losing mean easily


def test_permutation_edges_and_determinism():
    assert sc.permutation_pvalue([]) == 1.0
    rs = [0.4, -0.3, 0.9, -0.1, 0.6]
    p1, p2 = sc.permutation_pvalue(rs), sc.permutation_pvalue(rs)
    assert p1 == p2
    assert 0.0 < p1 <= 1.0  # add-one smoothing: never exactly 0


# ---------------------------------------------------------------------------
# verdict ladder (exact order)
# ---------------------------------------------------------------------------
def test_verdict_insufficient_data():
    v = sc.verdict({"n": 5, "mean_r": 0.9}, (0.5, 1.2), 0.001)
    assert v["code"] == "INSUFFICIENT_DATA"  # n gate wins even with great stats


def test_verdict_negative_beats_ci():
    v = sc.verdict({"n": 50, "mean_r": -0.1}, (0.01, 0.3), 0.001)
    assert v["code"] == "NEGATIVE"  # ladder order: negative checked before CI


def test_verdict_promising_and_honest_detail():
    v = sc.verdict({"n": 50, "mean_r": 0.2}, (0.05, 0.4), 0.01)
    assert v["code"] == "PROMISING"
    assert "NOT proven edge" in v["detail"]
    assert "proven" not in v["detail"].replace("NOT proven", "")


def test_verdict_inconclusive_paths():
    # ci_low <= 0
    assert sc.verdict({"n": 50, "mean_r": 0.2}, (-0.05, 0.4), 0.01)["code"] == "INCONCLUSIVE"
    # p >= 0.05
    assert sc.verdict({"n": 50, "mean_r": 0.2}, (0.05, 0.4), 0.2)["code"] == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# scorecard — acceptance criterion #5 end-to-end
# ---------------------------------------------------------------------------
def test_scorecard_deterministic_same_input_same_output():
    trades = [_trade(n, r) for n, r in
              [(10.0, 1.0), (-5.0, -0.5), (20.0, 2.0), (-10.0, -1.0), (5.0, 0.5)] * 8]  # n=40
    a = sc.scorecard(trades)
    b = sc.scorecard(list(trades))  # independent list, same content
    assert a == b
    assert a["ci_mean_r"] == b["ci_mean_r"]
    assert a["pvalue"] == b["pvalue"]


def test_scorecard_n5_insufficient_data():
    card = sc.scorecard(KNOWN)  # n=5
    assert card["metrics"]["n"] == 5
    assert card["verdict"]["code"] == "INSUFFICIENT_DATA"


def test_scorecard_all_positive_full_pipeline_promising():
    trades = [_trade(5.0, 0.5) for _ in range(20)] + [_trade(10.0, 1.0) for _ in range(15)]
    card = sc.scorecard(trades)  # n=35, all wins
    assert card["pvalue"] < 0.01
    assert card["ci_mean_r"][0] > 0
    assert card["verdict"]["code"] == "PROMISING"


def test_scorecard_benchmark_passthrough():
    bench = {"btc_ret_pct": 3.1, "agent_ret_pct": 4.0, "excess_pct": 0.9}
    card = sc.scorecard(KNOWN, benchmark=bench)
    assert card["benchmark"] == bench
    assert sc.scorecard(KNOWN)["benchmark"] is None
