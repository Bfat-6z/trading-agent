"""Continuous monitor of ZEC SHORT position. Logs to state/zec_monitor.log every 30s."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from pathlib import Path
import time

c = spot_client()
SYMBOL = "ZECUSDT"
LOG_FILE = Path("state/zec_monitor.log")
LOG_FILE.parent.mkdir(exist_ok=True)
fh = open(LOG_FILE, "a", encoding="utf-8", buffering=1)


def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    fh.write(line + "\n")


log(f"Monitor started for {SYMBOL}")
log("-" * 70)

last_pnl = None
while True:
    try:
        pos_list = c.futures_position_information(symbol=SYMBOL)
        if not pos_list or abs(float(pos_list[0]["positionAmt"])) == 0:
            log(f"Position CLOSED. Final state. Monitor exiting.")
            # Get last trade for outcome
            time.sleep(2)
            trades = c.futures_account_trades(symbol=SYMBOL, limit=3)
            for t in trades[-3:]:
                log(f"  Trade: {t.get('side')} {t.get('qty')} @ ${t.get('price')} realizedPnl=${t.get('realizedPnl')}")
            bal = c.futures_account_balance()
            for a in bal:
                if a["asset"] == "USDT":
                    log(f"  Futures wallet: ${a['balance']}")
                    break
            break

        pos = pos_list[0]
        qty = abs(float(pos["positionAmt"]))
        entry = float(pos["entryPrice"])
        mark = float(pos["markPrice"])
        pnl = float(pos["unRealizedProfit"])
        roe_pct = pnl / float(pos["isolatedWallet"]) * 100 if float(pos["isolatedWallet"]) > 0 else 0
        price_move_pct = (mark - entry) / entry * 100

        # Only log significant changes (every 30s if no change, or on >0.1% PnL change)
        if last_pnl is None or abs(pnl - last_pnl) >= 0.01:
            indicator = "📈" if pnl > 0 else "📉" if pnl < 0 else "➖"
            log(f"{indicator} mark=${mark:.4f}  price_move={price_move_pct:+.2f}%  unPnL=${pnl:+.4f}  ROE={roe_pct:+.2f}%")
            last_pnl = pnl

        time.sleep(30)
    except KeyboardInterrupt:
        log("Stopped by user.")
        break
    except Exception as e:
        log(f"Error: {type(e).__name__}: {e}")
        time.sleep(30)

fh.close()
