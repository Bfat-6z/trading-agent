"""Pull TV data + compute multi-timeframe indicators for analysis."""
from tvDatafeed import TvDatafeed, Interval
import pandas_ta_classic as ta
import sys

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "ZECUSDT"
EXCHANGE = sys.argv[2] if len(sys.argv) > 2 else "BINANCE"

tv = TvDatafeed()


def analyze(tf_name, interval, n):
    df = tv.get_hist(symbol=SYMBOL, exchange=EXCHANGE, interval=interval, n_bars=n)
    if df is None or len(df) < 50:
        print(f"=== {tf_name} === (insufficient data)\n")
        return None
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200) if len(df) >= 200 else df["ema50"]
    bb = ta.bbands(df["close"], length=20, std=2)
    df["bb_u"] = bb["BBU_20_2.0"]
    df["bb_l"] = bb["BBL_20_2.0"]
    df["bb_m"] = bb["BBM_20_2.0"]
    macd = ta.macd(df["close"])
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_sig"] = macd["MACDs_12_26_9"]
    df["macd_hist"] = macd["MACDh_12_26_9"]
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    stoch = ta.stoch(df["high"], df["low"], df["close"])
    df["stoch_k"] = stoch["STOCHk_14_3_3"]
    df["stoch_d"] = stoch["STOCHd_14_3_3"]
    adx = ta.adx(df["high"], df["low"], df["close"])
    df["adx"] = adx["ADX_14"]
    df["di_p"] = adx["DMP_14"]
    df["di_n"] = adx["DMN_14"]

    last = df.iloc[-1]
    prev = df.iloc[-2]
    p_chg = (last["close"] - prev["close"]) / prev["close"] * 100
    bb_pos = (last["close"] - last["bb_l"]) / (last["bb_u"] - last["bb_l"]) * 100
    ema20_dist = (last["close"] - last["ema20"]) / last["ema20"] * 100

    trend_short = "UP" if last["ema20"] > last["ema50"] else "DOWN"
    trend_long = "UP" if last["ema50"] > last["ema200"] else "DOWN"
    macd_dir = "BULL" if last["macd_hist"] > 0 else "BEAR"
    macd_cross = "UP" if last["macd_hist"] > 0 and prev["macd_hist"] <= 0 else (
                  "DN" if last["macd_hist"] < 0 and prev["macd_hist"] >= 0 else " ")
    rsi_state = "OVERBOUGHT" if last["rsi"] > 70 else ("OVERSOLD" if last["rsi"] < 30 else
                 "BULL" if last["rsi"] > 50 else "BEAR")
    stoch_state = "OB" if last["stoch_k"] > 80 else ("OS" if last["stoch_k"] < 20 else "MID")

    print(f"=== {tf_name} ===")
    print(f"  Close: ${last['close']:.2f}  ch: {p_chg:+.2f}%  ATR(14): ${last['atr']:.2f} ({last['atr']/last['close']*100:.1f}%)")
    print(f"  EMA20: ${last['ema20']:.2f}  EMA50: ${last['ema50']:.2f}  EMA200: ${last['ema200']:.2f}")
    print(f"  Trend: short={trend_short}  long={trend_long}  price-vs-EMA20: {ema20_dist:+.2f}%")
    print(f"  RSI: {last['rsi']:.1f} [{rsi_state}]  Stoch %K: {last['stoch_k']:.1f} [{stoch_state}]")
    print(f"  MACD: {last['macd']:.3f} sig={last['macd_sig']:.3f} hist={last['macd_hist']:+.3f} [{macd_dir}{macd_cross}]")
    print(f"  BB: [${last['bb_l']:.2f}, ${last['bb_u']:.2f}]  pos: {bb_pos:.0f}%")
    print(f"  ADX: {last['adx']:.1f}  +DI: {last['di_p']:.1f}  -DI: {last['di_n']:.1f}")
    print()

    return {
        "rsi": last["rsi"], "macd_hist": last["macd_hist"], "trend": trend_short,
        "bb_pos": bb_pos, "adx": last["adx"], "ema_dist": ema20_dist,
    }


print(f"\n[{SYMBOL}] multi-timeframe analysis\n")
r15 = analyze("15m", Interval.in_15_minute, 200)
r1h = analyze("1h ", Interval.in_1_hour, 300)
r4h = analyze("4h ", Interval.in_4_hour, 300)
r1d = analyze("1D ", Interval.in_daily, 300)

# Consolidated read
print("=== CONSOLIDATED ===")
def score(r, side):
    if r is None: return 0
    s = 0
    if side == "SHORT":
        if r["trend"] == "DOWN": s += 2
        if r["rsi"] > 70: s += 2
        elif r["rsi"] > 60: s += 1
        elif r["rsi"] < 30: s -= 2
        if r["macd_hist"] < 0: s += 2
        if r["bb_pos"] > 80: s += 1
        elif r["bb_pos"] < 20: s -= 1
        if r["adx"] > 25 and r["trend"] == "DOWN": s += 1
        if r["ema_dist"] > 5: s += 1
    return s

s15 = score(r15, "SHORT"); s1h = score(r1h, "SHORT")
s4h = score(r4h, "SHORT"); s1d = score(r1d, "SHORT")
total = s15 + s1h*1.5 + s4h*2 + s1d*1.5
print(f"  SHORT score: 15m={s15}  1h={s1h}  4h={s4h}  1D={s1d}  weighted={total:.1f}")
print(f"  Verdict: {'SHORT GOOD' if total > 6 else ('SHORT MARGINAL' if total > 2 else 'SHORT RISKY')}")
