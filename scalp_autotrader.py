"""24/7 futures scalp scanner/autotrader.

Default mode is PAPER. Live trading requires both --live and
--i-understand-risk. The goal is to collect evidence first, then trade only
when a small mechanical edge is visible after fees/slippage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Iterable

import requests
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

from event_store import safe_append_event
from inner_critic import evaluate_signal
from shadow_trade_logger import SHADOW_JSONL, append_shadow, build_shadow_open
from tradingagents.binance.client import spot_client


load_dotenv()


DEFAULT_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "HYPEUSDT",
    "DOGEUSDT",
    "XRPUSDT",
    "1000PEPEUSDT",
    "FETUSDT",
    "WLDUSDT",
    "SUIUSDT",
    "ENAUSDT",
    "AAVEUSDT",
    "PUMPUSDT",
    "WIFUSDT",
    "PENGUUSDT",
    "ARBUSDT",
    "FILUSDT",
    "LTCUSDT",
    "DOTUSDT",
]

EXCLUDE_SYMBOLS = {
    # Leftover reduce-only algo orders have existed on these symbols in this
    # workspace; avoid re-entry until manually cleaned.
    "XPLUSDT",
    "LINKUSDT",
}


@dataclass
class Signal:
    symbol: str
    side: str
    score: int
    long_score: int
    short_score: int
    price: float
    quote_volume_m: float
    spread_pct: float
    change_3m_pct: float
    change_5m_pct: float
    change_10m_pct: float
    volume_ratio_1m: float
    rsi_1m: float
    taker_flow_last: float
    taker_flow_avg: float
    reasons: list[str]


@dataclass
class PaperPosition:
    symbol: str
    side: str
    qty: Decimal
    margin_usdt: Decimal
    leverage: int
    notional: Decimal
    entry: Decimal
    stop: Decimal
    take_profit: Decimal
    opened_at: float
    signal: Signal

@dataclass
class OrderPlan:
    margin_usdt: Decimal
    leverage: int
    notional: Decimal
    confidence: float
    entry_type: str
    reasons: list[str]

@dataclass
class PaperPerformance:
    trades: int
    wins: int
    losses: int
    net: float
    win_rate: float


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_jsonl(path: Path, event: str, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": utc_now(), "event": event, **payload}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
    safe_append_event("scalp_autotrader", event, payload, ts=row["ts"])


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return values[-1]
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for value in values[period:]:
        e = value * k + e * (1 - k)
    return e


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 99.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def score_signal(
    symbol: str,
    price: float,
    quote_volume_m: float,
    spread_pct: float,
    change_3m_pct: float,
    change_5m_pct: float,
    change_10m_pct: float,
    volume_ratio_1m: float,
    rsi_1m: float,
    taker_flow_last: float,
    taker_flow_avg: float,
    ema9: float,
    ema21: float,
) -> Signal | None:
    long_reasons: list[str] = []
    short_reasons: list[str] = []

    def add(ok: bool, bucket: list[str], reason: str) -> int:
        if ok:
            bucket.append(reason)
            return 1
        return 0

    long_score = 0
    long_score += add(spread_pct <= 0.08, long_reasons, "tight_spread")
    long_score += add(volume_ratio_1m >= 0.55, long_reasons, "active_1m_volume")
    long_score += add(change_3m_pct >= 0.08, long_reasons, "3m_up")
    long_score += add(change_5m_pct >= 0.12, long_reasons, "5m_up")
    long_score += add(price > ema9 > ema21, long_reasons, "ema_stack_up")
    long_score += add(42 <= rsi_1m <= 76, long_reasons, "rsi_long_ok")
    long_score += add(taker_flow_last >= 0.9 or taker_flow_avg >= 1.05, long_reasons, "buyer_flow")

    short_score = 0
    short_score += add(spread_pct <= 0.08, short_reasons, "tight_spread")
    short_score += add(volume_ratio_1m >= 0.55, short_reasons, "active_1m_volume")
    short_score += add(change_3m_pct <= -0.08, short_reasons, "3m_down")
    short_score += add(change_5m_pct <= -0.12, short_reasons, "5m_down")
    short_score += add(price < ema9 < ema21, short_reasons, "ema_stack_down")
    short_score += add(24 <= rsi_1m <= 58, short_reasons, "rsi_short_ok")
    short_score += add(taker_flow_last <= 1.1 or taker_flow_avg <= 0.95, short_reasons, "seller_flow")

    if long_score >= 6 and long_score >= short_score + 2:
        return Signal(
            symbol=symbol,
            side="LONG",
            score=long_score,
            long_score=long_score,
            short_score=short_score,
            price=price,
            quote_volume_m=quote_volume_m,
            spread_pct=spread_pct,
            change_3m_pct=change_3m_pct,
            change_5m_pct=change_5m_pct,
            change_10m_pct=change_10m_pct,
            volume_ratio_1m=volume_ratio_1m,
            rsi_1m=rsi_1m,
            taker_flow_last=taker_flow_last,
            taker_flow_avg=taker_flow_avg,
            reasons=long_reasons,
        )
    if short_score >= 6 and short_score >= long_score + 2:
        return Signal(
            symbol=symbol,
            side="SHORT",
            score=short_score,
            long_score=long_score,
            short_score=short_score,
            price=price,
            quote_volume_m=quote_volume_m,
            spread_pct=spread_pct,
            change_3m_pct=change_3m_pct,
            change_5m_pct=change_5m_pct,
            change_10m_pct=change_10m_pct,
            volume_ratio_1m=volume_ratio_1m,
            rsi_1m=rsi_1m,
            taker_flow_last=taker_flow_last,
            taker_flow_avg=taker_flow_avg,
            reasons=short_reasons,
        )
    return None


class ScalpAutoTrader:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.client = spot_client()
        self.state_dir = Path(args.state_dir)
        self.log_path = self.state_dir / "scalp_autotrader.jsonl"
        self.paper_account_path = self.state_dir / "paper_account.json"
        self.paper_position: PaperPosition | None = None
        self.paper_equity = self.load_or_init_paper_account(reset=bool(args.reset_paper_account))
        self.consecutive_losses = 0
        self.realized_today = Decimal("0")
        self.live_session_started_at = self.utc_day_start_ms()
        self.last_trade_closed_at = 0.0
        self.filters_cache: dict[str, tuple[Decimal, Decimal]] = {}
        self.memory_bias_path = Path(args.memory_bias_path)
        self.last_risk_block_reason: str | None = None
        self.last_risk_log_at: dict[str, float] = {}
        self.last_shadow_log_at: dict[str, float] = {}
        self.last_live_protection_log_at: dict[str, float] = {}
        self.trades_by_bias_update: dict[str, int] = {}

    def load_or_init_paper_account(self, reset: bool = False) -> Decimal:
        default_equity = Decimal(str(self.args.paper_equity))
        if self.args.live:
            return default_equity
        if not reset and self.paper_account_path.exists():
            try:
                payload = json.loads(self.paper_account_path.read_text(encoding="utf-8", errors="ignore"))
                equity = Decimal(str(payload.get("equity", default_equity)))
                if equity > 0:
                    return equity
            except Exception as exc:
                self.log("paper_account_load_error", {"error": str(exc)[:180], "path": str(self.paper_account_path)})
        self.write_paper_account(default_equity, reason="reset" if reset else "init")
        return default_equity

    def write_paper_account(self, equity: Decimal, reason: str) -> None:
        if self.args.live:
            return
        payload = {
            "updated_at": utc_now(),
            "mode": "paper",
            "starting_equity": str(Decimal(str(self.args.paper_equity))),
            "equity": str(equity),
            "currency": "USDT",
            "reason": reason,
        }
        self.paper_account_path.parent.mkdir(parents=True, exist_ok=True)
        self.paper_account_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def log(self, event: str, payload: dict) -> None:
        append_jsonl(self.log_path, event, payload)

    @staticmethod
    def utc_day_start_ms() -> int:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1000)

    def log_risk_block(self, reason: str, payload: dict) -> None:
        self.last_risk_block_reason = reason
        now = time.time()
        last = self.last_risk_log_at.get(reason, 0.0)
        if now - last >= self.args.risk_log_interval_seconds:
            self.last_risk_log_at[reason] = now
            self.log("risk_block", {"reason": reason, **payload})

    def memory_bias(self) -> dict:
        if not self.memory_bias_path.exists():
            return {}
        try:
            return json.loads(self.memory_bias_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.log("memory_bias_error", {"error": str(exc)[:180], "path": str(self.memory_bias_path)})
            return {}

    def memory_sleep_active(self, bias: dict) -> tuple[bool, str | None]:
        sleep_until = bias.get("sleep_until")
        if not sleep_until:
            return False, None
        try:
            target = datetime.fromisoformat(str(sleep_until).replace("Z", "+00:00"))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
        except Exception:
            return False, None
        if datetime.now(timezone.utc) < target:
            return True, target.isoformat(timespec="seconds")
        return False, None

    def critic_bias_for_paper_sample(self, bias: dict) -> dict:
        if getattr(self.args, "live", False) or not getattr(self.args, "paper_trade_through_memory_sleep", False):
            return bias
        sleeping, sleep_until = self.memory_sleep_active(bias)
        if not sleeping:
            return bias
        critic_bias = dict(bias)
        critic_bias.pop("sleep_until", None)
        self.log(
            "paper_inner_critic_memory_sleep_bypass",
            {"sleep_until": sleep_until, "bias_updated_at": bias.get("updated_at"), "reason": "paper_sample_collection"},
        )
        return critic_bias

    def bias_key(self, bias: dict) -> str:
        return str(bias.get("updated_at") or "no_bias")

    def record_trade_against_bias(self, symbol: str) -> None:
        bias = self.memory_bias()
        key = self.bias_key(bias)
        self.trades_by_bias_update[key] = self.trades_by_bias_update.get(key, 0) + 1
        self.log("memory_bias_trade_count", {"bias_updated_at": key, "count": self.trades_by_bias_update[key], "symbol": symbol})

    def paper_performance(self, max_lines: int | None = None) -> PaperPerformance:
        if not self.log_path.exists():
            return PaperPerformance(trades=0, wins=0, losses=0, net=0.0, win_rate=0.0)
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception as exc:
            self.log("paper_performance_error", {"error": str(exc)[:180]})
            return PaperPerformance(trades=0, wins=0, losses=0, net=0.0, win_rate=0.0)
        if max_lines is None:
            max_lines = max(200, self.args.paper_performance_window * 8)
        closes: list[dict] = []
        for line in reversed(lines[-max_lines:]):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("event") == "paper_close":
                closes.append(row)
            if len(closes) >= self.args.paper_performance_window:
                break
        closes.reverse()
        wins = 0
        losses = 0
        net = 0.0
        for row in closes:
            value = float(row.get("net", 0) or 0)
            net += value
            if value > 0:
                wins += 1
            elif value < 0:
                losses += 1
        trades = len(closes)
        return PaperPerformance(trades=trades, wins=wins, losses=losses, net=net, win_rate=(wins / trades if trades else 0.0))

    def live_performance_gate_allows(self) -> bool:
        perf = self.paper_performance()
        if self.args.allow_live_without_paper_edge:
            self.log("live_performance_gate_override", {"performance": asdict(perf)})
            return True
        if perf.trades < self.args.live_min_paper_trades:
            self.log_risk_block(
                "live_paper_sample_too_small",
                {"performance": asdict(perf), "required_trades": self.args.live_min_paper_trades},
            )
            return False
        if perf.win_rate < self.args.live_required_win_rate:
            self.log_risk_block(
                "live_win_rate_below_gate",
                {"performance": asdict(perf), "required_win_rate": self.args.live_required_win_rate},
            )
            return False
        if perf.net < self.args.live_required_net_usdt:
            self.log_risk_block(
                "live_net_below_gate",
                {"performance": asdict(perf), "required_net": self.args.live_required_net_usdt},
            )
            return False
        return True

    def symbols(self) -> list[str]:
        raw = self.args.symbols or DEFAULT_SYMBOLS
        return [s.upper() for s in raw if s.upper() not in EXCLUDE_SYMBOLS]

    def order_plan(self, signal: Signal) -> OrderPlan:
        bias = self.memory_bias()
        min_margin = Decimal(str(min(self.args.min_margin_usdt, self.args.margin_usdt)))
        max_margin = Decimal(str(self.args.margin_usdt))
        min_leverage = max(1, min(int(self.args.min_leverage), int(self.args.leverage)))
        max_leverage = max(min_leverage, int(self.args.leverage))

        score_edge = clamp((signal.score - 6) / 3, 0.0, 1.0)
        volume_edge = clamp((signal.quote_volume_m - self.args.min_quote_volume_m) / 500, 0.0, 1.0)
        spread_edge = clamp(1.0 - signal.spread_pct / 0.08, 0.0, 1.0)
        move_edge = clamp(abs(signal.change_5m_pct) / 0.8, 0.0, 1.0)
        confidence = round(0.45 * score_edge + 0.2 * volume_edge + 0.2 * spread_edge + 0.15 * move_edge, 4)

        posture = str(bias.get("risk_posture") or "normal").lower()
        market_learning = bias.get("market_learning") if isinstance(bias.get("market_learning"), dict) else {}
        tags = {str(tag) for tag in market_learning.get("tags", [])}
        risk_multiplier = 1.0
        reasons = [f"confidence={confidence:.2f}", f"score={signal.score}", f"spread={signal.spread_pct:.4f}%"]
        if posture == "defensive":
            risk_multiplier *= 0.55
            reasons.append("defensive_bias")
        if "crowded_funding" in tags or "alt_mania" in tags:
            risk_multiplier *= 0.70
            reasons.append("crowded_or_alt_mania")
        if signal.volume_ratio_1m >= 1.2:
            risk_multiplier *= 1.10
            reasons.append("volume_confirmed")
        if signal.spread_pct > 0.04:
            risk_multiplier *= 0.75
            reasons.append("spread_penalty")

        margin_span = max_margin - min_margin
        margin = min_margin + (margin_span * Decimal(str(clamp(confidence * risk_multiplier, 0.0, 1.0))))
        leverage_float = min_leverage + (max_leverage - min_leverage) * clamp(confidence * risk_multiplier, 0.0, 1.0)
        leverage = max(min_leverage, min(max_leverage, int(round(leverage_float))))
        margin = margin.quantize(Decimal("0.0001"))
        notional = margin * Decimal(str(leverage))
        entry_type = "MARKET_NOW" if confidence >= 0.55 and signal.spread_pct <= 0.05 else "MARKET_SMALL"
        return OrderPlan(
            margin_usdt=margin,
            leverage=leverage,
            notional=notional.quantize(Decimal("0.0001")),
            confidence=confidence,
            entry_type=entry_type,
            reasons=reasons,
        )

    @staticmethod
    def order_plan_payload(plan: OrderPlan) -> dict:
        return {
            "margin_usdt": str(plan.margin_usdt),
            "leverage": plan.leverage,
            "notional": str(plan.notional),
            "confidence": plan.confidence,
            "entry_type": plan.entry_type,
            "reasons": plan.reasons,
        }

    def paper_plan_with_exchange_min_qty(self, signal: Signal, plan: OrderPlan, entry: Decimal, step: Decimal) -> tuple[OrderPlan | None, Decimal]:
        qty = floor_to_step(plan.notional / entry, step)
        if qty > 0:
            return plan, qty
        required_margin = ((entry * step) / Decimal(str(plan.leverage))).quantize(Decimal("0.0001"), rounding=ROUND_UP)
        if required_margin <= 0:
            self.log("paper_open_block", {"reason": "invalid_min_qty", "symbol": signal.symbol, "step": str(step), "entry": str(entry)})
            return None, Decimal("0")
        if required_margin > self.paper_equity:
            self.log(
                "paper_open_block",
                {
                    "reason": "paper_equity_below_min_qty_margin",
                    "symbol": signal.symbol,
                    "entry": str(entry),
                    "step": str(step),
                    "required_margin_usdt": str(required_margin),
                    "paper_equity": str(self.paper_equity),
                    "order_plan": self.order_plan_payload(plan),
                },
            )
            return None, Decimal("0")
        bumped = OrderPlan(
            margin_usdt=required_margin,
            leverage=plan.leverage,
            notional=(required_margin * Decimal(str(plan.leverage))).quantize(Decimal("0.0001")),
            confidence=plan.confidence,
            entry_type=plan.entry_type,
            reasons=[*plan.reasons, "min_qty_margin_bump"],
        )
        qty = floor_to_step(bumped.notional / entry, step)
        if qty <= 0:
            self.log(
                "paper_open_block",
                {
                    "reason": "paper_qty_still_zero_after_bump",
                    "symbol": signal.symbol,
                    "entry": str(entry),
                    "step": str(step),
                    "order_plan": self.order_plan_payload(bumped),
                },
            )
            return None, Decimal("0")
        self.log(
            "paper_min_qty_margin_bump",
            {
                "symbol": signal.symbol,
                "entry": str(entry),
                "step": str(step),
                "previous_plan": self.order_plan_payload(plan),
                "order_plan": self.order_plan_payload(bumped),
                "qty": str(qty),
            },
        )
        return bumped, qty

    def live_open_positions(self) -> list[dict]:
        positions = self.client.futures_position_information()
        return [p for p in positions if abs(Decimal(p.get("positionAmt", "0"))) > 0]

    def taker_flow(self, symbol: str) -> tuple[float, float]:
        data = requests.get(
            "https://fapi.binance.com/futures/data/takerlongshortRatio",
            params={"symbol": symbol, "period": "5m", "limit": 3},
            timeout=3,
        ).json()
        values = [float(x["buySellRatio"]) for x in data if "buySellRatio" in x]
        if not values:
            return 1.0, 1.0
        return values[-1], sum(values) / len(values)

    def inspect_symbol(self, symbol: str) -> Signal | None:
        ticker = self.client.futures_ticker(symbol=symbol)
        quote_volume_m = float(ticker["quoteVolume"]) / 1_000_000
        if quote_volume_m < self.args.min_quote_volume_m:
            return None

        candles = self.client.futures_klines(symbol=symbol, interval="1m", limit=30)
        closes = [float(x[4]) for x in candles]
        quote_volumes = [float(x[7]) for x in candles]
        if len(closes) < 22:
            return None

        order_book = self.client.futures_order_book(symbol=symbol, limit=5)
        bid = float(order_book["bids"][0][0])
        ask = float(order_book["asks"][0][0])
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100 if mid > 0 else 999
        price = closes[-1]
        change_3m_pct = (closes[-1] / closes[-4] - 1) * 100
        change_5m_pct = (closes[-1] / closes[-6] - 1) * 100
        change_10m_pct = (closes[-1] / closes[-11] - 1) * 100
        vol_base = sum(quote_volumes[-16:-1]) / 15 if sum(quote_volumes[-16:-1]) > 0 else 1
        volume_ratio_1m = quote_volumes[-1] / vol_base
        rsi_1m = rsi(closes)
        flow_last, flow_avg = self.taker_flow(symbol)
        return score_signal(
            symbol=symbol,
            price=price,
            quote_volume_m=quote_volume_m,
            spread_pct=spread_pct,
            change_3m_pct=change_3m_pct,
            change_5m_pct=change_5m_pct,
            change_10m_pct=change_10m_pct,
            volume_ratio_1m=volume_ratio_1m,
            rsi_1m=rsi_1m,
            taker_flow_last=flow_last,
            taker_flow_avg=flow_avg,
            ema9=ema(closes, 9),
            ema21=ema(closes, 21),
        )

    def scan_once(self, apply_memory_bias: bool = True) -> list[Signal]:
        signals: list[Signal] = []
        for symbol in self.symbols():
            try:
                signal = self.inspect_symbol(symbol)
                if signal:
                    signals.append(signal)
            except Exception as exc:
                self.log("symbol_error", {"symbol": symbol, "error": str(exc)[:180]})
        signals.sort(key=lambda s: (-s.score, -abs(s.change_5m_pct), s.spread_pct, -s.quote_volume_m))
        if not apply_memory_bias:
            return signals
        bias = self.memory_bias()
        blocked_symbols = {str(s).upper() for s in bias.get("blocked_symbols", []) if s}
        blocked_sides = {str(s).upper() for s in bias.get("blocked_sides", []) if s}
        if blocked_symbols or blocked_sides:
            before = len(signals)
            signals = [s for s in signals if s.symbol not in blocked_symbols and s.side not in blocked_sides]
            if before != len(signals):
                self.log(
                    "memory_bias_filter",
                    {"blocked_symbols": sorted(blocked_symbols), "blocked_sides": sorted(blocked_sides), "before": before, "after": len(signals)},
                )
        try:
            min_score = max(1, min(99, int(bias.get("min_signal_score", 6) or 6)))
        except Exception:
            min_score = 6
        if min_score > 6:
            before = len(signals)
            signals = [signal for signal in signals if signal.score >= min_score]
            if before != len(signals):
                self.log("memory_bias_filter", {"min_signal_score": min_score, "before": before, "after": len(signals)})
        return signals

    def log_shadow_trade(self, signal: Signal, block_reason: str, critic: dict | None = None, force: bool = False) -> dict | None:
        key = f"{signal.symbol}:{signal.side}:{block_reason}"
        now = time.time()
        if not force and now - self.last_shadow_log_at.get(key, 0.0) < self.args.shadow_log_interval_seconds:
            return None
        self.last_shadow_log_at[key] = now
        try:
            plan = self.order_plan(signal)
            entry = Decimal(str(signal.price))
            tick, _ = self.symbol_filters(signal.symbol)
            stop, take_profit = self.stops_for(signal, entry, tick)
            shadow = build_shadow_open(
                asdict(signal),
                self.order_plan_payload(plan),
                entry,
                stop,
                take_profit,
                block_reason=block_reason,
                critic=critic,
            )
            append_shadow(SHADOW_JSONL, shadow)
            self.log("shadow_open", shadow)
            return shadow
        except Exception as exc:
            self.log("shadow_error", {"symbol": signal.symbol, "block_reason": block_reason, "error": str(exc)[:180]})
            return None

    def symbol_filters(self, symbol: str) -> tuple[Decimal, Decimal]:
        if symbol in self.filters_cache:
            return self.filters_cache[symbol]
        info = self.client.futures_exchange_info()
        symbol_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
        tick = Decimal(next(f["tickSize"] for f in symbol_info["filters"] if f["filterType"] == "PRICE_FILTER"))
        step = Decimal(next(f["stepSize"] for f in symbol_info["filters"] if f["filterType"] == "LOT_SIZE"))
        self.filters_cache[symbol] = (tick, step)
        return tick, step

    def stops_for(self, signal: Signal, entry: Decimal, tick: Decimal) -> tuple[Decimal, Decimal]:
        tp_pct = Decimal(str(self.args.take_profit_pct)) / Decimal("100")
        sl_pct = Decimal(str(self.args.stop_loss_pct)) / Decimal("100")
        if signal.side == "LONG":
            stop = (entry * (Decimal("1") - sl_pct)).quantize(tick)
            take_profit = (entry * (Decimal("1") + tp_pct)).quantize(tick)
        else:
            stop = (entry * (Decimal("1") + sl_pct)).quantize(tick)
            take_profit = (entry * (Decimal("1") - tp_pct)).quantize(tick)
        return stop, take_profit

    def risk_allows_new_trade(self) -> bool:
        self.last_risk_block_reason = None
        if self.args.live:
            self.refresh_live_realized()
            if not self.live_performance_gate_allows():
                return False
        bias = self.memory_bias()
        if bias.get("allow_new_entries") is False:
            self.log_risk_block("memory_bias", {"bias": bias})
            return False
        sleeping, sleep_until = self.memory_sleep_active(bias)
        if sleeping:
            if not self.args.live and self.args.paper_trade_through_memory_sleep:
                self.log(
                    "paper_memory_sleep_bypass",
                    {"sleep_until": sleep_until, "bias_updated_at": bias.get("updated_at"), "reason": "paper_sample_collection"},
                )
            else:
                self.log_risk_block("memory_sleep", {"sleep_until": sleep_until, "bias_updated_at": bias.get("updated_at")})
                return False
        try:
            max_trades = int(bias.get("max_trades_until_next_reflection", 0) or 0)
        except Exception:
            max_trades = 0
        if max_trades > 0:
            key = self.bias_key(bias)
            used = self.trades_by_bias_update.get(key, 0)
            if used >= max_trades:
                self.log_risk_block("max_trades_until_next_reflection", {"bias_updated_at": key, "used": used, "limit": max_trades})
                return False
        if self.consecutive_losses >= self.args.max_consecutive_losses:
            sleep_elapsed = time.time() - self.last_trade_closed_at if self.last_trade_closed_at else 0.0
            if not self.args.live and sleep_elapsed >= self.args.loss_sleep_seconds:
                self.log(
                    "paper_sleep_reset",
                    {
                        "previous_consecutive_losses": self.consecutive_losses,
                        "sleep_elapsed_seconds": sleep_elapsed,
                        "loss_sleep_seconds": self.args.loss_sleep_seconds,
                    },
                )
                self.consecutive_losses = 0
            else:
                self.log_risk_block("max_consecutive_losses", {"count": self.consecutive_losses})
                return False
        if self.realized_today <= -Decimal(str(self.args.daily_loss_limit_usdt)):
            self.log_risk_block("daily_loss_limit", {"realized": str(self.realized_today), "risk_window_start_ms": self.live_session_started_at})
            return False
        if time.time() - self.last_trade_closed_at < self.args.cooldown_seconds:
            self.last_risk_block_reason = "cooldown"
            return False
        return True

    def refresh_live_realized(self) -> None:
        """Refresh live session PnL from Binance income history.

        Binance stores realized PnL and commission as income rows. Summing from
        process start gives a conservative session circuit breaker even if the
        exact strategy state is lost.
        """
        try:
            pnl_rows = self.client.futures_income_history(
                incomeType="REALIZED_PNL",
                startTime=self.live_session_started_at,
                limit=100,
            )
            commission_rows = self.client.futures_income_history(
                incomeType="COMMISSION",
                startTime=self.live_session_started_at,
                limit=100,
            )
        except Exception as exc:
            self.log("income_refresh_error", {"error": str(exc)[:180]})
            return
        realized = sum(Decimal(str(row.get("income", "0"))) for row in pnl_rows)
        commissions = sum(Decimal(str(row.get("income", "0"))) for row in commission_rows)
        self.realized_today = realized + commissions
        self.log(
            "live_income_refresh",
            {"realized_pnl": str(realized), "commissions": str(commissions), "net": str(self.realized_today)},
        )

    def open_paper(self, signal: Signal) -> None:
        bias = self.memory_bias()
        critic = evaluate_signal(asdict(signal), bias=self.critic_bias_for_paper_sample(bias))
        if critic.get("verdict") == "block":
            self.log("inner_critic_block", {"signal": asdict(signal), "critic": critic})
            self.log_shadow_trade(signal, "inner_critic_block", critic=critic)
            return
        plan = self.order_plan(signal)
        entry = Decimal(str(signal.price))
        _, step = self.symbol_filters(signal.symbol)
        plan, qty = self.paper_plan_with_exchange_min_qty(signal, plan, entry, step)
        if plan is None:
            return
        tick, _ = self.symbol_filters(signal.symbol)
        stop, take_profit = self.stops_for(signal, entry, tick)
        self.paper_position = PaperPosition(
            symbol=signal.symbol,
            side=signal.side,
            qty=qty,
            margin_usdt=plan.margin_usdt,
            leverage=plan.leverage,
            notional=plan.notional,
            entry=entry,
            stop=stop,
            take_profit=take_profit,
            opened_at=time.time(),
            signal=signal,
        )
        self.log("paper_open", {"position": self.paper_position_payload(), "signal": asdict(signal), "order_plan": self.order_plan_payload(plan), "critic": critic})
        self.record_trade_against_bias(signal.symbol)
        print(f"PAPER OPEN {signal.symbol} {signal.side} lev={plan.leverage} margin={plan.margin_usdt} qty={qty} entry={entry} SL={stop} TP={take_profit}", flush=True)

    def paper_position_payload(self) -> dict:
        if not self.paper_position:
            return {}
        p = self.paper_position
        return {
            "symbol": p.symbol,
            "side": p.side,
            "qty": str(p.qty),
            "margin_usdt": str(p.margin_usdt),
            "leverage": p.leverage,
            "notional": str(p.notional),
            "entry": str(p.entry),
            "stop": str(p.stop),
            "take_profit": str(p.take_profit),
            "opened_at": p.opened_at,
        }

    def manage_paper(self) -> None:
        if not self.paper_position:
            return
        p = self.paper_position
        mark = Decimal(self.client.futures_mark_price(symbol=p.symbol)["markPrice"])
        hit_tp = mark >= p.take_profit if p.side == "LONG" else mark <= p.take_profit
        hit_sl = mark <= p.stop if p.side == "LONG" else mark >= p.stop
        if not (hit_tp or hit_sl):
            max_age = self.args.max_hold_seconds
            if max_age > 0 and time.time() - p.opened_at >= max_age:
                hit_sl = True
            else:
                return
        if p.side == "LONG":
            gross = (mark - p.entry) * p.qty
        else:
            gross = (p.entry - mark) * p.qty
        # Taker fee approximation for entry + exit.
        fee_rate = Decimal(str(self.args.fee_rate))
        fees = (p.entry * p.qty + mark * p.qty) * fee_rate
        net = gross - fees
        self.paper_equity += net
        self.write_paper_account(self.paper_equity, reason="paper_close")
        self.realized_today += net
        self.consecutive_losses = self.consecutive_losses + 1 if net < 0 else 0
        self.last_trade_closed_at = time.time()
        self.log(
            "paper_close",
            {
                "symbol": p.symbol,
                "side": p.side,
                "qty": str(p.qty),
                "margin_usdt": str(p.margin_usdt),
                "leverage": p.leverage,
                "notional": str(p.notional),
                "entry": str(p.entry),
                "mark": str(mark),
                "gross": str(gross),
                "fees": str(fees),
                "net": str(net),
                "equity": str(self.paper_equity),
                "reason": "tp" if hit_tp else "sl_or_timeout",
            },
        )
        print(f"PAPER CLOSE {p.symbol} {p.side} mark={mark} net={net:+f} equity={self.paper_equity:f}", flush=True)
        self.paper_position = None

    def open_live(self, signal: Signal) -> None:
        if not self.args.i_understand_risk:
            raise RuntimeError("Live trading requires --i-understand-risk")
        plan = self.order_plan(signal)
        open_positions = self.live_open_positions()
        if open_positions:
            self.log("live_skip", {"reason": "open_position_exists", "positions": open_positions})
            return
        balance = self.futures_balance()
        if balance["available"] < float(plan.margin_usdt):
            self.log("live_skip", {"reason": "insufficient_available", "balance": balance, "order_plan": self.order_plan_payload(plan)})
            return
        try:
            stale_algos = self.client._request_futures_api("get", "openAlgoOrders", True, data={"symbol": signal.symbol})
        except Exception as exc:
            self.log("live_skip", {"reason": "algo_check_failed", "symbol": signal.symbol, "error": str(exc)[:160]})
            return
        if stale_algos:
            self.log(
                "live_skip",
                {
                    "reason": "open_algo_orders_exist",
                    "symbol": signal.symbol,
                    "algo_count": len(stale_algos),
                },
            )
            return

        symbol = signal.symbol
        tick, step = self.symbol_filters(symbol)
        self.set_isolated(symbol)
        self.client.futures_change_leverage(symbol=symbol, leverage=plan.leverage)
        mark = Decimal(self.client.futures_mark_price(symbol=symbol)["markPrice"])
        notional = plan.notional
        qty = floor_to_step(notional / mark, step)
        if qty <= 0:
            raise RuntimeError(f"qty computed as {qty}")

        entry_side = "BUY" if signal.side == "LONG" else "SELL"
        close_side = "SELL" if signal.side == "LONG" else "BUY"
        self.log("live_order_attempt", {"signal": asdict(signal), "order_plan": self.order_plan_payload(plan), "qty": str(qty), "mark": str(mark)})
        self.client.futures_create_order(symbol=symbol, side=entry_side, type="MARKET", quantity=format(qty, "f"))
        time.sleep(1)
        position = next(
            (p for p in self.client.futures_position_information(symbol=symbol) if abs(Decimal(p["positionAmt"])) > 0),
            None,
        )
        if not position:
            raise RuntimeError("No position after live order")
        entry = Decimal(position["entryPrice"])
        qty_real = abs(Decimal(position["positionAmt"]))
        stop, take_profit = self.stops_for(signal, entry, tick)
        try:
            self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="STOP_MARKET",
                stopPrice=format(stop, "f"),
                quantity=format(qty_real, "f"),
                reduceOnly="true",
                workingType="MARK_PRICE",
            )
        except Exception:
            self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="MARKET",
                quantity=format(qty_real, "f"),
                reduceOnly="true",
            )
            raise
        try:
            self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=format(take_profit, "f"),
                quantity=format(qty_real, "f"),
                reduceOnly="true",
                workingType="MARK_PRICE",
            )
        except Exception as exc:
            self.log("live_protection_incomplete", {"symbol": symbol, "missing": "take_profit", "error": str(exc)[:180]})
            if self.args.close_if_tp_placement_fails:
                self.client.futures_create_order(
                    symbol=symbol,
                    side=close_side,
                    type="MARKET",
                    quantity=format(qty_real, "f"),
                    reduceOnly="true",
                )
                raise
        self.log(
            "live_open",
            {
                "symbol": symbol,
                "side": signal.side,
                "qty": str(qty_real),
                "margin_usdt": str(plan.margin_usdt),
                "leverage": plan.leverage,
                "notional": str(plan.notional),
                "entry": str(entry),
                "stop": str(stop),
                "take_profit": str(take_profit),
                "signal": asdict(signal),
                "order_plan": self.order_plan_payload(plan),
            },
        )
        self.record_trade_against_bias(symbol)
        print(f"LIVE OPEN {symbol} {signal.side} lev={plan.leverage} margin={plan.margin_usdt} qty={qty_real} entry={entry} SL={stop} TP={take_profit}", flush=True)

    def live_open_orders(self, symbol: str) -> list[dict]:
        try:
            return self.client.futures_get_open_orders(symbol=symbol)
        except AttributeError:
            return self.client.futures_get_all_orders(symbol=symbol, limit=50)

    @staticmethod
    def order_reduce_only(order: dict) -> bool:
        return str(order.get("reduceOnly", "false")).lower() == "true" or bool(order.get("closePosition"))

    def live_protection_state(self, symbol: str) -> tuple[bool, bool, list[dict]]:
        orders = self.live_open_orders(symbol)
        reduce_orders = [o for o in orders if self.order_reduce_only(o)]
        has_stop = any(str(o.get("type", "")).upper() in {"STOP", "STOP_MARKET"} for o in reduce_orders)
        has_tp = any(str(o.get("type", "")).upper() in {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"} for o in reduce_orders)
        return has_stop, has_tp, reduce_orders

    def emergency_close_live_position(self, position: dict, reason: str) -> None:
        symbol = position["symbol"]
        amt = Decimal(position["positionAmt"])
        if amt == 0:
            return
        close_side = "SELL" if amt > 0 else "BUY"
        qty = abs(amt)
        self.log("live_emergency_close_attempt", {"symbol": symbol, "qty": str(qty), "reason": reason})
        self.client.futures_create_order(symbol=symbol, side=close_side, type="MARKET", quantity=format(qty, "f"), reduceOnly="true")

    def manage_live(self) -> None:
        for position in self.live_open_positions():
            symbol = position["symbol"]
            try:
                has_stop, has_tp, reduce_orders = self.live_protection_state(symbol)
            except Exception as exc:
                self.log_risk_block("live_protection_check_failed", {"symbol": symbol, "error": str(exc)[:180]})
                continue
            now = time.time()
            if now - self.last_live_protection_log_at.get(symbol, 0.0) >= self.args.live_protection_log_interval_seconds:
                self.last_live_protection_log_at[symbol] = now
                self.log(
                    "live_protection_status",
                    {
                        "symbol": symbol,
                        "has_stop": has_stop,
                        "has_take_profit": has_tp,
                        "reduce_order_count": len(reduce_orders),
                        "unrealized_profit": position.get("unRealizedProfit"),
                        "entry_price": position.get("entryPrice"),
                    },
                )
            if not has_stop and self.args.emergency_close_unprotected_live:
                self.emergency_close_live_position(position, "missing_reduce_only_stop")
            elif not has_tp:
                self.log_risk_block("live_missing_take_profit", {"symbol": symbol})

    def set_isolated(self, symbol: str) -> None:
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
        except BinanceAPIException as exc:
            if exc.code != -4046:
                raise

    def futures_balance(self) -> dict:
        for asset in self.client.futures_account_balance():
            if asset.get("asset") == "USDT":
                return {
                    "balance": float(asset.get("balance", 0)),
                    "available": float(asset.get("availableBalance", 0)),
                }
        return {"balance": 0.0, "available": 0.0}

    def tick(self) -> None:
        if self.args.live:
            if self.live_open_positions():
                self.manage_live()
                return
        else:
            self.manage_paper()
            if self.paper_position:
                return
        if not self.risk_allows_new_trade():
            if not self.args.live:
                signals = self.scan_once(apply_memory_bias=False)
                if signals:
                    self.log_shadow_trade(signals[0], self.last_risk_block_reason or "risk_block")
            return
        signals = self.scan_once()
        if not signals:
            self.log("scan", {"signals": 0})
            return
        top = signals[0]
        self.log("signal", {"signal": asdict(top), "all_symbols": [s.symbol for s in signals[:5]]})
        if self.args.live:
            self.open_live(top)
        else:
            self.open_paper(top)

    def run(self) -> None:
        mode = "LIVE" if self.args.live else "PAPER"
        self.log("start", {"mode": mode, "args": vars(self.args), "paper_equity": str(self.paper_equity) if not self.args.live else None})
        if not self.args.live:
            self.log(
                "paper_session_start",
                {
                    "equity": str(self.paper_equity),
                    "starting_equity": str(Decimal(str(self.args.paper_equity))),
                    "account_path": str(self.paper_account_path),
                    "reset": bool(self.args.reset_paper_account),
                },
            )
        print(f"SCALP AUTOTRADER START mode={mode} symbols={len(self.symbols())}", flush=True)
        if self.args.once:
            self.tick()
            return
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                self.log("stop", {"reason": "keyboard_interrupt"})
                raise
            except Exception as exc:
                self.log("error", {"error": str(exc)[:300]})
                print(f"ERR {str(exc)[:160]}", flush=True)
            time.sleep(self.args.interval_seconds)


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper/live Binance futures scalp autotrader")
    parser.add_argument("--live", action="store_true", help="place real Binance futures orders")
    parser.add_argument("--i-understand-risk", action="store_true", help="required with --live")
    parser.add_argument("--once", action="store_true", help="run one scan/manage tick then exit")
    parser.add_argument("--symbols", nargs="*", help="symbols to scan; default is curated liquid futures list")
    parser.add_argument("--state-dir", default=os.environ.get("STATE_DIR", "state"))
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--min-quote-volume-m", type=float, default=20.0)
    parser.add_argument("--margin-usdt", type=float, default=1.0, help="maximum margin cap per trade; adaptive planner may use less")
    parser.add_argument("--min-margin-usdt", type=float, default=0.25, help="minimum simulated/live margin used by adaptive planner")
    parser.add_argument("--leverage", type=int, default=20, help="maximum leverage cap; adaptive planner may use less")
    parser.add_argument("--min-leverage", type=int, default=3, help="minimum leverage used by adaptive planner")
    parser.add_argument("--take-profit-pct", type=float, default=0.5)
    parser.add_argument("--stop-loss-pct", type=float, default=0.35)
    parser.add_argument("--max-hold-seconds", type=float, default=180.0)
    parser.add_argument("--daily-loss-limit-usdt", type=float, default=2.0)
    parser.add_argument("--max-consecutive-losses", type=int, default=2)
    parser.add_argument("--loss-sleep-seconds", type=float, default=21600.0)
    parser.add_argument("--cooldown-seconds", type=float, default=60.0)
    parser.add_argument("--paper-equity", type=float, default=100.0)
    parser.add_argument("--reset-paper-account", action="store_true", help="reset paper account equity to --paper-equity on startup")
    parser.add_argument("--reset-paper-account-only", action="store_true", help="write paper account state and exit without scanning")
    parser.add_argument("--paper-trade-through-memory-sleep", action="store_true", help="allow paper-only sample collection while memory sleep blocks live risk")
    parser.add_argument("--fee-rate", type=float, default=0.0005, help="taker fee approximation per side")
    parser.add_argument("--memory-bias-path", default=os.environ.get("MEMORY_BIAS_PATH", "state/agent_memory/execution_bias.json"))
    parser.add_argument("--risk-log-interval-seconds", type=float, default=60.0)
    parser.add_argument("--shadow-log-interval-seconds", type=float, default=60.0, help="minimum seconds between duplicate shadow would-trade logs")
    parser.add_argument("--live-protection-log-interval-seconds", type=float, default=30.0)
    parser.add_argument("--paper-performance-window", type=int, default=50, help="recent paper closes used for live permission gate")
    parser.add_argument("--live-min-paper-trades", type=int, default=30, help="minimum closed paper trades before live entries are allowed")
    parser.add_argument("--live-required-win-rate", type=float, default=0.80, help="required paper win rate before live entries are allowed")
    parser.add_argument("--live-required-net-usdt", type=float, default=0.0, help="required recent paper net PnL before live entries are allowed")
    parser.add_argument("--allow-live-without-paper-edge", action="store_true", help="manual override for the 80 percent paper-performance live gate")
    parser.add_argument("--emergency-close-unprotected-live", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--close-if-tp-placement-fails", action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args(list(argv))
    if args.live and not args.i_understand_risk:
        parser.error("--live requires --i-understand-risk")
    if args.margin_usdt <= 0 or args.min_margin_usdt <= 0 or args.leverage <= 0 or args.min_leverage <= 0:
        parser.error("margin and leverage bounds must be positive")
    if args.min_margin_usdt > args.margin_usdt:
        parser.error("--min-margin-usdt must be <= --margin-usdt")
    if args.min_leverage > args.leverage:
        parser.error("--min-leverage must be <= --leverage")
    if args.paper_performance_window < 1 or args.live_min_paper_trades < 1:
        parser.error("paper performance windows must be positive")
    if not 0 < args.live_required_win_rate <= 1:
        parser.error("--live-required-win-rate must be in (0, 1]")
    return args


def reset_paper_account_only(args: argparse.Namespace) -> Path:
    if args.live:
        raise SystemExit("Refusing to reset paper account in --live mode")
    state_dir = Path(args.state_dir)
    path = state_dir / "paper_account.json"
    equity = Decimal(str(args.paper_equity))
    payload = {
        "updated_at": utc_now(),
        "mode": "paper",
        "starting_equity": str(equity),
        "equity": str(equity),
        "currency": "USDT",
        "reason": "manual_reset",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_jsonl(state_dir / "scalp_autotrader.jsonl", "paper_account_reset", {"path": str(path), "equity": str(equity), "reason": "manual_reset"})
    return path


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.reset_paper_account_only:
        path = reset_paper_account_only(args)
        print(f"paper_account_reset path={path} equity={args.paper_equity}", flush=True)
        return 0
    ScalpAutoTrader(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
