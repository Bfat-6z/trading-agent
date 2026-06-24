"""Convert all Spot ETH to USDT via Binance Convert API."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
c = spot_client()

# Read current ETH balance
bal = c.get_asset_balance(asset="ETH")
eth_free = float(bal["free"])
print(f"Current ETH free balance: {eth_free}")
if eth_free < 0.0001:
    print("Not enough ETH to convert.")
    raise SystemExit(1)

# Use slightly less than free to avoid rounding issues
amount = f"{eth_free * 0.999:.8f}".rstrip("0").rstrip(".")
print(f"Converting {amount} ETH to USDT...")

# Step 1: get quote
quote = c.convert_request_quote(fromAsset="ETH", toAsset="USDT", fromAmount=amount)
print(f"  Quote: {amount} ETH = {quote['toAmount']} USDT")
print(f"  Rate: 1 ETH = ${quote['ratio']}")
print(f"  QuoteId: {quote['quoteId']}")

# Step 2: accept quote (auto since user pre-authorized handling)
result = c.convert_accept_quote(quoteId=quote["quoteId"])
print(f"\nResult: {result}")
print(f"Status: {result.get('orderStatus')}")
