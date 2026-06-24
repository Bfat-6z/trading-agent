from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
c = spot_client()
pos = c.futures_position_information(symbol="MAGMAUSDT")[0]
for k, v in pos.items():
    print(f"  {k}: {v}")
