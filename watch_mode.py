"""
Autonomous watch loop for the trading agent.
- Re-scans Binance top movers every INTERVAL_MIN minutes
- Runs 8-agent debate on top candidates
- Auto-executes BUY when verdict = EXECUTE
- Monitors open positions for SL/TP

Run: python watch_mode.py
Halt: create file state/kill_switch (or run `trading-agent.bat kill`)
"""
from __future__ import annotations

import math
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

# Resolve project root + load .env
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from tradingagents.binance import spot as bs
from tradingagents.binance.client import spot_client
from tradingagents.binance.data import fetch_binance_snapshot
from tradingagents.crypto import agents as ag
from tradingagents.dex import positions

# Config
INTERVAL_MIN = int(os.environ.get("WATCH_INTERVAL_MIN", "20"))
BUDGET_USD = float(os.environ.get("MAX_POSITION_USD", "2.0"))
MAX_OPEN = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "1"))
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT_USD", "3.0"))
TOP_K = 6              # how many candidates to analyze per scan
MIN_VOL_M = 1.0        # $M (lowered to expand universe)
EXCLUDE = {
    # Stablecoins
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD", "PYUSD", "EUR", "USD1", "EURI",
    # Wrapped / staked / synth
    "WBTC", "WETH", "WBETH", "STETH", "CBETH", "RETH", "BTCB",
    # NOTE: memes like PEPE/WIF/BONK/DOGE INCLUDED on purpose — let agents re-evaluate fresh each cycle
}


_LOG_FILE = ROOT / "state" / "watch.log"
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_log_fh = open(_LOG_FILE, "a", encoding="utf-8", buffering=1)


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        _log_fh.write(line + "\n")
    except Exception:
        pass


def get_usdt_balance() -> float:
    try:
        b = spot_client().get_asset_balance(asset="USDT")
        return float(b["free"]) if b else 0.0
    except Exception:
        return 0.0


def scan_top_movers(top_k: int) -> list[dict]:
    """Return top-K candidates that are tradable with our budget."""
    c = spot_client()
    tickers = c.get_ticker()
    out = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in EXCLUDE:
            continue
        if base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        try:
            vol_m = float(t["quoteVolume"]) / 1e6
            ch = float(t["priceChangePercent"])
            count = int(t["count"])
            if vol_m < MIN_VOL_M or count < 5000:
                continue
            high = float(t["highPrice"])
            low = float(t["lowPrice"])
            price = float(t["lastPrice"])
            rng_pos = (price - low) / (high - low) if high > low else 0.5
            # Composite score (favor oversold bounces + mild gains)
            log_v = math.log10(max(vol_m * 1e6, 1)) / 8
            if -15 <= ch <= -3 and rng_pos > 0.4:
                regime = 1.5
            elif -3 < ch <= 5:
                regime = 1.0
            elif 5 < ch <= 12:
                regime = 1.1
            else:
                regime = 0.4
            score = log_v * regime
            out.append({
                "symbol": s, "base": base, "score": score, "price": price,
                "ch24": ch, "vol_m": vol_m, "rng_pos": rng_pos,
            })
        except Exception:
            continue

    out.sort(key=lambda x: x["score"], reverse=True)

    # Filter by min notional <= budget
    tradable = []
    for c_ in out[:30]:
        try:
            f = bs.get_symbol_filters(c_["symbol"])
            mn = float(f.get("min_notional", 99))
            if mn <= BUDGET_USD:
                c_["min_notional"] = mn
                tradable.append(c_)
            if len(tradable) >= top_k:
                break
        except Exception:
            continue
    return tradable


def run_analysis(symbol: str, eth_usd: float = 2120.0) -> dict | None:
    """Full 16-call pipeline on a single symbol."""
    try:
        snap = fetch_binance_snapshot(symbol)
    except Exception as e:
        log(f"  {symbol}: snapshot fail ({e})")
        return None

    analyst_fns = [
        ag.agent_market,
        ag.agent_onchain,
        lambda s: ag.agent_liquidity(s, BUDGET_USD),
        ag.agent_sentiment,
        ag.agent_news,
    ]
    analysts = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(fn, snap) for fn in analyst_fns]
        for f in futs:
            try:
                analysts.append(f.result(timeout=60))
            except Exception:
                continue
    if not analysts:
        return None

    debate = ag.debate_round(snap, analysts, num_rounds=2)
    risk = ag.risk_debate(snap, debate,
                          positions.count_open(),
                          positions.realized_pnl_today_usd(),
                          BUDGET_USD)
    order = ag.agent_trader(snap, debate, risk, BUDGET_USD, eth_usd)
    verdict = ag.agent_portfolio_manager(
        order, snap, debate, risk,
        positions.count_open(), positions.realized_pnl_today_usd(),
        DAILY_LOSS_LIMIT, MAX_OPEN,
    )
    return {
        "symbol": symbol, "snap": snap, "verdict": verdict, "order": order,
        "debate": debate, "risk": risk, "analysts": analysts,
    }


def auto_execute_buy(result: dict) -> bool:
    """Execute BUY via Binance market order."""
    sym = result["symbol"]
    order = result["order"]
    snap = result["snap"]

    # Refresh USDT balance for sizing
    bal = get_usdt_balance()
    spend = min(BUDGET_USD, bal - 0.05)   # leave a sliver for safety
    if spend < 1.0:
        log(f"  Skip buy {sym}: insufficient USDT balance (${bal:.2f})")
        return False

    log(f"  EXECUTING BUY {sym} amount=${spend:.2f}")
    try:
        res = bs.market_buy(sym, spend)
    except Exception as e:
        log(f"  BUY FAILED: {e}")
        return False

    log(f"  Filled: {res.executed_qty} {sym} at avg ${res.avg_price}")
    log(f"  Cost: ${res.executed_quote_qty:.4f}")

    # Record position (we don't use the same SL/TP-on-chain approach here — Binance Spot doesn't allow OCO without locked qty)
    pos = positions.Position(
        token_address=f"binance:{sym}",
        token_symbol=sym,
        status="open",
        entry_tx=str(res.order_id),
        entry_price_usd=res.avg_price,
        entry_price_eth=0.0,
        amount_token_raw=int(res.executed_qty * 1e18),
        decimals=18,
        amount_eth_spent=spend,
        sl_pct=order.stop_loss_pct,
        tp1_pct=order.take_profit_1_pct,
        tp2_pct=order.take_profit_2_pct,
        pair_address=sym,
    )
    positions.upsert(pos)
    return True


def manage_open_positions():
    """Check open positions and sell if SL/TP hit (uses Binance market sell)."""
    open_p = positions.list_open()
    if not open_p:
        return
    for p in open_p:
        if not p.pair_address:
            continue
        try:
            t = spot_client().get_ticker(symbol=p.pair_address)
            price = float(t["lastPrice"])
        except Exception:
            continue

        change = (price - p.entry_price_usd) / p.entry_price_usd * 100
        log(f"  Position {p.token_symbol} @ ${price:.6f} ({change:+.2f}% from entry)")

        # SL
        if price <= p.sl_price_usd:
            log(f"  -> SL hit, selling all")
            try:
                qty = bs.get_balance(p.token_symbol[:-4])["free"]
                res = bs.market_sell(p.pair_address, qty)
                p.status = "closed"
                p.closed_at = time.time()
                p.exit_price_usd = res.avg_price
                p.realized_pnl_usd = (res.avg_price - p.entry_price_usd) * res.executed_qty
                positions.upsert(p)
                positions.append_pnl(p.token_symbol, "SL", p.realized_pnl_usd, "watch SL")
            except Exception as e:
                log(f"  Sell failed: {e}")
        # TP2
        elif price >= p.tp2_price_usd and not p.tp2_hit:
            log(f"  -> TP2 hit, selling all")
            try:
                qty = bs.get_balance(p.token_symbol[:-4])["free"]
                res = bs.market_sell(p.pair_address, qty)
                p.status = "closed"
                p.tp2_hit = True
                p.closed_at = time.time()
                p.exit_price_usd = res.avg_price
                p.realized_pnl_usd = (res.avg_price - p.entry_price_usd) * res.executed_qty
                positions.upsert(p)
                positions.append_pnl(p.token_symbol, "TP2", p.realized_pnl_usd, "watch TP2")
            except Exception as e:
                log(f"  Sell failed: {e}")
        # TP1 (sell 50%)
        elif price >= p.tp1_price_usd and not p.tp1_hit:
            log(f"  -> TP1 hit, selling 50%")
            try:
                qty = bs.get_balance(p.token_symbol[:-4])["free"]
                half = qty * 0.5
                res = bs.market_sell(p.pair_address, half)
                p.tp1_hit = True
                positions.upsert(p)
                pnl = (res.avg_price - p.entry_price_usd) * res.executed_qty
                positions.append_pnl(p.token_symbol, "TP1", pnl, "watch TP1 (50%)")
            except Exception as e:
                log(f"  Sell failed: {e}")


def main_loop():
    log(f"Watch mode started. Interval: {INTERVAL_MIN}min  Budget: ${BUDGET_USD}")
    log(f"Halt: trading-agent.bat kill  (creates state/kill_switch)")

    while True:
        if positions.kill_switch_active():
            log("Kill switch active. Halting.")
            break

        try:
            # Step 1: manage any open positions
            manage_open_positions()

            # Step 2: if room for new position, scan
            open_count = positions.count_open()
            if open_count >= MAX_OPEN:
                log(f"Max positions reached ({open_count}/{MAX_OPEN}). Just monitoring.")
            else:
                bal = get_usdt_balance()
                log(f"USDT balance: ${bal:.4f}. Scanning top movers...")
                if bal < 1.0:
                    log("  Balance too low to trade, skipping scan")
                else:
                    candidates = scan_top_movers(TOP_K)
                    log(f"  {len(candidates)} tradable candidates")

                    # Run analyses in parallel (max 2 concurrent to respect API limits)
                    found_buy = False
                    with ThreadPoolExecutor(max_workers=2) as ex:
                        futs = {ex.submit(run_analysis, c["symbol"]): c["symbol"]
                                for c in candidates}
                        for fut in as_completed(futs):
                            sym = futs[fut]
                            try:
                                r = fut.result(timeout=180)
                            except Exception as e:
                                log(f"  {sym}: pipeline error ({e})")
                                continue
                            if not r:
                                continue
                            v = r["verdict"]
                            d = r["debate"]
                            rk = r["risk"]
                            log(f"  {sym:<12} {v.action:<8} debate={d.consensus} ({d.consensus_strength:.2f})  risk={rk.recommendation} ({rk.risk_score:.1f})")
                            if v.action == "EXECUTE" and not found_buy:
                                ok = auto_execute_buy(r)
                                if ok:
                                    found_buy = True
                                    log(f"  ✓ Position opened on {sym}")
                                    break

                    if not found_buy:
                        log("  No EXECUTE signal. Holding cash.")

        except Exception as e:
            log(f"Loop error: {type(e).__name__}: {e}")
            traceback.print_exc()

        log(f"Sleeping {INTERVAL_MIN}min until next cycle...")
        for _ in range(INTERVAL_MIN * 60 // 10):
            if positions.kill_switch_active():
                log("Kill switch detected during sleep. Halting.")
                return
            time.sleep(10)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log("Stopped by user.")
