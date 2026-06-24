"""Single-cycle monitor for PLAY A+ signals. Run periodically."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import requests
from datetime import datetime

c = spot_client()
SYM = "PLAYUSDT"

def rsi14(closes):
    if len(closes) < 15: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains[:14])/14; avg_l = sum(losses[:14])/14
    for i in range(14, len(gains)):
        avg_g = (avg_g*13 + gains[i]) / 14
        avg_l = (avg_l*13 + losses[i]) / 14
    rs = avg_g / avg_l if avg_l > 0 else 99
    return 100 - 100/(1+rs)

now = datetime.now().strftime("%H:%M:%S")
t = c.futures_ticker(symbol=SYM)
mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
hi = float(t['highPrice']); lo = float(t['lowPrice'])
rng = (mark - lo) / (hi - lo) * 100
ch24 = float(t['priceChangePercent'])

klines_15m = c.futures_klines(symbol=SYM, interval="15m", limit=20)
closes = [float(k[4]) for k in klines_15m]
r15 = rsi14(closes)

try:
    flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
        params={"symbol": SYM, "period": "15m", "limit": 3}, timeout=10).json()
    flow_latest = float(flow[-1]["buySellRatio"])
    flow_prev = float(flow[-2]["buySellRatio"]) if len(flow) >= 2 else flow_latest
except:
    flow_latest = flow_prev = 0

try:
    oi_hist = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
        params={"symbol": SYM, "period": "1h", "limit": 24}, timeout=10).json()
    oi_24h_ago = float(oi_hist[0]["sumOpenInterest"])
    oi_now = float(oi_hist[-1]["sumOpenInterest"])
    oi_2h_ago = float(oi_hist[-3]["sumOpenInterest"]) if len(oi_hist) >= 3 else oi_now
    oi_trend_24h = (oi_now/oi_24h_ago - 1)*100
    oi_trend_2h = (oi_now/oi_2h_ago - 1)*100
except:
    oi_trend_24h = oi_trend_2h = 0

try:
    fund = c.futures_funding_rate(symbol=SYM, limit=1)
    fund_pct = float(fund[0]['fundingRate'])*100*3*365 if fund else 0
except:
    fund_pct = 0

# A+ Score
score = 0
score += 1 if rng < 20 else 0
score += 1 if r15 < 40 else 0
score += 1 if flow_latest >= 1.5 else 0  # KEY: buyers stepping in
score += 1 if oi_trend_24h < 0 else 0    # KEY: shorts covering
score += 1 if -50 < fund_pct < 50 else 0  # neutral funding (not extreme)

print(f"[{now}] {SYM} ${mark:.5f} ch24={ch24:+.2f}% rng={rng:.0f}% rsi15m={r15:.0f}")
print(f"  flow now/prev: {flow_latest:.2f}/{flow_prev:.2f} (need >=1.5)")
print(f"  OI trend 24h: {oi_trend_24h:+.1f}% / 2h: {oi_trend_2h:+.1f}% (need <0)")
print(f"  funding: {fund_pct:+.0f}%/yr (need -50..+50)")
print(f"  A+ SCORE: {score}/5 {'[A+ FIRE]' if score>=4 else '[wait]'}")
