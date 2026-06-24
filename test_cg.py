from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.data import _coingecko_caps
for s in ["PENGU", "NEIRO", "BTC", "ETH"]:
    f, m = _coingecko_caps(s)
    print(f"{s:8s} FDV=${f/1e6:>10.1f}M  MC=${m/1e6:>10.1f}M")
