from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
c = spot_client()
orders = c.futures_get_open_orders()
print(f"Total open futures orders: {len(orders)}")
for o in orders:
    print(f"  {o.get('symbol')} type={o.get('type')} side={o.get('side')} stopPrice={o.get('stopPrice')} orderId={o.get('orderId')} status={o.get('status')}")
