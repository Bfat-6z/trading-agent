# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
c = spot_client()
pos = c.futures_position_information(symbol="MAGMAUSDT")[0]
for k, v in pos.items():
    print(f"  {k}: {v}")
