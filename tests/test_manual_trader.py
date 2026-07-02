"""manual_trader — scale-out math + paper-only tripwire. The stateful resolve
paths are verified empirically (real placement); here we lock the money math and
the never-go-live guarantee."""
from __future__ import annotations

import re
from pathlib import Path

import manual_trader as mt

ROOT = Path(__file__).resolve().parents[1]


def test_scale_price_math():
    # +100% profit at x10 = a 10% favourable move
    assert abs(mt._scale_price("LONG", 100.0, 10, 100) - 110.0) < 1e-9
    assert abs(mt._scale_price("SHORT", 100.0, 10, 100) - 90.0) < 1e-9
    # +50% at x5 = a 10% move
    assert abs(mt._scale_price("LONG", 200.0, 5, 50) - 220.0) < 1e-9
    assert abs(mt._scale_price("SHORT", 200.0, 5, 50) - 180.0) < 1e-9


def test_paper_only_no_live_calls():
    text = (ROOT / "manual_trader.py").read_text(encoding="utf-8")
    call = re.compile(r"\.\s*(futures_create_order|create_order|futures_change_leverage|"
                      r"futures_cancel_order|transfer)\s*\(")
    m = call.search(text)
    assert m is None, f"manual_trader has a live-order call: {m.group(0) if m else ''}"


def test_no_live_env_assignment():
    pat = re.compile(r"ALLOW_LIVE_ORDERS[\"']?\s*\]?\s*=")
    for i, line in enumerate((ROOT / "manual_trader.py").read_text(encoding="utf-8").splitlines(), 1):
        assert not pat.search(line), f"manual_trader.py:{i} assigns ALLOW_LIVE_ORDERS"


def test_separate_account_dir():
    # must not share the llm_trader account (keeps the LLM scorecard clean)
    assert mt.MT.name == "manual_trader"
    assert "llm_trader" not in str(mt.ACCOUNT)
