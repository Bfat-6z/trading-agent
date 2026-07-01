"""Phase 2 — central, pessimistic-leaning cost model for the paper simulator.

Single source of truth so the three exit/cost sites (paper_execution_simulator,
paper_execution_lifecycle_loop timeout exit, counterfactual_replay_agent shadow
engine) cannot diverge. Pure functions, no network, no state.

Rationale (edge-first plan 260701, owner chose PESSIMISTIC): it is far better to
kill a fake edge in paper than to discover it is fake with real money. Costs are
graded by liquidity tier: majors stay cheap (~1-2bps), microcaps carry heavy
floors (they genuinely do). Numbers sourced in phase2-design.md.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

# Fees (Binance USDT-M VIP0, no BNB discount) — taker on both legs (pessimistic).
TAKER_FEE_RATE = Decimal("0.0005")   # 5.0 bps
MAKER_FEE_RATE = Decimal("0.0002")   # 2.0 bps

# Liquidity tiers by 24h quote volume (USDT).
MAJOR_MIN_QUOTE_VOLUME = Decimal("500000000")   # >= $500M -> major
MID_MIN_QUOTE_VOLUME = Decimal("50000000")      # >= $50M  -> mid, else micro

# Half-spread charged on every fill (bps of price), by tier.
HALF_SPREAD_BPS = {"major": Decimal("1"), "mid": Decimal("6"), "micro": Decimal("30")}
# Market-order slippage (bps), by tier. Floors, leaning pessimistic.
SLIPPAGE_BPS = {"major": Decimal("2"), "mid": Decimal("10"), "micro": Decimal("40")}
# Stop-market orders slip worse in volatility.
STOP_SLIPPAGE_MULTIPLIER = Decimal("3")

# Maintenance margin: conservative floor. $100 account is always Binance bracket
# 1; full tiered brackets deferred (see phase2-design.md). A floor >= 1% for
# unknown/alt symbols is safer than a fake-precise flat 0.5%.
MMR_MAJOR = Decimal("0.004")   # BTC/ETH tier-1
MMR_DEFAULT_FLOOR = Decimal("0.01")


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def liquidity_tier(quote_volume_24h: Any) -> str:
    """Classify a symbol's liquidity from its 24h quote volume (USDT)."""
    qv = _dec(quote_volume_24h)
    if qv >= MAJOR_MIN_QUOTE_VOLUME:
        return "major"
    if qv >= MID_MIN_QUOTE_VOLUME:
        return "mid"
    return "micro"


def half_spread_bps(tier: str) -> Decimal:
    return HALF_SPREAD_BPS.get(tier, HALF_SPREAD_BPS["micro"])


def slippage_bps(tier: str, *, is_stop: bool = False) -> Decimal:
    """Total adverse bps for a fill = slippage + half-spread; stops slip worse."""
    base = SLIPPAGE_BPS.get(tier, SLIPPAGE_BPS["micro"])
    if is_stop:
        base = base * STOP_SLIPPAGE_MULTIPLIER
    return base + half_spread_bps(tier)


def mmr_for(tier: str) -> Decimal:
    """Maintenance-margin rate floor for the liquidity tier (conservative)."""
    if tier == "major":
        return MMR_MAJOR
    return MMR_DEFAULT_FLOOR


def fill_bps(tier: str, *, is_stop: bool = False) -> Decimal:
    """Convenience: total adverse bps to apply to a market/stop fill price."""
    return slippage_bps(tier, is_stop=is_stop)
