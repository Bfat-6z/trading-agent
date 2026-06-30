"""Deterministic paper capital and risk manager.

The manager simulates a 100 USDT learning account by default. It can approve or
reject paper orders, but it never places live orders and never imports exchange
execution code.
"""
from __future__ import annotations

import argparse
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION, validate_contract
from atomic_state import append_jsonl, read_json, write_json_atomic
from runtime_config import evaluate_mode, load_runtime_config
from timebase import seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
ACCOUNT_PATH = STATE_DIR / "paper_account.json"
RISK_STATE_PATH = MEMORY_DIR / "paper_risk_state.json"
POSITION_HISTORY_PATH = MEMORY_DIR / "paper_position_history.jsonl"

DEFAULT_EQUITY = Decimal("100")
DEFAULT_MAX_MARGIN_FRACTION = Decimal("0.45")
DEFAULT_MAX_RISK_FRACTION = Decimal("0.05")
DEFAULT_MAX_LEVERAGE = Decimal("50")
EXCHANGE_SANITY_MAX_LEVERAGE = Decimal("125")
DEFAULT_TAKER_FEE_RATE = Decimal("0.0005")
MAINTENANCE_MARGIN_RATE = Decimal("0.005")


def dec(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def dec_str(value: Decimal, places: str = "0.00000001") -> str:
    normalized = dec(value).quantize(Decimal(places), rounding=ROUND_DOWN).normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")


def position_unrealized_pnl(position: dict[str, Any]) -> Decimal:
    side = str(position.get("side") or "").upper()
    entry = dec(position.get("entry"))
    qty = dec(position.get("qty"))
    candles = position.get("replay_candles") if isinstance(position.get("replay_candles"), list) else []
    mark = entry
    if candles and isinstance(candles[-1], dict):
        mark = dec(candles[-1].get("close"), str(entry))
    if entry <= 0 or qty <= 0 or mark <= 0:
        return Decimal("0")
    if side == "LONG":
        return (mark - entry) * qty
    if side == "SHORT":
        return (entry - mark) * qty
    return Decimal("0")

def normalize_account(account: dict[str, Any], original: dict[str, Any] | None = None) -> dict[str, Any]:
    original = original or account
    starting = dec(account.get("starting_equity"), "100")
    positions = [row for row in account.get("open_positions", []) if isinstance(row, dict)] if isinstance(account.get("open_positions"), list) else []
    open_margin = sum(dec(row.get("margin")) for row in positions)
    unrealized = sum(position_unrealized_pnl(row) for row in positions)
    if original.get("cash") not in (None, ""):
        cash = dec(account.get("cash"), str(starting - open_margin))
    elif original.get("equity") not in (None, ""):
        cash = dec(original.get("equity")) - open_margin - unrealized
    elif original.get("realized_pnl") not in (None, ""):
        cash = starting + dec(account.get("realized_pnl")) - open_margin
    else:
        cash = starting - open_margin
    realized = dec(account.get("realized_pnl")) if original.get("realized_pnl") not in (None, "") else cash + open_margin - starting
    equity = cash + open_margin + unrealized
    return {
        **account,
        "starting_equity": dec_str(starting),
        "cash": dec_str(cash),
        "equity": dec_str(equity),
        "realized_pnl": dec_str(realized),
        "open_margin": dec_str(max(Decimal("0"), open_margin)),
        "unrealized_pnl": dec_str(unrealized),
        "open_positions": positions,
    }

def default_account(equity: Decimal = DEFAULT_EQUITY) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "paper",
        "currency": "USDT",
        "created_at": now,
        "starting_equity": dec_str(equity),
        "equity": dec_str(equity),
        "cash": dec_str(equity),
        "realized_pnl": "0",
        "fees_paid": "0",
        "closed_trades": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "trial_days": 0,
        "open_margin": "0",
        "unrealized_pnl": "0",
        "open_positions": [],
        "updated_at": now,
    }


def load_account(path: Path = ACCOUNT_PATH) -> dict[str, Any]:
    payload = read_json(path, default={})
    if not isinstance(payload, dict) or not payload:
        return default_account()
    merged = {**default_account(dec(payload.get("starting_equity"), "100")), **payload}
    return normalize_account(merged, original=payload)


def save_account(account: dict[str, Any], path: Path = ACCOUNT_PATH) -> dict[str, Any]:
    account = normalize_account({**account, "updated_at": utc_now()}, original=account)
    write_json_atomic(path, account)
    return account


def risk_decision_id(payload: dict[str, Any]) -> str:
    raw = str(sorted(payload.items()))
    return "risk_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def side_risk_distance(side: str, entry: Decimal, sl: Decimal) -> Decimal:
    if entry <= 0 or sl <= 0:
        return Decimal("0")
    if side == "LONG":
        return max(Decimal("0"), (entry - sl) / entry)
    if side == "SHORT":
        return max(Decimal("0"), (sl - entry) / entry)
    return Decimal("0")

def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

def estimate_liquidation_price(entry: Decimal, side: str, leverage: Decimal) -> Decimal:
    lev = max(Decimal("1"), leverage)
    move = (Decimal("1") / lev) - MAINTENANCE_MARGIN_RATE
    if str(side or "").upper() == "LONG":
        return entry * (Decimal("1") - move)
    return entry * (Decimal("1") + move)


def choose_requested_margin(equity: Decimal, requested_margin: Any = None) -> Decimal:
    if requested_margin not in (None, ""):
        return dec(requested_margin)
    return equity * Decimal("0.05")


def evaluate_paper_order(
    symbol: str,
    side: str,
    entry: Any,
    sl: Any,
    tp: Any,
    requested_margin: Any = None,
    requested_leverage: Any = None,
    setup_id: str = "unknown",
    account: dict[str, Any] | None = None,
    instrument: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    account = account or load_account()
    config_eval = evaluate_mode(config or load_runtime_config())
    equity = dec(account.get("equity"), "100")
    cash = dec(account.get("cash"), str(equity))
    entry_dec = dec(entry)
    sl_dec = dec(sl)
    tp_dec = dec(tp)
    side_up = str(side or "").upper()
    inst = instrument or {}
    inst_max_lev = dec(inst.get("max_leverage"), str(DEFAULT_MAX_LEVERAGE))
    max_leverage = min(DEFAULT_MAX_LEVERAGE, inst_max_lev, EXCHANGE_SANITY_MAX_LEVERAGE)
    leverage = dec(requested_leverage, "3")
    margin = choose_requested_margin(equity, requested_margin)
    max_margin = equity * DEFAULT_MAX_MARGIN_FRACTION
    risk_distance = side_risk_distance(side_up, entry_dec, sl_dec)
    notional = margin * leverage
    qty = notional / entry_dec if entry_dec > 0 else Decimal("0")
    step_size = dec(inst.get("step_size"), "0")
    if step_size > 0:
        qty = round_down_to_step(qty, step_size)
        notional = qty * entry_dec
        if leverage > 0:
            margin = notional / leverage
    estimated_loss = notional * risk_distance
    max_loss = equity * DEFAULT_MAX_RISK_FRACTION
    errors: list[str] = []
    warnings: list[str] = []

    if config_eval.get("feature_flags", {}).get("live_orders") or config_eval.get("live_execution_enabled"):
        errors.append("live_orders_enabled_in_config")
    if not config_eval.get("feature_flags", {}).get("paper_trading", True):
        errors.append("paper_trading_disabled")
    if side_up not in {"LONG", "SHORT"}:
        errors.append("invalid_side")
    if entry_dec <= 0 or sl_dec <= 0 or tp_dec <= 0:
        errors.append("invalid_prices")
    if risk_distance <= 0:
        errors.append("invalid_stop_geometry")
    if leverage <= 0:
        errors.append("invalid_leverage")
    if leverage > max_leverage:
        errors.append("requested_leverage_above_cap")
    if margin <= 0:
        errors.append("invalid_margin")
    if margin > max_margin:
        errors.append("requested_margin_above_cap")
    if margin > cash:
        errors.append("insufficient_paper_cash")
    if estimated_loss > max_loss:
        errors.append("estimated_loss_above_risk_cap")
    min_notional = dec(inst.get("min_notional"), "0")
    if step_size > 0 and qty <= 0:
        errors.append("quantity_zero_after_step_rounding")
    if min_notional > 0 and notional < min_notional:
        errors.append("notional_below_exchange_minimum")
    if inst.get("status") and str(inst.get("status")).lower() not in {"trading", "paper_allowed"}:
        errors.append("instrument_not_trading")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "risk_decision_id": risk_decision_id(
            {
                "symbol": symbol,
                "side": side_up,
                "entry": str(entry_dec),
                "sl": str(sl_dec),
                "tp": str(tp_dec),
                "margin": str(margin),
                "leverage": str(leverage),
                "setup_id": setup_id,
            }
        ),
        "evaluated_at": utc_now(),
        "mode": "paper",
        "symbol": str(symbol or "").upper(),
        "side": side_up,
        "setup_id": str(setup_id or "unknown"),
        "can_place_live_orders": False,
        "can_open_paper": not errors,
        "reason": "ok" if not errors else ";".join(errors),
        "errors": errors,
        "warnings": warnings,
        "account_equity": dec_str(equity),
        "account_cash": dec_str(cash),
        "entry": dec_str(entry_dec),
        "sl": dec_str(sl_dec),
        "tp": dec_str(tp_dec),
        "margin": dec_str(margin),
        "leverage": dec_str(leverage, "0.01"),
        "notional": dec_str(notional),
        "qty": dec_str(qty),
        "fee_to_close_reserve": dec_str(abs(notional) * DEFAULT_TAKER_FEE_RATE),
        "estimated_loss": dec_str(estimated_loss),
        "max_loss": dec_str(max_loss),
        "risk_distance": dec_str(risk_distance),
        "instrument_snapshot_id": inst.get("instrument_snapshot_id"),
        "canonical_instrument_id": inst.get("canonical_instrument_id"),
        "price_basis": inst.get("price_basis_contract") or {"fills": "BOOK_MID/LAST+slippage"},
    }
    contract = validate_contract("risk_decision", payload)
    if not contract.ok:
        payload["can_open_paper"] = False
        payload["errors"] = payload["errors"] + contract.errors
        payload["reason"] = ";".join(payload["errors"])
    write_json_atomic(RISK_STATE_PATH, payload)
    return payload


def apply_paper_close(account: dict[str, Any], net_pnl: Any, fee: Any = "0") -> dict[str, Any]:
    pnl = dec(net_pnl)
    fee_dec = dec(fee)
    equity = dec(account.get("equity"), "100") + pnl
    cash = dec(account.get("cash"), str(equity)) + pnl
    realized = dec(account.get("realized_pnl")) + pnl
    fees_paid = dec(account.get("fees_paid")) + fee_dec
    return {
        **account,
        "equity": dec_str(equity),
        "cash": dec_str(cash),
        "realized_pnl": dec_str(realized),
        "fees_paid": dec_str(fees_paid),
        "updated_at": utc_now(),
    }

def open_paper_position(risk_decision: dict[str, Any], account: dict[str, Any] | None = None, path: Path = ACCOUNT_PATH, entry_fee: Any = "0") -> dict[str, Any]:
    account = account or load_account(path)
    if not risk_decision.get("can_open_paper"):
        return {"ok": False, "reason": "risk_decision_rejected", "account": account, "can_place_live_orders": False}
    margin = dec(risk_decision.get("margin"))
    entry_fee_dec = dec(entry_fee)
    cash = dec(account.get("cash"), "100")
    if margin <= 0 or margin + entry_fee_dec > cash:
        return {"ok": False, "reason": "insufficient_paper_cash", "account": account, "can_place_live_orders": False}
    position_id = "paper_pos_" + hashlib.sha256(f"{risk_decision.get('risk_decision_id')}:{utc_now()}".encode("utf-8")).hexdigest()[:20]
    position = {
        "schema_version": SCHEMA_VERSION,
        "position_id": position_id,
        "opened_at": utc_now(),
        "status": "open",
        "symbol": risk_decision.get("symbol"),
        "side": risk_decision.get("side"),
        "setup_id": risk_decision.get("setup_id"),
        "entry": risk_decision.get("entry"),
        "sl": risk_decision.get("sl"),
        "tp": risk_decision.get("tp"),
        "qty": risk_decision.get("qty"),
        "margin": risk_decision.get("margin"),
        "leverage": risk_decision.get("leverage"),
        "notional": risk_decision.get("notional"),
        "entry_fee": dec_str(entry_fee_dec),
        "entry_fee_paid_at_open": True,
        "fee_to_close_reserve": risk_decision.get("fee_to_close_reserve") or dec_str(dec(risk_decision.get("notional")) * DEFAULT_TAKER_FEE_RATE),
        "liquidation_price": dec_str(estimate_liquidation_price(dec(risk_decision.get("entry")), str(risk_decision.get("side") or ""), dec(risk_decision.get("leverage"), "1"))),
        "maintenance_margin_rate": dec_str(MAINTENANCE_MARGIN_RATE),
        "execution_assumptions": {
            "venue": "binance_usdm_paper",
            "fee_model": "maker_taker_v1",
            "entry_fee_paid_at_open": True,
            "fee_to_close_reserved": True,
            "liquidation_model": "isolated_maintenance_margin_v1",
            "live_execution": False,
        },
        "risk_decision_id": risk_decision.get("risk_decision_id"),
        "can_place_live_orders": False,
    }
    positions = [row for row in account.get("open_positions", []) if isinstance(row, dict)]
    updated = {
        **account,
        "cash": dec_str(cash - margin - entry_fee_dec),
        "fees_paid": dec_str(dec(account.get("fees_paid")) + entry_fee_dec),
        "open_margin": dec_str(dec(account.get("open_margin")) + margin),
        "open_positions": positions + [position],
        "updated_at": utc_now(),
    }
    updated = save_account(updated, path)
    append_jsonl(POSITION_HISTORY_PATH, {"event": "paper_position_open", **position})
    return {"ok": True, "position": position, "account": updated, "can_place_live_orders": False}

def close_paper_position(position_id: str, exit_price: Any, fee: Any = "0", reason: str = "manual_sim_close", account: dict[str, Any] | None = None, path: Path = ACCOUNT_PATH, funding_payment: Any = "0") -> dict[str, Any]:
    account = account or load_account(path)
    positions = [row for row in account.get("open_positions", []) if isinstance(row, dict)]
    position = next((row for row in positions if row.get("position_id") == position_id), None)
    if not position:
        return {"ok": False, "reason": "position_not_found", "account": account, "can_place_live_orders": False}
    side = str(position.get("side") or "").upper()
    entry = dec(position.get("entry"))
    exit_dec = dec(exit_price)
    qty = dec(position.get("qty"))
    margin = dec(position.get("margin"))
    entry_fee_dec = dec(position.get("entry_fee"))
    exit_fee_dec = dec(fee)
    funding_dec = dec(funding_payment)
    entry_fee_paid_at_open = bool(position.get("entry_fee_paid_at_open"))
    total_fee_dec = entry_fee_dec + exit_fee_dec
    gross = (exit_dec - entry) * qty if side == "LONG" else (entry - exit_dec) * qty
    net_before_funding = gross - total_fee_dec
    net = net_before_funding + funding_dec
    remaining = [row for row in positions if row.get("position_id") != position_id]
    cash_delta = margin + gross - exit_fee_dec + funding_dec
    if not entry_fee_paid_at_open:
        cash_delta -= entry_fee_dec
    cash = dec(account.get("cash"), "100") + cash_delta
    now = utc_now()
    closed_trades = int(account.get("closed_trades") or account.get("trades") or 0) + 1
    wins = int(account.get("wins") or 0) + (1 if net > 0 else 0)
    losses = int(account.get("losses") or 0) + (1 if net < 0 else 0)
    trial_age = seconds_between(account.get("created_at") or account.get("started_at") or account.get("updated_at"), now)
    trial_days = max(int(account.get("trial_days") or 0), int((trial_age or 0) // 86400))
    closed = {
        **position,
        "status": "closed",
        "closed_at": now,
        "exit": dec_str(exit_dec),
        "gross": dec_str(gross),
        "entry_fee": dec_str(entry_fee_dec),
        "exit_fee": dec_str(exit_fee_dec),
        "fee": dec_str(total_fee_dec),
        "fees": dec_str(total_fee_dec),
        "funding_payment": dec_str(funding_dec),
        "net_before_funding": dec_str(net_before_funding),
        "net": dec_str(net),
        "reason": reason,
        "execution_assumptions": {
            **(position.get("execution_assumptions") if isinstance(position.get("execution_assumptions"), dict) else {}),
            "entry_fee_paid_at_open": entry_fee_paid_at_open,
            "exit_fee_paid_on_close": True,
            "funding_applied_on_close": funding_dec != 0,
        },
    }
    updated = {
        **account,
        "cash": dec_str(cash),
        "realized_pnl": dec_str(dec(account.get("realized_pnl")) + net),
        "fees_paid": dec_str(dec(account.get("fees_paid")) + (exit_fee_dec if entry_fee_paid_at_open else total_fee_dec)),
        "closed_trades": closed_trades,
        "trades": closed_trades,
        "wins": wins,
        "losses": losses,
        "trial_days": trial_days,
        "open_margin": dec_str(sum(dec(row.get("margin")) for row in remaining)),
        "open_positions": remaining,
        "updated_at": now,
    }
    updated = save_account(updated, path)
    append_jsonl(POSITION_HISTORY_PATH, {"event": "paper_position_close", **closed})
    return {"ok": True, "position": closed, "account": updated, "can_place_live_orders": False}


def initialize_account(path: Path = ACCOUNT_PATH, equity: Decimal = DEFAULT_EQUITY) -> dict[str, Any]:
    account = default_account(equity)
    write_json_atomic(path, account)
    return account


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage deterministic paper account risk")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--equity", default="100")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.init:
        print(initialize_account(equity=dec(args.equity, "100")))
    else:
        print(load_account())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
