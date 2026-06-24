"""Quick test that TV analyst is firing in the pipeline."""
from dotenv import load_dotenv
load_dotenv()
import time
from tradingagents.binance.data import fetch_binance_snapshot
from tradingagents.crypto import agents

print("Fetching ZECUSDT snapshot...")
snap = fetch_binance_snapshot("ZECUSDT")
print(f"  symbol={snap.symbol} price=${snap.price_usd}\n")

print("Calling agent_tv_technicals...")
t0 = time.time()
out = agents.agent_tv_technicals(snap, binance_symbol="ZECUSDT")
dt = time.time() - t0
print(f"  [{dt:.1f}s] verdict={out.verdict.upper()} conf={out.confidence:.2f}")
print(f"  agent_name={out.agent_name}")
print(f"  reasoning: {out.reasoning}")
print(f"  key_points: {out.key_points}")
print(f"  red_flags: {out.red_flags}")
