"""Canonical NON-CRYPTO exclusion set — ONE source of truth.

Binance lists tokenized stocks / commodities / leveraged-ETFs / stablecoins as USDT perps
(NVDAUSDT, XAUUSDT, MUUSDT, CRCLUSDT...). A 15m crypto scalper has no business there:
RTH-only moves, weekend gaps, near-zero ATR on pegged gold, structural equity up-drift that
makes crypto-style momentum/breakdown theses fail. The 2026-07-11 lane forensic found ~50% of
the lane farm's losses came from exactly these — because lane_farm.py had NO exclusion filter at
all, while the mission and shadow eval each kept their OWN drifting copy of the list.

This module is the canonical list for the EXECUTION + MEASUREMENT pipeline and the A+ scanner
family, so those copies can't diverge again. Consumers that import it:
  llm_trader (mission), shadow_trigger_eval, lane_farm, futures_watch,
  aplus_scan, aplus_5crit_scan, chart_scan, deep_scan.
NOT migrated (different intent — they exclude stables/majors/wrapped-ETH, not stock perps, by
design): market_observer, scan_binance, scan_and_analyze, watch_mode, scan_now, scan_short.

Verified against brain.db lane autopsies 2026-07-11: every base below is a stock/commodity/ETF/
stable perp that actually traded and bled. MU (Micron) and CRCL (Circle, NYSE) are unambiguous
listed-company perps. DRAM is a memory-chip thematic perp (traded 132x alongside SKHYNIX/SNDK/MU,
stock-like mean-reversion, no known crypto by that name) — if a real DRAM token ever lists, revisit.

DO NOT add real crypto that merely LOOKS like a ticker: SKL (SKALE), VVV (Venice.ai) stay IN
the tradable universe — a past bug wrongly excluded them. (VVV was also dropped from futures_watch
here; the Binance VVVUSDT underlying is Venice.ai, not NYSE:VVV/Valvoline.)
"""
from __future__ import annotations

NON_CRYPTO: frozenset = frozenset({
    # metals / commodity ETFs
    "XAU", "XAG", "PAXG", "GLD", "SLV", "USO", "TLT",
    # mega-cap / tech / semiconductor stocks
    "NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "GOOG", "META", "AMZN", "NFLX",
    "INTC", "AMD", "INTU", "CRM", "ORCL", "DIS",
    "MU", "MRVL", "DRAM", "SKHYNIX", "SAMSUNG", "SNDK",        # memory/semis (MU, DRAM new 07-11)
    # consumer / finance / industrial
    "JPM", "BAC", "V", "MA", "KO", "PEP", "WMT", "MCD", "HD", "NKE", "BA", "GE", "F", "GM",
    # ETFs / indices / leveraged
    "SOXL", "SOXX", "QQQ", "SPY", "IWM", "INX", "TQQQ", "SQQQ", "UVXY", "EWY",
    # crypto-adjacent equities (the stock, not the token)
    "MSTR", "COIN", "HOOD", "RIOT", "MARA", "SQ", "SPCX", "CRCL",   # CRCL (Circle) new 07-11
    # stablecoins / fiat
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR",
})


def base(sym: str) -> str:
    """Strip the USDT quote to get the base-asset ticker (BTCUSDT -> BTC)."""
    s = str(sym).upper()
    return s[:-4] if s.endswith("USDT") else s


def is_non_crypto(sym: str) -> bool:
    """True if `sym` is a tokenized stock/commodity/ETF/stable perp that must be excluded."""
    return base(sym) in NON_CRYPTO
