from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance import spot as bs

# Check ETH-quoted pairs + low-cap memes
pairs = [
    # ETH-quoted
    "PEPEETH", "DOGEETH", "SHIBETH", "FLOKIETH", "BONKETH", "WIFETH",
    # ETH-USDT direct convert via low min
    "ETHFDUSD", "ETHUSDC",
    # BNB-quoted (need BNB)
    "PEPEBNB", "DOGEBNB",
]
for sym in pairs:
    try:
        f = bs.get_symbol_filters(sym)
        min_n = f.get("min_notional", "?")
        print(f"{sym:12s} min_notional={min_n}  min_qty={f.get('min_qty')}  step={f.get('step_size')}")
    except Exception as e:
        print(f"{sym:12s} NOT LISTED ({type(e).__name__})")

# Try convert quote endpoint
print("\n=== Convert ETH -> USDT quote test ===")
from tradingagents.binance.client import spot_client
c = spot_client()
try:
    # Binance Convert: lower minimum (~$1)
    quote = c.convert_request_quote(fromAsset="ETH", toAsset="USDT", fromAmount="0.001")
    print(f"  Quote: 0.001 ETH = {quote.get('toAmount')} USDT  (quoteId {quote.get('quoteId')})")
    print(f"  Ratio: {quote.get('ratio')}")
    print(f"  Valid until: {quote.get('validTimestamp')}")
except Exception as e:
    print(f"  Error: {type(e).__name__}: {e}")
