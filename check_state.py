from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
c = spot_client()
pos = c.futures_position_information()
opens = [p for p in pos if abs(float(p["positionAmt"])) > 0]
print(f"Open positions: {len(opens)}")
for p in opens:
    qty = float(p["positionAmt"])
    side = "LONG" if qty > 0 else "SHORT"
    entry = float(p["entryPrice"])
    mark = float(p["markPrice"])
    pnl = float(p["unRealizedProfit"])
    print(f"  {p['symbol']} {side} qty={abs(qty)} entry=${entry} mark=${mark} unPnL=${pnl:+.4f}")
bal = c.futures_account_balance()
usdt = next(a for a in bal if a["asset"] == "USDT")
print(f"Wallet: ${float(usdt['balance']):.4f}  Avail: ${float(usdt['availableBalance']):.4f}")

# Recent BSB trades
import time
from datetime import datetime
recent = c.futures_account_trades(symbol="BSBUSDT", limit=5)
print("\nRecent BSB trades:")
for t in recent[-5:]:
    ts = datetime.fromtimestamp(t["time"]/1000).strftime("%H:%M:%S")
    print(f"  {ts} {t['side']} qty={t['qty']} @ ${t['price']} pnl=${t.get('realizedPnl','0')}")
