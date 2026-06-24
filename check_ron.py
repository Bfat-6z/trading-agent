from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance import spot as bs
from tradingagents.binance.client import spot_client
c = spot_client()

# Check Ronin (RON) + related alts user might want
for sym in ["RONINUSDT", "RONUSDT", "AXSUSDT", "SLPUSDT",
            "NEARUSDT", "SUIUSDT", "APTUSDT", "INJUSDT",
            "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
            "LINKUSDT", "ATOMUSDT", "DOTUSDT", "AVAXUSDT",
            "ARBUSDT", "OPUSDT", "BASEUSDT"]:
    try:
        f = bs.get_symbol_filters(sym)
        info = c.get_symbol_info(sym)
        if not info or info.get("status") != "TRADING":
            print(f"  {sym:12s} NOT LISTED or halted")
            continue
        mn = f.get("min_notional", "?")
        print(f"  {sym:12s} min_notional=${mn}  min_qty={f.get('min_qty')}")
    except Exception as e:
        print(f"  {sym:12s} {type(e).__name__}: {e}")
