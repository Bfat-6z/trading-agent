"""HARNESS-1 tests: every strategy block is NO-LOOKAHEAD.

Core proof: a block's value at bar i must depend only on bars 0..i. We verify by
computing a block on the full series, then on a truncated series (0..i), and
asserting the value at i is identical. If a block peeked at future bars, the two
would differ. Run for every block in the registry.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import backtest_chart_signal as cs
import strategy_blocks as sb


def _bars(n, step_s=300, base=100.0, wave=True):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out = []
    px = base
    for i in range(n):
        # a wavy series so trend/structure/pullback conditions actually toggle
        drift = (1.0 if (i // 7) % 2 == 0 else -1.0) * 0.4 if wave else 0.2
        o = px
        c = px + drift
        hi = max(o, c) + 0.6
        lo = min(o, c) - 0.6
        ot = (t0 + timedelta(seconds=step_s * i)).isoformat(timespec="seconds")
        ct = (t0 + timedelta(seconds=step_s * (i + 1))).isoformat(timespec="seconds")
        vol = 1000 + (500 if i % 5 == 0 else 0)
        out.append({"open_time": ot, "close_time": ct, "open": f"{o:.4f}", "high": f"{hi:.4f}",
                    "low": f"{lo:.4f}", "close": f"{c:.4f}", "volume": f"{vol}", "is_final": True,
                    "available_at": ct, "known_at": ct, "ingested_at": ct, "finalized_at": ct})
        px = c
    return out


BLOCK_PARAMS = {
    "regime_atr_percentile": {"low_pct": 0.0, "high_pct": 1.0, "window": 50},
    "structure_break": {"left": 2, "right": 2},
    "volume_min_ratio": {"min_ratio": 1.2},
    "volume_spike": {"mult": 1.5},
    "location_near_ema": {"max_atr": 1.0},
    "location_not_overextended": {"max_atr": 2.0},
}


@pytest.mark.parametrize("name", list(sb.BLOCKS.keys()))
@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_block_is_no_lookahead(name, direction):
    bars = _bars(180)
    df_full = cs.compute_indicators(bars)
    params = BLOCK_PARAMS.get(name)
    full = sb.evaluate_block(name, df_full, direction, params)

    # truncate to 0..cut and recompute; the value at `cut` must match `full[cut]`.
    for cut in (120, 150, 170):
        df_trunc = cs.compute_indicators(bars[: cut + 1])
        trunc = sb.evaluate_block(name, df_trunc, direction, params)
        assert bool(trunc.iloc[cut]) == bool(full.iloc[cut]), (
            f"{name}/{direction} at bar {cut} changed when future bars removed "
            f"-> lookahead leak")


def test_registry_covers_all_families():
    names = set(sb.BLOCKS)
    for fam in ("trend_", "regime_", "structure_", "volume_", "location_"):
        assert any(n.startswith(fam) for n in names), f"missing family {fam}"


def test_unknown_block_raises():
    df = cs.compute_indicators(_bars(60))
    with pytest.raises(KeyError):
        sb.evaluate_block("does_not_exist", df, "LONG")


def test_blocks_return_bool_series_aligned():
    df = cs.compute_indicators(_bars(80))
    s = sb.evaluate_block("trend_ema_stack", df, "LONG")
    assert len(s) == len(df)
    assert s.dtype == bool
