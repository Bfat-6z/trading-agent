"""R2 tests — stage-2 second look + gate semantics. All behavior is behind LLM_TRADER_REDESIGN
(default OFF): with the flag off, _stage2_confirm must be a pure pass-through."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import llm_trader as lt
import llm_trader_triggers as ltt

NOW_MS = 1_800_000_000_000


@pytest.fixture(autouse=True)
def _isolate_heartbeat(monkeypatch, tmp_path):
    """_stage2_confirm writes a mid-cycle heartbeat — redirect it for EVERY test in this module,
    or a pytest run CLOBBERS the production heartbeat file (caught live 2026-07-10: prod hb showed
    'AAAUSDT / phase stage2' test data — a false-fresh heartbeat can mask a dead mission from the
    supervisor). Test isolation from prod state is non-negotiable."""
    monkeypatch.setattr(lt, "HEARTBEAT", tmp_path / "hb_test_isolated.json")


def _bars(n=60, px=100.0):
    out = []
    for i in range(n):
        out.append({"ts_ms": NOW_MS - (n - i) * 900_000, "open": px, "high": px * 1.01,
                    "low": px * 0.99, "close": px, "volume": 10.0, "quote_volume": 1000.0,
                    "taker_buy_base": 5.0, "close_time": NOW_MS - (n - i) * 900_000 + 899_999})
    return out


def _decision(**kw):
    d = {"symbol": "AAAUSDT", "action": "LONG", "leverage": 5, "size_pct": 8.0,
         "sl_pct": 2.0, "tp_pct": 4.0, "entry_px": None, "tf_basis": "15m",
         "rationale": "test", "price": 100.0, "_bars": _bars(), "regime": "trending"}
    d.update(kw)
    return d


def test_redesign_flag_defaults_off():
    # the file-flag flip mechanism: with no env and no state/llm_trader/redesign.flag,
    # REDESIGN must be False (dark). This pins the CURRENT deployed state.
    import os
    assert os.environ.get("LLM_TRADER_REDESIGN", "0") != "1"
    assert not (lt.LT_DIR / "redesign.flag").exists()
    assert lt.REDESIGN is False


def test_stage2_is_passthrough_when_redesign_off(monkeypatch):
    monkeypatch.setattr(lt, "REDESIGN", False)
    ds = [_decision()]
    out = lt._stage2_confirm(ds, client=None, now_ms=NOW_MS)
    assert out is ds and "_stage2" not in out[0]


def test_stage2_confirm_keeps_and_clamps(monkeypatch):
    monkeypatch.setattr(lt, "REDESIGN", True)
    monkeypatch.setattr(lt.smc, "smc_summary", lambda *a, **k: {"summary": {}, "hlines": None})
    monkeypatch.setattr(lt.ltc, "render_chart", lambda *a, **k: "b64")
    monkeypatch.setattr(lt, "_llm_vision",
                        lambda s, t, i: json.dumps({"confirm": True, "reason": "clean retest",
                                                    "sl_pct": 99.0, "tp_pct": 0.01}))
    out = lt._stage2_confirm([_decision()], client=None, now_ms=NOW_MS)
    assert len(out) == 1
    assert out[0]["_stage2"] == "confirmed"
    assert out[0]["sl_pct"] == 8.0    # clamped to the same bound as _validate_decisions
    assert out[0]["tp_pct"] == 0.3
    assert "s2:clean retest" in out[0]["rationale"]


def test_stage2_reject_drops_the_trade(monkeypatch, tmp_path):
    monkeypatch.setattr(lt, "REDESIGN", True)
    monkeypatch.setattr(lt, "LT_DIR", tmp_path)
    monkeypatch.setattr(lt.smc, "smc_summary", lambda *a, **k: {"summary": {}, "hlines": None})
    monkeypatch.setattr(lt.ltc, "render_chart", lambda *a, **k: "b64")
    monkeypatch.setattr(lt, "_llm_vision",
                        lambda s, t, i: json.dumps({"confirm": False, "reason": "structure broke"}))
    out = lt._stage2_confirm([_decision()], client=None, now_ms=NOW_MS)
    assert out == []
    gov = [json.loads(l) for l in (tmp_path / "governance.jsonl").read_text(encoding="utf-8").splitlines()]
    assert gov[-1]["event"] == "stage2_reject"


def test_stage2_null_sl_tp_keeps_stage1_values(monkeypatch):
    monkeypatch.setattr(lt, "REDESIGN", True)
    monkeypatch.setattr(lt.smc, "smc_summary", lambda *a, **k: {"summary": {}, "hlines": None})
    monkeypatch.setattr(lt.ltc, "render_chart", lambda *a, **k: "b64")
    monkeypatch.setattr(lt, "_llm_vision",
                        lambda s, t, i: json.dumps({"confirm": True, "reason": "ok",
                                                    "sl_pct": None, "tp_pct": None}))
    out = lt._stage2_confirm([_decision(sl_pct=2.5, tp_pct=5.0)], client=None, now_ms=NOW_MS)
    assert out[0]["sl_pct"] == 2.5 and out[0]["tp_pct"] == 5.0


def test_stage2_technical_failure_is_passthrough_not_block(monkeypatch, tmp_path):
    monkeypatch.setattr(lt, "REDESIGN", True)
    monkeypatch.setattr(lt, "LT_DIR", tmp_path)
    monkeypatch.setattr(lt.smc, "smc_summary", lambda *a, **k: {"summary": {}, "hlines": None})
    monkeypatch.setattr(lt.ltc, "render_chart", lambda *a, **k: "b64")
    monkeypatch.setattr(lt, "_llm_vision", lambda s, t, i: (_ for _ in ()).throw(RuntimeError("api down")))
    out = lt._stage2_confirm([_decision()], client=None, now_ms=NOW_MS)
    assert len(out) == 1 and out[0]["_stage2"] == "error_passthrough"
    gov = [json.loads(l) for l in (tmp_path / "governance.jsonl").read_text(encoding="utf-8").splitlines()]
    assert gov[-1]["event"] == "stage2_error_passthrough"


def test_stage2_budget_bound(monkeypatch):
    monkeypatch.setattr(lt, "REDESIGN", True)
    monkeypatch.setattr(lt, "STAGE2_MAX", 1)
    monkeypatch.setattr(lt.smc, "smc_summary", lambda *a, **k: {"summary": {}, "hlines": None})
    monkeypatch.setattr(lt.ltc, "render_chart", lambda *a, **k: "b64")
    monkeypatch.setattr(lt, "_llm_vision",
                        lambda s, t, i: json.dumps({"confirm": True, "reason": "ok"}))
    out = lt._stage2_confirm([_decision(), _decision(symbol="BBBUSDT")], client=None, now_ms=NOW_MS)
    assert out[0]["_stage2"] == "confirmed"
    assert out[1]["_stage2"] == "skipped_budget"   # over budget passes through, visibly tagged


def test_trigger_log_rotation(tmp_path):
    p = tmp_path / "trigger_log.jsonl"
    p.write_text("x" * 16_000_000, encoding="utf-8")   # over the 15MB cap
    ltt.log_cycle(p, NOW_MS, {}, 10)
    rotated = tmp_path / "trigger_log.jsonl.1"
    assert rotated.exists() and rotated.stat().st_size > 15_000_000
    assert p.stat().st_size < 1000                      # fresh file holds just the new line
    assert json.loads(p.read_text(encoding="utf-8").strip())["n_ctx"] == 10
