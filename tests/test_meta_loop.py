"""Meta-loop Layer 2-4 tests (pure pieces; the live iteration is run separately)."""
from datetime import datetime, timedelta, timezone

import backtest_chart_signal as cs
import meta_loop as ml
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


def test_generate_specs_have_hypotheses_and_valid_blocks():
    specs = ml.generate_specs()
    assert len(specs) > 20
    for s in specs:
        assert s.get("hypothesis") and s.get("source")
        assert sc.validate_spec(s) == []           # every generated spec compiles
    # dedup: unique spec ids
    ids = [sc.spec_id(s) for s in specs]
    assert len(ids) == len(set(ids))


def test_generate_specs_skips_illogical_combo():
    specs = ml.generate_specs()
    for s in specs:
        blocks = {b["block"] for b in s["entry"]["all"]}
        # trend-continuation trigger must NOT carry a funding-fade filter
        assert not ({"ts_momentum", "funding_zscore_fade"} <= blocks)


def test_guard_specs_rejects_repaint(monkeypatch):
    import pandas as pd
    df = cs.compute_indicators(_bars(160))
    df1 = cs.compute_indicators(_bars(40, step_s=3600))
    specs = ml.generate_specs()[:5]

    def leaky_mask(spec, d, d1):
        m = pd.Series(False, index=d.index)
        if len(d) >= 160:
            m.iloc[len(d) - 2] = True   # depends on full length -> repaint
        return m

    monkeypatch.setattr(sc, "compute_mask", leaky_mask)
    clean, rejected = ml.guard_specs(specs, df, df1)
    assert len(rejected) == len(specs) and not clean   # all leak -> all rejected


def test_guard_specs_keeps_causal_specs():
    df = cs.compute_indicators(_bars(200))
    df1 = cs.compute_indicators(_bars(40, step_s=3600))
    specs = ml.generate_specs()[:8]
    clean, rejected = ml.guard_specs(specs, df, df1)
    assert not rejected and len(clean) == len(specs)   # real blocks are causal


def test_component_stats_from_sweeps(tmp_path):
    import json
    d = tmp_path / "sweeps"; d.mkdir()
    rows = [
        {"spec": {"entry": {"all": [{"block": "ts_momentum"}]}}, "in_sample": {"expectancy_r": 0.1, "trades": 500}},
        {"spec": {"entry": {"all": [{"block": "bb_reversion"}]}}, "in_sample": {"expectancy_r": -0.2, "trades": 400}},
        {"spec": {"entry": {"all": [{"block": "bb_reversion"}]}}, "in_sample": {"expectancy_r": -0.1, "trades": 300}},
        {"spec": {"entry": {"all": [{"block": "ts_momentum"}]}}, "in_sample": {"expectancy_r": 0.05, "trades": 50}},  # <100 -> ignored
    ]
    with open(d / "x_insample.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    stats = ml.component_stats_from_sweeps(sweep_dir=d)
    assert stats["ts_momentum"]["n"] == 1          # the <100-trade row ignored
    assert stats["bb_reversion"]["always_negative"] is True


def test_dry_streak_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "DRY_STREAK_PATH", tmp_path / "dry.json")
    assert ml._read_dry_streak() == 0
    ml._write_dry_streak(2)
    assert ml._read_dry_streak() == 2


def test_evolve_pools_drops_proven_bad_components():
    # stats say sweep_reversal + cvd_reversal are below the drop threshold; funding
    # zscore too. evolve must drop them and keep the rest.
    stats = {
        "sweep_reversal": {"mean_expectancy_r": -0.26, "n": 496},
        "cvd_reversal": {"mean_expectancy_r": -0.21, "n": 120},
        "funding_zscore_fade": {"mean_expectancy_r": -0.20, "n": 150},
        "ts_momentum": {"mean_expectancy_r": -0.10, "n": 72},   # above -0.15 -> kept
    }
    ev = ml.evolve_pools_from_stats(stats, drop_below=-0.15)
    trig_blocks = {t.get("block") for t in ev["triggers"]}
    filt_blocks = {f.get("block") for f in ev["filters"]}
    assert "sweep_reversal" not in trig_blocks      # dropped by number
    assert "cvd_reversal" not in filt_blocks        # flow filter dropped
    assert "funding_zscore_fade" not in filt_blocks
    assert "ts_momentum" in trig_blocks             # least-bad kept
    assert any(d[0] == "trigger" for d in ev["dropped"])


def test_generate_specs_with_evolved_pools_and_ema_trigger():
    # extended pool with the direction-specific EMA-location trigger compiles
    triggers = [ml.EMA_LOCATION_TRIGGER, {"block": "ts_momentum", "params": [{"lookback": 20}],
                                          "src": "T3", "hyp": "momentum"}]
    specs = ml.generate_specs(triggers=triggers, gates=[{"block": None, "params": None, "hyp": "none"}],
                              filters=[{"block": None, "params": None, "hyp": "none"}])
    blocks = {b["block"] for s in specs for b in s["entry"]["all"]}
    assert "location_reject_ema_from_below" in blocks    # SHORT direction
    assert "location_reclaim_ema_from_above" in blocks   # LONG direction
    for s in specs:
        assert sc.validate_spec(s) == []
