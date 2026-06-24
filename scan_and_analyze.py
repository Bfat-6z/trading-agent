"""
Full pipeline: scan Binance USDT pairs -> filter for trade-able with budget ->
analyze top N candidates in parallel with 8-agent pipeline -> recommend best.
"""
from dotenv import load_dotenv
load_dotenv()

import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from tradingagents.binance.client import spot_client
from tradingagents.binance import spot as bs
from tradingagents.binance.data import fetch_binance_snapshot
from tradingagents.crypto import agents

# Constants
BUDGET_USD = 3.0
MAX_CANDIDATES = 6                 # how many to put through agent pipeline

EXCLUDE = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD", "PYUSD", "EUR", "USD1",
    "WBTC", "WETH", "WBETH", "STETH", "CBETH", "RETH", "BTCB", "ETHFI",
    "PEPE", "WIF", "BONK", "DOGE",   # already analyzed
}


def get_movers():
    c = spot_client()
    tickers = c.get_ticker()
    out = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in EXCLUDE or any(x in base for x in ["UP", "DOWN", "BULL", "BEAR"]):
            continue
        try:
            v = float(t["quoteVolume"])
            ch = float(t["priceChangePercent"])
            cnt = int(t["count"])
            if v < 2_000_000 or cnt < 10000:
                continue
            high = float(t["highPrice"])
            low = float(t["lowPrice"])
            price = float(t["lastPrice"])
            rng_pos = (price - low) / (high - low) if high > low else 0.5
            out.append({
                "symbol": s, "base": base, "price": price,
                "ch24": ch, "vol_m": v / 1e6, "count": cnt,
                "high": high, "low": low, "rng_pos": rng_pos,
                "rng_pct": (high - low) / low * 100 if low > 0 else 0,
            })
        except Exception:
            continue
    return out


def score(c):
    """Composite: volume + price action regime + range position."""
    vol_s = math.log10(max(c["vol_m"] * 1_000_000, 1)) / 8
    ch = c["ch24"]
    pos = c["rng_pos"]
    if -15 <= ch <= -3 and pos > 0.4:
        regime = 1.5     # oversold bounce
    elif -3 < ch <= 5:
        regime = 1.0     # near flat
    elif 5 < ch <= 15:
        regime = 1.2     # healthy gain
    elif 15 < ch <= 30:
        regime = 0.7     # late momentum
    else:
        regime = 0.2
    return vol_s * regime


def check_min_notional(symbol):
    try:
        f = bs.get_symbol_filters(symbol)
        return float(f.get("min_notional", 5))
    except Exception:
        return 999


def main():
    print(f"Scanning Binance USDT pairs (budget ${BUDGET_USD})...")
    movers = get_movers()
    print(f"  {len(movers)} pairs pass volume/activity filters")

    # Rank
    movers.sort(key=score, reverse=True)

    # Filter by min notional
    tradable = []
    for m in movers[:50]:
        mn = check_min_notional(m["symbol"])
        if mn <= BUDGET_USD:
            m["min_notional"] = mn
            tradable.append(m)
        if len(tradable) >= MAX_CANDIDATES:
            break

    if not tradable:
        print("No tradable candidates with $3 budget. All min_notional > $3.")
        sys.exit(1)

    print(f"\nTop {len(tradable)} candidates (after min_notional <= ${BUDGET_USD}):")
    print(f"{'Rank':<5} {'Symbol':<12} {'Score':<7} {'24h%':<8} {'Vol $M':<8} {'Min$':<6} {'RngPos':<7}")
    for i, c in enumerate(tradable, 1):
        print(f"{i:<5} {c['symbol']:<12} {score(c):<7.3f} {c['ch24']:<8.2f} {c['vol_m']:<8.1f} ${c['min_notional']:<5} {c['rng_pos']:<7.2f}")

    # Run agent pipeline in parallel
    print(f"\nRunning 8-agent pipeline on top {len(tradable)} (parallel)...")
    print("This will take 60-90s. Each pair uses ~16 LLM calls via 9router (free).\n")

    results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        future_to_sym = {}
        for c in tradable:
            future_to_sym[ex.submit(_run_analysis, c["symbol"])] = c["symbol"]
        for fut in as_completed(future_to_sym):
            sym = future_to_sym[fut]
            try:
                r = fut.result(timeout=180)
                results.append(r)
                print(f"  {sym:<12} -> {r['action']:<8} debate {r['consensus']:<8} ({r['cs']:.2f})  risk {r['rec']:<14} score {r['rs']:.1f}")
            except Exception as e:
                print(f"  {sym:<12} -> FAILED {type(e).__name__}: {e}")

    # Sort by attractiveness: EXECUTE first, then consensus + strength
    def rank(r):
        if r["action"] == "EXECUTE":
            base = 100
        elif r["consensus"] == "bullish":
            base = 50
        elif r["consensus"] == "neutral":
            base = 25
        else:
            base = 0
        return base + r["cs"] * 10 - r["rs"]

    results.sort(key=rank, reverse=True)
    print("\n=== FINAL RANKING ===")
    for r in results:
        print(f"  {r['symbol']:<12} action={r['action']:<8} consensus={r['consensus']:<8} ({r['cs']:.2f})  risk={r['rec']:<14} ({r['rs']:.1f})")
        print(f"      Bull: {r['bull'][:120]}")
        print(f"      Bear: {r['bear'][:120]}")

    # Best pick
    best = results[0]
    print(f"\n>> BEST CANDIDATE: {best['symbol']}")
    if best["action"] == "EXECUTE":
        print(f"   Agents APPROVE entry. To execute:")
        print(f"   trading-agent.bat buy-binance {best['symbol']} --amount {BUDGET_USD} --skip-analyze")
    else:
        print(f"   Agents REJECT all candidates. Best of bad lot is {best['symbol']}.")
        print(f"   Recommendation: wait for clearer setup.")


def _run_analysis(symbol):
    """Run full 16-call pipeline for one symbol, return summary dict."""
    snap = fetch_binance_snapshot(symbol)
    analyst_fns = [
        agents.agent_market,
        agents.agent_onchain,
        lambda s: agents.agent_liquidity(s, BUDGET_USD),
        agents.agent_sentiment,
        agents.agent_news,
    ]
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(fn, snap) for fn in analyst_fns]
        analysts = []
        for f in futs:
            try:
                analysts.append(f.result(timeout=60))
            except Exception:
                pass

    debate = agents.debate_round(snap, analysts, num_rounds=2)
    risk = agents.risk_debate(snap, debate, 0, 0.0, BUDGET_USD)
    order = agents.agent_trader(snap, debate, risk, BUDGET_USD, 2120.0)
    verdict = agents.agent_portfolio_manager(order, snap, debate, risk, 0, 0.0, 3.0, 3)

    return {
        "symbol": symbol,
        "action": verdict.action,
        "consensus": debate.consensus,
        "cs": debate.consensus_strength,
        "rec": risk.recommendation,
        "rs": risk.risk_score,
        "bull": debate.bull_thesis,
        "bear": debate.bear_thesis,
    }


if __name__ == "__main__":
    main()
