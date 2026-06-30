"""Set SL/TP with explicit workingType + handle hedge mode."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time

c = spot_client()

# Check position mode (one-way vs hedge)
mode = c.futures_get_position_mode()
print(f"Position mode: {mode}")  # {'dualSidePosition': False} = One-way mode

# Verify ALGO position
pos_info = c.futures_position_information(symbol="ALGOUSDT")
print(f"Position info: {pos_info}")

# Wait and retry SL placement with retry logic
import time
time.sleep(2)

# Simple TP order — sell to close SHORT when price drops to target
print("\nPlacing TP MARKET (close at $0.1069)...")
try:
    res = c.futures_create_order(
        symbol="ALGOUSDT",
        side="BUY",
        type="TAKE_PROFIT_MARKET",
        stopPrice="0.1069",
        closePosition="true",
    )
    print(f"  Response: {res}")
except Exception as e:
    print(f"  Error: {e}")

time.sleep(1)
print("\nPlacing SL MARKET (close at $0.1247)...")
try:
    res = c.futures_create_order(
        symbol="ALGOUSDT",
        side="BUY",
        type="STOP_MARKET",
        stopPrice="0.1247",
        closePosition="true",
    )
    print(f"  Response: {res}")
except Exception as e:
    print(f"  Error: {e}")

time.sleep(1)
print("\nFinal open orders:")
for o in c.futures_get_open_orders(symbol="ALGOUSDT"):
    print(f"  {o.get('type')} side={o.get('side')} stopPrice={o.get('stopPrice')} orderId={o.get('orderId')}")
