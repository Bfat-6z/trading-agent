"""Phase 2 — pessimistic tiered cost model + monotonic-cost guarantee."""
from decimal import Decimal

import paper_cost_model as cm
import paper_execution_simulator as sim


def test_tiers_by_quote_volume():
    assert cm.liquidity_tier(1_000_000_000) == "major"
    assert cm.liquidity_tier(100_000_000) == "mid"
    assert cm.liquidity_tier(1_000_000) == "micro"
    assert cm.liquidity_tier(0) == "micro"


def test_cost_ordering_micro_worse_than_major():
    assert cm.slippage_bps("micro") > cm.slippage_bps("mid") > cm.slippage_bps("major")
    assert cm.half_spread_bps("micro") > cm.half_spread_bps("major")
    # stop orders slip worse than market
    assert cm.slippage_bps("major", is_stop=True) > cm.slippage_bps("major")
    assert cm.mmr_for("micro") >= cm.mmr_for("major")


def test_new_model_is_never_cheaper_than_old_flat_2bps():
    """Monotonic-cost guarantee: for every tier, a TP exit under the new model
    fills no better than the legacy flat-2bps path (we removed optimism, not
    added it)."""
    candle = {"ts": "t", "open": "100.5", "high": "103", "low": "99.5", "close": "100.8"}
    legacy = sim.simulate_exit("LONG", "100", "1", "98", "102", [candle], "10")  # no quote_volume -> legacy 2bps
    for qv in (1_000_000_000, 100_000_000, 1_000_000):
        new = sim.simulate_exit("LONG", "100", "1", "98", "102", [candle], "10", quote_volume=qv)
        # LONG TP: lower exit = worse fill = higher cost
        assert Decimal(new["exit"]) <= Decimal(legacy["exit"]), f"tier {qv} exit better than legacy"


def test_stop_exit_costs_more_than_market_exit_same_tier():
    sl_candle = {"ts": "t", "open": "97", "high": "97.5", "low": "96", "close": "97"}
    tp_candle = {"ts": "t", "open": "100.5", "high": "103", "low": "99.5", "close": "100.8"}
    sl = sim.simulate_exit("LONG", "100", "1", "98", "102", [sl_candle], "10", quote_volume=1_000_000_000)
    tp = sim.simulate_exit("LONG", "100", "1", "98", "102", [tp_candle], "10", quote_volume=1_000_000_000)
    assert sl["reason"] == "sl" and tp["reason"] == "tp"
    assert Decimal(sl["slippage_bps_applied"]) > Decimal(tp["slippage_bps_applied"])


def test_liquidation_mmr_is_tiered_not_flat():
    # micro tier MMR (1%) pulls liq closer to entry than the old flat 0.5%.
    liq_micro = sim.liquidation_price(Decimal("10"), "LONG", "50", quote_volume=1000)
    liq_legacy = sim.liquidation_price(Decimal("10"), "LONG", "50")  # flat 0.5%
    assert liq_micro > liq_legacy  # closer to entry (10) = more conservative
