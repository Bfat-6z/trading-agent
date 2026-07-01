"""HARNESS-3 tests: sweep runner enumerates + backtests IN-SAMPLE only."""
from datetime import datetime, timedelta, timezone

import backtest_chart_signal as cs
import sweep_runner as sw


def _bars(n, step_s=300):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    px = 100.0
    for i in range(n):
        drift = (1.0 if (i // 11) % 2 == 0 else -1.0) * 0.5
        o = px; c = px + drift
        hi = max(o, c) + 0.7; lo = min(o, c) - 0.7
        ot = (t0 + timedelta(seconds=step_s * i)).isoformat(timespec="seconds")
        ct = (t0 + timedelta(seconds=step_s * (i + 1))).isoformat(timespec="seconds")
        out.append({"open_time": ot, "close_time": ct, "open": f"{o:.4f}", "high": f"{hi:.4f}",
                    "low": f"{lo:.4f}", "close": f"{c:.4f}", "volume": f"{1000 + (700 if i % 3 == 0 else 0)}",
                    "is_final": True, "available_at": ct, "known_at": ct, "ingested_at": ct, "finalized_at": ct})
        px = c
    return out


def _datasets():
    b5 = _bars(400); b1 = _bars(80, step_s=3600)
    return {"AAAUSDT": {"bars_5m": b5, "bars_1h": b1, "quote_volume_24h": 1e10},
            "BBBUSDT": {"bars_5m": b5, "bars_1h": b1, "quote_volume_24h": 5e9}}


def _factory(params):
    return {
        "name": "reject_ema_short",
        "direction": "SHORT",
        "entry": {"all": [
            {"block": "trend_ema_stack"},
            {"block": "location_reject_ema_from_below"},
            {"block": "regime_adx_min", "params": {"adx_min": params["adx_min"]}},
        ]},
        "exit": {"sl_atr": params["sl_atr"], "tp_atr": params["tp_atr"]},
    }


def test_expand_grid_cartesian():
    grid = {"a": [1, 2], "b": [3, 4, 5]}
    combos = sw.expand_grid(grid)
    assert len(combos) == 6
    assert {"a": 1, "b": 3} in combos and {"a": 2, "b": 5} in combos


def test_build_specs_dedups_by_id():
    grid = {"adx_min": [20, 20], "sl_atr": [1.5], "tp_atr": [3.0]}  # duplicate adx_min
    specs = sw.build_specs(_factory, grid)
    assert len(specs) == 1  # dedup


def test_run_sweep_counts_trials_and_is_insample_only(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "SWEEP_DIR", tmp_path / "sweeps")
    ds = _datasets()
    df = cs.compute_indicators(ds["AAAUSDT"]["bars_5m"])
    split = int(df.iloc[300]["ts_ms"])
    grid = {"adx_min": [15, 20, 25], "sl_atr": [1.0, 1.5], "tp_atr": [2.0, 3.0]}
    res = sw.run_sweep(_factory, grid, ds, split, sweep_name="unit")
    # honest trial count = number of unique specs (3*2*2 = 12)
    assert res["n_trials"] == 12
    assert len(res["results"]) == 12
    # every trade must be in-sample (entry before split)
    for r in res["results"]:
        for t in r["trades"]:
            assert int(t["entry_ts"]) < split, "sweep leaked holdout data"
    # a log file was written
    assert (tmp_path / "sweeps" / "unit_insample.jsonl").exists()
