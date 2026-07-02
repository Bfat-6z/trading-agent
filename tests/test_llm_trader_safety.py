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
