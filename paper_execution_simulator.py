"""Realistic-enough paper execution simulator for Phase B learning."""
from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, write_json_atomic
from paper_cost_model import fill_bps, liquidity_tier, mmr_for
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
PAPER_ORDERS = STATE_DIR / "paper_orders.jsonl"
PAPER_POSITIONS = STATE_DIR / "paper_positions.json"

TAKER_FEE_RATE = Decimal("0.0005")
MAKER_FEE_RATE = Decimal("0.0002")
DEFAULT_SLIPPAGE_BPS = Decimal("2")  # legacy default; real fills use paper_cost_model tiers
MAINTENANCE_MARGIN_RATE = Decimal("0.005")  # legacy; liquidation now uses mmr_for(tier)


def dec(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def dstr(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.00000001")).normalize())


def order_id(payload: dict[str, Any]) -> str:
    return "paper_order_" + hashlib.sha256(repr(sorted(payload.items())).encode("utf-8")).hexdigest()[:20]


def adverse_slippage(price: Decimal, side: str, bps: Decimal = DEFAULT_SLIPPAGE_BPS) -> Decimal:
    factor = bps / Decimal("10000")
    if side.upper() == "LONG":
        return price * (Decimal("1") + factor)
    return price * (Decimal("1") - factor)


def exit_slippage(price: Decimal, side: str, bps: Decimal = DEFAULT_SLIPPAGE_BPS) -> Decimal:
    factor = bps / Decimal("10000")
    if side.upper() == "LONG":
        return price * (Decimal("1") - factor)
    return price * (Decimal("1") + factor)


def simulate_entry_order(
    symbol: str,
    side: str,
    order_type: str,
    qty: Any,
    price: Any,
    candle: dict[str, Any],
    post_only: bool = False,
    append_order: bool = True,
    quote_volume: Any = None,
) -> dict[str, Any]:
    side_up = side.upper()
    order_type = order_type.lower()
    qty_dec = dec(qty)
    requested = dec(price)
    open_price = dec(candle.get("open"), str(requested))
    high = dec(candle.get("high"))
    low = dec(candle.get("low"))
    filled = False
    fill_price = requested
    fee_rate = TAKER_FEE_RATE
    reason = "unfilled"
    # Phase 2: market entry pays tiered slippage + half-spread (pessimistic).
    # Missing quote_volume -> "micro" tier (most expensive), never the cheap default.
    entry_bps = fill_bps(liquidity_tier(quote_volume))
    if order_type == "market":
        filled = True
        fill_price = adverse_slippage(open_price if open_price > 0 else requested, side_up, entry_bps)
        reason = "market_fill"
    elif order_type == "limit":
        touch = low <= requested if side_up == "LONG" else high >= requested
        if touch and not post_only:
            filled = True
            fill_price = requested
            fee_rate = MAKER_FEE_RATE
            reason = "limit_fill"
        elif touch and post_only:
            reason = "post_only_resting"
        else:
            reason = "limit_unfilled"
    else:
        reason = "unsupported_order_type"
    fill_fraction = dec(candle.get("fill_fraction"), "1") if filled else Decimal("0")
    if fill_fraction < 0:
        fill_fraction = Decimal("0")
    if fill_fraction > 1:
        fill_fraction = Decimal("1")
    filled_qty = qty_dec * fill_fraction if filled else Decimal("0")
    remaining_qty = max(Decimal("0"), qty_dec - filled_qty)
    if filled and filled_qty <= 0:
        filled = False
        reason = "partial_fill_zero"
    status = "filled" if filled and remaining_qty == 0 else "partial" if filled else "open"
    notional = filled_qty * fill_price if filled else Decimal("0")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "order_id": order_id({"symbol": symbol, "side": side_up, "type": order_type, "qty": str(qty_dec), "price": str(requested), "ts": candle.get("ts")}),
        "ts": candle.get("ts") or utc_now(),
        "symbol": symbol.upper(),
        "side": side_up,
        "order_type": order_type,
        "status": status,
        "reason": reason,
        "qty": dstr(qty_dec),
        "filled_qty": dstr(filled_qty),
        "remaining_qty": dstr(remaining_qty),
        "requested_price": dstr(requested),
        "fill_price": dstr(fill_price) if filled else None,
        "fee": dstr(notional * fee_rate),
        "notional": dstr(notional),
    }
    if append_order:
        append_jsonl(PAPER_ORDERS, payload)
    return payload


def liquidation_price(entry: Decimal, side: str, leverage: Any, quote_volume: Any = None) -> Decimal:
    lev = max(Decimal("1"), dec(leverage, "1"))
    # Phase 2: conservative tiered maintenance margin (not flat 0.5%).
    mmr = mmr_for(liquidity_tier(quote_volume)) if quote_volume is not None else MAINTENANCE_MARGIN_RATE
    move = (Decimal("1") / lev) - mmr
    if side.upper() == "LONG":
        return entry * (Decimal("1") - move)
    return entry * (Decimal("1") + move)


def simulate_exit(
    side: str,
    entry: Any,
    qty: Any,
    sl: Any,
    tp: Any,
    candles: list[dict[str, Any]],
    leverage: Any = "1",
    quote_volume: Any = None,
) -> dict[str, Any]:
    side_up = side.upper()
    entry_dec = dec(entry)
    qty_dec = dec(qty)
    sl_dec = dec(sl)
    tp_dec = dec(tp)
    # Phase 2: pessimistic tiered costs. SL is a stop-market (worse slippage);
    # TP is treated as a market exit at the level; liquidation now also slips.
    # Missing quote_volume -> "micro" (most expensive): unknown liquidity is
    # treated pessimistically, never given the cheap "major" tier by default.
    tier = liquidity_tier(quote_volume)
    sl_bps = fill_bps(tier, is_stop=True)
    mkt_bps = fill_bps(tier)
    liq = liquidation_price(entry_dec, side_up, leverage, quote_volume=quote_volume)
    for candle in candles:
        open_price = dec(candle.get("open"))
        high = dec(candle.get("high"))
        low = dec(candle.get("low"))
        if side_up == "LONG":
            if low <= liq:
                close = min(open_price, liq) if open_price < entry_dec else liq
                close = exit_slippage(close, side_up, mkt_bps)
                reason = "liquidation"
            elif low <= sl_dec:
                close = open_price if open_price < sl_dec else sl_dec
                close = exit_slippage(close, side_up, sl_bps)
                reason = "sl"
            elif high >= tp_dec:
                close = exit_slippage(tp_dec, side_up, mkt_bps)
                reason = "tp"
            else:
                continue
            gross = (close - entry_dec) * qty_dec
        else:
            if high >= liq:
                close = max(open_price, liq) if open_price > entry_dec else liq
                close = exit_slippage(close, side_up, mkt_bps)
                reason = "liquidation"
            elif high >= sl_dec:
                close = open_price if open_price > sl_dec else sl_dec
                close = exit_slippage(close, side_up, sl_bps)
                reason = "sl"
            elif low <= tp_dec:
                close = exit_slippage(tp_dec, side_up, mkt_bps)
                reason = "tp"
            else:
                continue
            gross = (entry_dec - close) * qty_dec
        fee = abs(close * qty_dec) * TAKER_FEE_RATE
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "closed",
            "reason": reason,
            "close_ts": candle.get("ts") or utc_now(),
            "exit": dstr(close),
            "gross": dstr(gross),
            "fee": dstr(fee),
            "net": dstr(gross - fee),
            "liquidation_price": dstr(liq),
            "liquidity_tier": tier,
            "slippage_bps_applied": dstr(sl_bps if reason == "sl" else mkt_bps),
            "promotion_blocked": reason == "liquidation",
        }
    return {"schema_version": SCHEMA_VERSION, "status": "open", "reason": "unresolved", "liquidation_price": dstr(liq), "promotion_blocked": False}


def apply_funding_payment(account: dict[str, Any], notional: Any, funding_rate: Any, side: str) -> dict[str, Any]:
    payment = dec(notional) * dec(funding_rate)
    if side.upper() == "LONG":
        payment = -payment
    equity = dec(account.get("equity"), "100") + payment
    result = {**account, "equity": dstr(equity), "last_funding_payment": dstr(payment), "updated_at": utc_now()}
    write_json_atomic(PAPER_POSITIONS, result)
    return result


def simulate_round_trip(trade: dict[str, Any], candles: list[dict[str, Any]], append_order: bool = True) -> dict[str, Any]:
    if not candles:
        return {"status": "skipped", "reason": "missing_candles"}
    # Phase 2 lockstep: tier off the trade's 24h quote volume (fall back to
    # candle quote_volume, never base volume). Missing -> None -> micro (pessimistic).
    quote_volume = trade.get("quote_volume") or candles[0].get("quote_volume")
    entry = simulate_entry_order(trade["symbol"], trade["side"], trade.get("order_type", "market"), trade["qty"], trade["entry"], candles[0], append_order=append_order, quote_volume=quote_volume)
    if entry["status"] not in {"filled", "partial"}:
        return {**entry, "trade_status": "not_opened"}
    exit_row = simulate_exit(trade["side"], entry["fill_price"], entry.get("filled_qty") or trade["qty"], trade["sl"], trade["tp"], candles[1:] or candles, trade.get("leverage", "1"), quote_volume=quote_volume)
    return {"entry_order": entry, "exit": exit_row, "trade_status": exit_row.get("status")}
