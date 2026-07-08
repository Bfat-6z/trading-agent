"""llm_trader_risk — pure risk / liquidation / cost math for the paper LLM trader.

Plan: plans/260702-0900-llm-trader-core-upgrade (extraction items #1-#4).

WHY a separate pure module: llm_trader.py previously exited only on SL/TP
touch, which silently ignores forced liquidation, funding drag and per-coin
costs — exactly the optimism that makes a fake edge look real in paper.
Every function here is deterministic and side-effect free (no network, no
file/DB I/O) so the integrator and the tests exercise identical math.

Conventions:
- side is "LONG" or "SHORT" (case-insensitive).
- Costs are positive-means-cost; funding may be negative (we received it).
- Gating functions (can_open, daily_breaker) are FAIL-CLOSED: any malformed
  input refuses trading rather than crashing the loop or silently allowing.
"""
from __future__ import annotations

import math

from paper_cost_model import TAKER_FEE_RATE, fill_bps, liquidity_tier

_MS_PER_DAY = 86_400_000

# Maintenance-margin rate: pessimistic flat floors (plan item #1). BTC/ETH get
# a Binance-tier-1-ish 0.5%; every alt gets 1% because a $100 paper account
# never leaves bracket 1, and an optimistic MMR would place the liquidation
# price further from entry than the exchange actually would.
_MMR_MAJOR = 0.005
_MMR_DEFAULT = 0.01
_MAJOR_BASES = ("BTC", "ETH")
_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "USD")


def mmr_for(symbol: str) -> float:
    """Maintenance-margin rate: 0.005 for BTC/ETH, 0.01 for everything else.

    The quote suffix is stripped before matching so ETHFIUSDT (an alt) does
    not accidentally inherit the ETH major rate through a prefix match.
    """
    base = str(symbol or "").upper().strip()
    for suffix in _QUOTE_SUFFIXES:
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break
    return _MMR_MAJOR if base in _MAJOR_BASES else _MMR_DEFAULT


def liquidation_price(entry: float, leverage: int, side: str, mmr: float) -> float:
    """Isolated-margin liquidation price.

    LONG:  entry * (1 - 1/lev + mmr)   -> x10 @ mmr 1% liquidates at -9.0%
    SHORT: entry * (1 + 1/lev - mmr)

    WHY this form: with isolated margin the position is force-closed when the
    adverse move has eaten (initial margin - maintenance margin), i.e. a
    fraction (1/lev - mmr) of notional. The mmr term moves the liq price
    CLOSER to entry — ignoring it is the classic optimistic-backtest bug.
    """
    s = str(side).upper()
    lev = int(leverage)
    if lev <= 0:
        raise ValueError(f"leverage must be positive, got {leverage!r}")
    e = float(entry)
    m = float(mmr)
    # data-flow audit 2026-07-08: a NaN/inf/0 entry (or NaN mmr) sails through and yields a NaN/absurd
    # liq price that gets STORED on the position, then crashes exit_check (a raise OUTSIDE resolve's
    # try) on the next cycle -> the whole resolve batch dies. Fail loud HERE, consistent with lev<=0.
    if not (math.isfinite(e) and e > 0 and math.isfinite(m)):
        raise ValueError(f"liquidation_price needs finite entry>0 and finite mmr, got entry={entry!r} mmr={mmr!r}")
    if s == "LONG":
        return e * (1.0 - 1.0 / lev + m)
    if s == "SHORT":
        return e * (1.0 + 1.0 / lev - m)
    raise ValueError(f"unknown side {side!r}")


def exit_check(bar: dict, side: str, liq_px: float, sl: float,
               tp: float) -> tuple[float, str] | None:
    """Pessimistic intrabar exit resolution: liquidation -> sl -> tp.

    A single OHLC bar hides the intrabar path. When the bar touches both the
    liquidation price and the SL we cannot know which traded first, so we
    assume the WORST (liquidation) — killing a fake edge in paper is cheaper
    than discovering it is fake with real money. The same logic ranks sl
    above tp when both are touched. Levels that are None or <= 0 are treated
    as absent; a non-finite (NaN/inf) level or bar price raises ValueError —
    silently skipping a corrupted stop would disable the loss path while TP
    kept firing, i.e. asymmetric optimism. Returns (exit_px, reason) or None.

    Gap handling (adverse exits only): a stop-market cannot fill at a price
    that never traded. If bar["open"] is present and the bar opened through
    the level (gap), the fill is booked at the open; the fill is always
    clamped into [low, high]. TP stays booked at the tp level itself — a
    favorable gap would only improve a limit fill, and we do not take the
    improvement (pessimistic).
    """
    s = str(side).upper()
    if s not in ("LONG", "SHORT"):
        raise ValueError(f"unknown side {side!r}")
    high = float(bar["high"])
    low = float(bar["low"])
    if not (math.isfinite(high) and math.isfinite(low)):
        raise ValueError(f"non-finite bar high/low: {bar!r}")
    open_raw = bar.get("open")
    open_px: float | None = None
    if open_raw is not None:
        open_px = float(open_raw)
        if not math.isfinite(open_px):
            raise ValueError(f"non-finite bar open: {bar!r}")

    def _level(level: float | None) -> float | None:
        """None if absent; float if valid; raises on NaN/inf (fail-loud)."""
        if level is None:
            return None
        lv = float(level)
        if not math.isfinite(lv):
            raise ValueError(f"non-finite exit level {level!r}")
        if lv <= 0.0:
            return None
        return lv

    def _touched(lv: float | None, *, adverse: bool) -> bool:
        if lv is None:
            return False
        if adverse:  # liq / sl sit on the losing side of entry
            return low <= lv if s == "LONG" else high >= lv
        return high >= lv if s == "LONG" else low <= lv

    def _adverse_fill(lv: float) -> float:
        # Gap-through: bar opened past the stop -> stop-market fills at open.
        if s == "LONG":
            px = lv if open_px is None else min(lv, open_px)
            return max(min(px, high), low)  # only traded prices are fillable
        px = lv if open_px is None else max(lv, open_px)
        return min(max(px, low), high)

    liq_lv = _level(liq_px)
    sl_lv = _level(sl)
    tp_lv = _level(tp)
    if _touched(liq_lv, adverse=True):
        return _adverse_fill(liq_lv), "liquidation"
    if _touched(sl_lv, adverse=True):
        return _adverse_fill(sl_lv), "sl"
    if _touched(tp_lv, adverse=False):
        return tp_lv, "tp"
    return None


def funding_cost(side: str, qty: float, entry_px: float,
                 events: list[tuple[int, float]], t0_ms: int, t1_ms: int) -> float:
    """Total funding over the half-open window (t0_ms, t1_ms]; positive = cost.

    Each event (ts_ms, rate) with t0 < ts <= t1 charges rate * notional.
    LONG pays when rate > 0 (the usual perp premium), SHORT receives it —
    hence the sign flip; a negative total means we were paid. Notional is
    frozen at entry (qty * entry_px): paper positions here do not compound
    and mark-price notional is not available offline. The half-open boundary
    guarantees consecutive holding windows never double-charge one event.
    """
    s = str(side).upper()
    if s == "LONG":
        sign = 1.0
    elif s == "SHORT":
        sign = -1.0
    else:
        raise ValueError(f"unknown side {side!r}")
    notional = float(qty) * float(entry_px)
    t0 = int(t0_ms)
    t1 = int(t1_ms)
    total = 0.0
    for ts, rate in events or []:
        if t0 < int(ts) <= t1:
            total += sign * float(rate) * notional
    return total


def trade_costs(entry: float, exit_px: float, qty: float, quote_vol: float) -> dict:
    """Round-trip fee + slippage inputs, graded by liquidity tier.

    Fee is taker on BOTH legs (pessimistic, single source: paper_cost_model).
    slip_bps is informational: the integrator applies slippage by adjusting
    the fill prices themselves — charging it here as well would double count.
    slip_bps is for plain market fills (entries, tp); slip_bps_stop applies
    paper_cost_model's STOP_SLIPPAGE_MULTIPLIER and MUST be used for sl /
    liquidation exits — stop-markets slip ~3x worse, and using the plain
    number on the loss path understates adverse slippage.
    Returns {"fee": float, "slip_bps": float, "slip_bps_stop": float, "tier": str}.
    """
    tier = liquidity_tier(quote_vol)
    notional_both_legs = (abs(float(entry)) + abs(float(exit_px))) * abs(float(qty))
    fee = float(TAKER_FEE_RATE) * notional_both_legs
    return {
        "fee": fee,
        "slip_bps": float(fill_bps(tier)),
        "slip_bps_stop": float(fill_bps(tier, is_stop=True)),
        "tier": tier,
    }


def net_pnl(side: str, entry: float, exit_px: float, qty: float, margin: float,
            fee: float, funding: float, liquidated: bool) -> float:
    """Net PnL with the isolated-margin floor.

    gross = directional move * qty; net = gross - fee - funding. On a forced
    liquidation the WHOLE margin is gone regardless of the residual math (the
    exchange keeps the maintenance buffer plus a liquidation fee), so net is
    pinned to exactly -margin. Isolated margin also means we can never lose
    MORE than margin, hence the max() floor on the normal path — fees and
    funding cannot push the loss past what was posted.
    """
    s = str(side).upper()
    m = float(margin)
    if liquidated:
        return -m
    if s == "LONG":
        gross = (float(exit_px) - float(entry)) * float(qty)
    elif s == "SHORT":
        gross = (float(entry) - float(exit_px)) * float(qty)
    else:
        raise ValueError(f"unknown side {side!r}")
    net = gross - float(fee) - float(funding)
    # NaN-safe (data-flow audit 2026-07-08): a non-finite leg (e.g. a "nan" funding rate) makes
    # net=NaN, and max(NaN,-m) returns NaN in Python (first-arg-wins) -> written to equity
    # permanently -> daily_breaker then fail-closes ALL trading forever. Non-finite -> the -margin
    # floor (the worst realized case). Same isfinite discipline the gating funcs already use.
    return -m if not math.isfinite(net) else max(net, -m)


def can_open(new_margin: float, equity: float, open_positions: list[dict],
             max_total_margin_pct: float = 60.0,
             max_concurrent: int = 4) -> tuple[bool, str]:
    """Pre-trade caps; (True, "ok") only when EVERY cap passes (fail-closed).

    Caps (plan item #4): at most max_concurrent open positions, and total
    committed margin (existing + new) capped at max_total_margin_pct of
    equity, so one bad session cannot commit the whole account. WHY the
    blanket except: a gating function that crashes lets the caller's error
    handling decide — refusing here keeps the failure mode safe by default.
    """
    try:
        nm = float(new_margin)
        eq = float(equity)
        # NaN never raises and every NaN comparison is False, so an explicit
        # isfinite gate is required — otherwise NaN sails through <=0 and the
        # cap check below, approving unlimited positions (fail-OPEN).
        if not (math.isfinite(nm) and math.isfinite(eq)) or nm <= 0.0 or eq <= 0.0:
            return False, f"invalid margin/equity (new_margin={new_margin!r}, equity={equity!r})"
        positions = list(open_positions or [])
        if len(positions) >= int(max_concurrent):
            return False, f"max_concurrent reached ({len(positions)}/{int(max_concurrent)})"
        used = sum(float(p["margin"]) for p in positions)
        if not math.isfinite(used):
            return False, f"invalid open position margins (sum={used!r})"
        cap = eq * float(max_total_margin_pct) / 100.0
        if not math.isfinite(cap):
            return False, f"invalid max_total_margin_pct {max_total_margin_pct!r}"
        if used + nm > cap:
            return False, (f"total margin {used + nm:.2f} > cap {cap:.2f} "
                           f"({float(max_total_margin_pct):.0f}% of equity {eq:.2f})")
        return True, "ok"
    except Exception as exc:  # malformed position rows, non-numeric args, ...
        return False, f"can_open_error_fail_closed: {exc}"


def daily_breaker(closed: list[dict], equity_day_start: float, now_ms: int,
                  max_daily_loss_pct: float = 15.0) -> tuple[bool, str]:
    """True = trading BLOCKED for the current UTC day.

    Sums net of trades whose closed_ts falls in the SAME UTC day as now_ms
    (integer day index: ms // 86_400_000 — no timezone/DST ambiguity).
    Blocked once realized loss reaches max_daily_loss_pct of the day-start
    equity. Malformed rows contribute 0 (they cannot hide a loss, but they
    cannot fake a profit either); any structural exception BLOCKS trading —
    a circuit breaker that crashes open is not a breaker.
    """
    try:
        eq0 = float(equity_day_start)
        # float(nan) does not raise and nan<=0 is False: without the isfinite
        # gate a NaN day-start equity yields a NaN limit that never compares
        # True, i.e. the breaker silently never blocks (fail-OPEN).
        if not math.isfinite(eq0) or eq0 <= 0.0:
            return True, f"breaker: invalid equity_day_start {equity_day_start!r}"
        day = int(now_ms) // _MS_PER_DAY
        realized = 0.0
        for row in closed:
            try:
                if int(row["closed_ts"]) // _MS_PER_DAY != day:
                    continue
                net = float(row["net"])
                if not math.isfinite(net):
                    continue  # NaN would poison the whole sum, disabling the breaker
                realized += net
            except Exception:
                continue  # malformed row = 0 contribution, never unblocks
        limit = -eq0 * float(max_daily_loss_pct) / 100.0
        if not math.isfinite(limit):
            return True, f"breaker: invalid max_daily_loss_pct {max_daily_loss_pct!r}"
        if realized <= limit:
            return True, f"daily_breaker: realized {realized:.2f} <= limit {limit:.2f}"
        return False, "ok"
    except Exception:
        return True, "breaker_error_fail_closed"
