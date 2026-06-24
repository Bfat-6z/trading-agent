"""
Scan Binance Spot for best trading candidates.
Filter: tradable USDT pairs, decent volume, interesting price action.
Returns top N candidates ranked by composite score.
"""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import sys

EXCLUDE_BASES = {
    # Stablecoins
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD", "PYUSD",
    # Wrapped / staked / leveraged
    "WBTC", "WETH", "WBETH", "STETH", "CBETH", "RETH", "BTCB", "ETHFI",
    # Already-checked
    "PEPE", "WIF", "BONK", "DOGE",
    # Major (low % moves, not useful for $3)
    "BTC", "ETH",
}


def get_top_movers(n_top: int = 30) -> list[dict]:
    c = spot_client()
    tickers = c.get_ticker()
    usdt = [t for t in tickers if t["symbol"].endswith("USDT")]

    # Parse + filter
    candidates = []
    for t in usdt:
        sym = t["symbol"]
        base = sym[:-4]
        if base in EXCLUDE_BASES:
            continue
        # Drop leveraged tokens (UP/DOWN)
        if base.endswith("UP") or base.endswith("DOWN") or base.endswith("BULL") or base.endswith("BEAR"):
            continue
        try:
            quote_vol = float(t["quoteVolume"])    # USDT vol
            price_change = float(t["priceChangePercent"])
            count = int(t["count"])
            if quote_vol < 1_000_000:    # skip <$1M daily vol = thin
                continue
            if count < 5000:              # skip <5k trades = thin participation
                continue
            candidates.append({
                "symbol": sym,
                "base": base,
                "price": float(t["lastPrice"]),
                "change_24h": price_change,
                "volume_usdt": quote_vol,
                "trades_count": count,
                "high_24h": float(t["highPrice"]),
                "low_24h": float(t["lowPrice"]),
            })
        except (ValueError, KeyError):
            continue

    return candidates


def score_candidate(c: dict) -> float:
    """
    Composite score for picking attractive candidates.
    Favor: high volume + recent dump + late-day bounce pattern (mean reversion)
    Or: moderate gain with steady volume (continuation)
    """
    change = c["change_24h"]
    vol = c["volume_usdt"]
    # Range: how much price has moved within 24h
    rng = (c["high_24h"] - c["low_24h"]) / c["low_24h"] * 100 if c["low_24h"] > 0 else 0
    # Position in 24h range: 0 = at low, 1 = at high
    if c["high_24h"] > c["low_24h"]:
        pos = (c["price"] - c["low_24h"]) / (c["high_24h"] - c["low_24h"])
    else:
        pos = 0.5

    # Score components:
    # - log volume (favor liquid)
    # - oversold bounce: -10% to -3% AND bounced off low (pos > 0.4) = good
    # - mild gain: 0% to +8% with healthy participation
    import math
    vol_score = math.log10(max(vol, 1)) / 8     # 1e6 vol -> 0.75, 1e8 vol -> 1.0

    if -15 <= change <= -3 and pos > 0.4:
        # Oversold bounce setup
        action_score = 1.5
    elif 0 <= change <= 10:
        # Mild gain, sustainable
        action_score = 1.0
    elif -3 < change < 0:
        # Slight pullback, near breakeven
        action_score = 0.8
    elif 10 < change <= 25:
        # Momentum (risk of fading)
        action_score = 0.6
    else:
        action_score = 0.2     # too dumpy or too pumpy

    return vol_score * action_score


def main(top_n: int = 10):
    print("Scanning Binance USDT pairs...")
    cs = get_top_movers()
    print(f"  {len(cs)} pairs pass filters")

    scored = [(score_candidate(c), c) for c in cs]
    scored.sort(reverse=True, key=lambda x: x[0])

    print(f"\nTop {top_n} candidates by score:")
    print(f"{'Rank':<5} {'Symbol':<12} {'Score':<7} {'Price':<14} {'24h%':<10} {'Vol $M':<10} {'Range%':<8} {'RngPos':<7}")
    for i, (score, c) in enumerate(scored[:top_n], 1):
        rng = (c["high_24h"] - c["low_24h"]) / c["low_24h"] * 100 if c["low_24h"] > 0 else 0
        pos = (c["price"] - c["low_24h"]) / (c["high_24h"] - c["low_24h"]) if c["high_24h"] > c["low_24h"] else 0
        print(f"{i:<5} {c['symbol']:<12} {score:<7.3f} {c['price']:<14.6f} {c['change_24h']:<10.2f} {c['volume_usdt']/1e6:<10.1f} {rng:<8.2f} {pos:<7.2f}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    main(n)
