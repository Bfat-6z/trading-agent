"""Check multiple candidates live."""
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
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains[:14])/14; avg_l = sum(losses[:14])/14
    for i in range(14, len(gains)):
        avg_g = (avg_g*13 + gains[i]) / 14
        avg_l = (avg_l*13 + losses[i]) / 14
    rs = avg_g / avg_l if avg_l > 0 else 99
    return 100 - 100/(1+rs)

for sym in ["FARTCOINUSDT", "STEEMUSDT", "NEARUSDT", "INUSDT", "JUPUSDT", "1000PEPEUSDT"]:
    try:
        t = c.futures_ticker(symbol=sym)
        mark = float(c.futures_mark_price(symbol=sym)["markPrice"])
        hi = float(t['highPrice']); lo = float(t['lowPrice'])
        rng = (mark - lo) / (hi - lo) * 100 if hi > lo else 50
        ch24 = float(t['priceChangePercent'])
        vol_m = float(t['quoteVolume']) / 1e6
        k = c.futures_klines(symbol=sym, interval="15m", limit=20)
        closes = [float(x[4]) for x in k]
        r = rsi14(closes)
        try:
            flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                params={"symbol": sym, "period": "15m", "limit": 1}, timeout=5).json()
            f_ratio = float(flow[0]['buySellRatio']) if flow else 0
        except: f_ratio = 0
        try:
            fund = c.futures_funding_rate(symbol=sym, limit=1)
            f_pct = float(fund[0]['fundingRate'])*100*3*365 if fund else 0
        except: f_pct = 0
        print(f"{sym:15s} ${mark:.5f} ch24={ch24:+.1f}% rng={rng:.0f}% vol=${vol_m:.0f}M rsi={r:.0f} flow={f_ratio:.2f} fund={f_pct:+.0f}%/yr")
    except Exception as e:
        print(f"{sym}: err {str(e)[:30]}")
