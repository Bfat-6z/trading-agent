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


def test_cost_monotonic_across_tiers_and_missing_is_micro():
    """Worse liquidity = worse fill (higher cost). And MISSING quote_volume must
    fall to the most-expensive micro tier, never the cheap major default —
    unknown liquidity is treated pessimistically."""
    candle = {"ts": "t", "open": "100.5", "high": "103", "low": "99.5", "close": "100.8"}
    major = Decimal(sim.simulate_exit("LONG", "100", "1", "98", "102", [candle], "10", quote_volume=1_000_000_000)["exit"])
    mid = Decimal(sim.simulate_exit("LONG", "100", "1", "98", "102", [candle], "10", quote_volume=100_000_000)["exit"])
    micro = Decimal(sim.simulate_exit("LONG", "100", "1", "98", "102", [candle], "10", quote_volume=1_000_000)["exit"])
    missing = Decimal(sim.simulate_exit("LONG", "100", "1", "98", "102", [candle], "10")["exit"])
    # LONG TP: lower exit = worse fill. micro worst, major best.
    assert micro < mid < major
    # missing quote_volume must equal the micro (pessimistic) fill, not major.
    assert missing == micro


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
