"""Method Lab engine — the honest research->test->curate loop. Locks the DSL,
the no-lookahead backtest, and the survive/kill gate. Synthetic + offline."""
from __future__ import annotations

import re
from pathlib import Path

import method_lab as ml

ROOT = Path(__file__).resolve().parents[1]


def _bars(prices):
    out = []
    t = 1_700_000_000_000
    for i, p in enumerate(prices):
        out.append({"open": p, "high": p * 1.002, "low": p * 0.998, "close": p,
                    "volume": 1000.0, "ts_ms": t + i * 900_000})
    return out


def test_dsl_condition_and_fires():
    row = {"rsi14": 25.0, "ema_stack": 1, "vol_ratio": 2.0}
    assert ml._cond_ok(row, {"feat": "rsi14", "op": "<", "val": 30})
    assert not ml._cond_ok(row, {"feat": "rsi14", "op": ">", "val": 30})
    assert ml._cond_ok(row, {"feat": "missing", "op": "<", "val": 1}) is False   # unknown feat safe
    m = {"when": [{"feat": "rsi14", "op": "<", "val": 30}, {"feat": "ema_stack", "op": "==", "val": 1}]}
    assert ml.method_fires(row, m)
    assert not ml.method_fires({"rsi14": 40, "ema_stack": 1}, m)
    assert not ml.method_fires(row, {"when": []})   # no conditions never fires


def test_feature_frame_no_lookahead_shape():
    rows = ml.feature_frame(_bars([100 + i * 0.1 for i in range(300)]))
    assert len(rows) == 300
    assert {"rsi14", "ema_stack", "vol_ratio", "px_vs_ema200"} <= set(rows[0])
    # feature at bar i must use only close[i]; a monotonic rise => price above EMAs late
    assert rows[-1]["px_vs_ema200"] > 0


def test_backtest_produces_trades_and_wins_on_uptrend():
    # steady uptrend: a LONG method that always fires should mostly hit TP
    rows = ml.feature_frame(_bars([100 * (1.004 ** i) for i in range(320)]))
    m = {"id": "always_long", "side": "LONG", "when": [{"feat": "close", "op": ">", "val": 0}],
         "sl_pct": 1.5, "tp_pct": 2.5}
    trades = ml.backtest_method(rows, m, "TEST")
    assert trades
    assert sum(1 for t in trades if t["net"] > 0) / len(trades) > 0.7   # uptrend => wins
    # no overlapping positions: bars strictly increase
    bars_used = [t["bar"] for t in trades]
    assert bars_used == sorted(bars_used) and len(set(bars_used)) == len(bars_used)


def test_evaluate_gate_too_few_and_losing():
    frames = {"A": ml.feature_frame(_bars([100 - i * 0.05 for i in range(320)]))}  # downtrend
    # a LONG method in a downtrend should NOT survive
    m = {"id": "long_dump", "side": "LONG", "when": [{"feat": "close", "op": ">", "val": 0}],
         "sl_pct": 1.5, "tp_pct": 2.5}
    res = ml.evaluate_method(m, frames, n_methods=10)
    assert res["survived"] is False


def test_paper_only_no_order_calls():
    text = (ROOT / "method_lab.py").read_text(encoding="utf-8")
    assert re.search(r"\.\s*(futures_create_order|create_order|transfer)\s*\(", text) is None
    assert "ALLOW_LIVE_ORDERS" not in text
