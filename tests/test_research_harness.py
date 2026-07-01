"""HARNESS-5/6 tests: orchestrator wiring, ledger, ranked report, universe."""
from datetime import datetime, timedelta, timezone

import backtest_chart_signal as cs
import research_harness as rh
import research_ledger as rl
import universe_selector as us


def _bars(n, step_s=300):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    px = 100.0
    for i in range(n):
        drift = (1.0 if (i // 13) % 2 == 0 else -1.0) * 0.5
        o = px; c = px + drift
        hi = max(o, c) + 0.7; lo = min(o, c) - 0.7
        ot = (t0 + timedelta(seconds=step_s * i)).isoformat(timespec="seconds")
        ct = (t0 + timedelta(seconds=step_s * (i + 1))).isoformat(timespec="seconds")
        out.append({"open_time": ot, "close_time": ct, "open": f"{o:.4f}", "high": f"{hi:.4f}",
                    "low": f"{lo:.4f}", "close": f"{c:.4f}", "volume": "1500", "quote_volume": "150000",
                    "is_final": True, "available_at": ct, "known_at": ct, "ingested_at": ct, "finalized_at": ct})
        px = c
    return out


def _factory(params):
    return {"name": "reject_ema_short", "direction": "SHORT",
            "entry": {"all": [{"block": "trend_ema_stack"},
                              {"block": "location_reject_ema_from_below"},
                              {"block": "regime_adx_min", "params": {"adx_min": params["adx_min"]}}]},
            "exit": {"sl_atr": params["sl_atr"], "tp_atr": params["tp_atr"]}}


def test_ledger_append_and_rank(tmp_path):
    lp = tmp_path / "led.jsonl"; rp = tmp_path / "ranked.md"
    rl.append_row({"family": "f", "timeframe": "1h", "direction": "SHORT",
                   "in_sample": {"expectancy_r": 0.1, "trades": 500},
                   "holdout": {"expectancy_r": 0.2, "trades": 450},
                   "dsr": {"dsr": 0.97}, "verdict": "PASS", "reason": "ok"}, ledger_path=lp)
    rl.append_row({"family": "f", "timeframe": "15m", "direction": "LONG",
                   "in_sample": {"expectancy_r": -0.3, "trades": 800},
                   "holdout": None, "dsr": {"dsr": 0.1}, "verdict": "KILL",
                   "reason": "failed_gate"}, ledger_path=lp)
    content = rl.regenerate_ranked(ledger_path=lp, ranked_path=rp)
    assert "Total setups tested: **2**" in content
    # PASS row (holdout 0.2) ranks above the KILL row (no holdout). Check the
    # table rows (start with "| 1 " / "| 2 "), not the descriptive header prose.
    rows = [ln for ln in content.splitlines() if ln.startswith("| 1 ") or ln.startswith("| 2 ")]
    assert "PASS" in rows[0] and "KILL" in rows[1]


def test_run_family_kill_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "LEDGER_PATH", tmp_path / "led.jsonl")
    monkeypatch.setattr(rl, "RANKED_PATH", tmp_path / "ranked.md")
    monkeypatch.setattr(rh, "REPORT_DIR", tmp_path / "reports")
    import sweep_runner as sw
    monkeypatch.setattr(sw, "SWEEP_DIR", tmp_path / "sweeps")

    b5 = _bars(400); b1 = _bars(80, step_s=3600)
    ds = {f"S{i}USDT": {"bars_5m": b5, "bars_1h": b1, "quote_volume_24h": 1e10} for i in range(3)}
    df = cs.compute_indicators(b5)
    split = int(df.iloc[300]["ts_ms"])
    grid = {"adx_min": [20, 25], "sl_atr": [1.5], "tp_atr": [3.0]}
    row = rh.run_family("reject_ema", _factory, grid, ds, entry_tf="5m",
                        split_ts_ms=split, stamped_at="2026-07-01T00:00:00Z")
    # synthetic data has no real edge -> must KILL, holdout must stay sealed
    assert row["verdict"] == "KILL"
    assert row["holdout"] is None  # not peeked because in-sample gate failed
    assert (tmp_path / "reports" / "sweep_reject_ema_5m.md").exists()


def test_universe_volume_at_start(monkeypatch):
    # objective liquidity measure sums quote_volume over first day
    bars = _bars(200, step_s=3600)  # 1h bars, quote_volume 150000 each
    vol = us.start_window_daily_quote_volume(bars, "1h")
    assert vol == 150000 * 24  # first 24 bars
