"""Find coins at EXTREME microstructure NOW (A+ setup hunt).
Criteria for LONG bounce: rng_pos<25 + ch24<-5 + vol>$30M + 15m_rsi<40
Criteria for SHORT exhaustion: rng_pos>80 + ch24>+15 + vol>$30M + 15m_rsi>72
"""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import requests
c = spot_client()

def rsi14(closes):
    if len(closes) < 15: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:14])/14
    avg_l = sum(losses[:14])/14
    for i in range(14, len(gains)):
        avg_g = (avg_g*13 + gains[i]) / 14
        avg_l = (avg_l*13 + losses[i]) / 14
    rs = avg_g / avg_l if avg_l > 0 else 99
    return 100 - 100/(1+rs)

# Get all tickers
all_t = c.futures_ticker()
big = [t for t in all_t if float(t["quoteVolume"]) > 30_000_000]
print(f"Universe (vol>$30M): {len(big)}")

long_candidates = []
short_candidates = []

for t in big:
    sym = t["symbol"]
    if not sym.endswith("USDT"): continue
    ch24 = float(t["priceChangePercent"])
    hi = float(t["highPrice"]); lo = float(t["lowPrice"])
    last = float(t["lastPrice"])
    rng = (last - lo) / (hi - lo) * 100 if hi > lo else 50
    vol_m = float(t["quoteVolume"]) / 1e6

    # Filter for extreme
    if ch24 < -5 and rng < 25:
        long_candidates.append({"sym": sym, "ch24": ch24, "rng": rng, "vol_m": vol_m, "last": last, "lo": lo, "hi": hi})
    elif ch24 > 15 and rng > 80:
        short_candidates.append({"sym": sym, "ch24": ch24, "rng": rng, "vol_m": vol_m, "last": last, "lo": lo, "hi": hi})

# Sort by extremity
long_candidates.sort(key=lambda x: x["rng"])
short_candidates.sort(key=lambda x: -x["rng"])

print(f"\n=== LONG BOUNCE candidates (rng<25 + ch24<-5 + vol>$30M) ===")
for cd in long_candidates[:8]:
    # Get 15m RSI
    try:
        k = c.futures_klines(symbol=cd["sym"], interval="15m", limit=40)
        closes = [float(x[4]) for x in k]
        r = rsi14(closes)
        funding = c.futures_funding_rate(symbol=cd["sym"], limit=1)
        f_pct = float(funding[0]["fundingRate"])*100*3*365 if funding else 0
        print(f"  {cd['sym']:14s} ch24={cd['ch24']:+.1f}% rng={cd['rng']:.0f}% vol=${cd['vol_m']:.0f}M last=${cd['last']:.5f} rsi15m={r:.0f} fund={f_pct:+.0f}%/yr")
    except Exception as e:
        print(f"  {cd['sym']}: err {str(e)[:30]}")

print(f"\n=== SHORT EXHAUSTION candidates (rng>80 + ch24>+15 + vol>$30M) ===")
for cd in short_candidates[:8]:
    try:
        k = c.futures_klines(symbol=cd["sym"], interval="15m", limit=40)
        closes = [float(x[4]) for x in k]
        r = rsi14(closes)
        funding = c.futures_funding_rate(symbol=cd["sym"], limit=1)
        f_pct = float(funding[0]["fundingRate"])*100*3*365 if funding else 0
        print(f"  {cd['sym']:14s} ch24={cd['ch24']:+.1f}% rng={cd['rng']:.0f}% vol=${cd['vol_m']:.0f}M last=${cd['last']:.5f} rsi15m={r:.0f} fund={f_pct:+.0f}%/yr")
    except Exception as e:
        print(f"  {cd['sym']}: err {str(e)[:30]}")
