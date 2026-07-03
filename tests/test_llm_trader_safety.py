"""Static safety net (plan 260702 #17): the LLM trader family must stay
paper-only forever. These greps are the tripwire — if any llm_trader* source
ever gains a live-order call or flips the live-orders env var, this fails the
suite BEFORE the code can run."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted(ROOT.glob("llm_trader*.py"))


def test_llm_trader_files_exist():
    names = {p.name for p in SOURCES}
    assert {"llm_trader.py", "llm_trader_risk.py",
            "llm_trader_scorecard.py", "llm_trader_memory.py"} <= names


def test_no_live_order_calls():
    # Match actual CALL syntax (attribute access + open paren) so prose in
    # docstrings that *mentions* the API by name doesn't trip the wire.
    call = re.compile(r"\.\s*(futures_create_order|create_order|futures_change_leverage|"
                      r"futures_cancel_order|transfer)\s*\(")
    for src in SOURCES:
        text = src.read_text(encoding="utf-8")
        m = call.search(text)
        assert m is None, f"{src.name} contains live-order call: {m.group(0)!r}"


def test_no_live_env_assignment():
    # Assignment (enabling live) is banned; merely *reading* the var is fine.
    pat = re.compile(r"ALLOW_LIVE_ORDERS[\"']?\s*\]?\s*=")
    for src in SOURCES:
        for i, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
            assert not pat.search(line), f"{src.name}:{i} assigns ALLOW_LIVE_ORDERS"


def test_owner_rules_pinned():
    """Owner's hard rules stay in code: 5-10% sizing, x5/x10 only."""
    text = (ROOT / "llm_trader.py").read_text(encoding="utf-8")
    assert "SIZE_PCT_MIN, SIZE_PCT_MAX = 5.0, 10.0" in text
    assert "ALLOWED_LEVERAGE = (5, 10)" in text


def test_split_thinking_handles_brackets_and_delimiter():
    import llm_trader as l
    raw = 'THINKING:\nBTC [strong] 3 conf [ok]\n===DECISIONS===\n[{"symbol":"BTCUSDT","action":"LONG"}]'
    out = l._split_thinking(raw)
    assert isinstance(out, list) and out[0]["symbol"] == "BTCUSDT"   # array parsed despite [ ] in thinking
    # no delimiter -> whole text extraction still works
    assert l._split_thinking('[{"symbol":"ETHUSDT"}]')[0]["symbol"] == "ETHUSDT"
    assert l._split_thinking(None) is None


def test_structure_sl_and_limit_entry():
    import llm_trader as l
    # structure SL: stop beyond the support zone; far resistance -> zone TP, R:R>=1.5
    d = {"_smc": {"nearest_support": {"lo": 97, "hi": 98}, "nearest_resistance": {"lo": 107, "hi": 108},
                  "invalidation": 96}, "atr": 1.0, "sl_pct": 2, "tp_pct": 3}
    sl, tp = l._structure_sl_tp("LONG", 100, d)
    assert sl < 97 and (tp - 100) / (100 - sl) >= 1.5      # below support, >=1.5 R:R
    # a NEAR opposing zone (<1.5R) must SKIP — never synthesize a TP through it
    d2 = {"_smc": {"nearest_support": {"lo": 97, "hi": 98}, "nearest_resistance": {"lo": 103, "hi": 104},
                   "invalidation": 96}, "atr": 1.0, "sl_pct": 2, "tp_pct": 3}
    assert l._structure_sl_tp("LONG", 100, d2) == (None, None)
    # structure further than 6% -> NOT clamped into the zone; falls back to LLM %
    d3 = {"_smc": {"nearest_support": {"lo": 90, "hi": 93}, "nearest_resistance": None},
          "atr": 1.0, "sl_pct": 2, "tp_pct": 3}
    sl3, _ = l._structure_sl_tp("LONG", 100, d3)
    assert abs(sl3 - 98.0) < 1e-6                           # LLM fallback, not 94-inside-zone
    # no structure -> falls back to the LLM %
    sl2, _ = l._structure_sl_tp("LONG", 100, {"sl_pct": 2, "tp_pct": 3})
    assert abs(sl2 - 98.0) < 1e-6
    # entry_px: keep a favorable pullback limit, drop a FOMO (wrong-side) one
    by = {"X": {"symbol": "X", "price": 100.0}}
    keep = l._validate_decisions([{"symbol": "X", "action": "LONG", "entry_px": 98,
                                   "leverage": 10, "size_pct": 5, "sl_pct": 2, "tp_pct": 3}], by)
    assert keep[0]["entry_px"] == 98.0
    drop = l._validate_decisions([{"symbol": "X", "action": "LONG", "entry_px": 103,
                                   "leverage": 10, "size_pct": 5, "sl_pct": 2, "tp_pct": 3}], by)
    assert drop[0]["entry_px"] is None                     # above price for a long = FOMO -> market
    # chase gate judges the EFFECTIVE entry: extended RSI68 market = blocked,
    # but the same coin with a pullback LIMIT at the EMA passes (audit fix)
    by2 = {"C": {"symbol": "C", "price": 100.0, "rsi14": 68, "px_vs_ema20_pct": 1.6}}
    blocked = l._validate_decisions([{"symbol": "C", "action": "LONG",
                                      "leverage": 5, "size_pct": 5, "sl_pct": 2, "tp_pct": 3}], by2)
    assert blocked == []
    limit_ok = l._validate_decisions([{"symbol": "C", "action": "LONG", "entry_px": 98.4,
                                       "leverage": 5, "size_pct": 5, "sl_pct": 2, "tp_pct": 3}], by2)
    assert limit_ok and limit_ok[0]["entry_px"] == 98.4
