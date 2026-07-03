"""Regression tests for llm_trader.resolve — honest paper exit model.

Regression target (code-review 2026-07-02, confirmed by repro): resolve() used
to know only sl/tp/timeout — no liquidation model (scorecard liq_count was
structurally 0 = false safety evidence), funding was fetched as an LLM feature
but never charged as P&L, and stops filled at their exact price with zero
slippage. All three made the paper 'net' structurally optimistic vs real
Binance USDT-M.

These tests drive resolve() through fake market data (no network) and assert
the producer now emits liquidation records, charges funding, and slips stop
fills — i.e. the numbers the scorecard certifies are no longer flattered.
"""
from __future__ import annotations

import json

import pytest

import llm_trader as lt
import llm_trader_scorecard as sc

ENTRY_TS = 1_000_000_000
BAR_MS = 900_000  # 15m


def _pos(symbol="ALTUSDT", side="LONG", entry=100.0, lev=10, margin=10.0,
         sl=95.0, tp=110.0, **extra) -> dict:
    """Open-position record as written by open_positions (pre-fix rows lack
    liq_px/quote_vol_24h on purpose: the fallback path must handle them)."""
    p = {"symbol": symbol, "side": side, "entry": entry,
         "qty": margin * lev / entry, "margin": margin, "leverage": lev,
         "sl": sl, "tp": tp, "entry_ts": ENTRY_TS, "opened_at": "t",
         "regime": "trend", "hour_utc": 3, "rationale": "test"}
    p.update(extra)
    return p


def _bar(i: int, high: float, low: float, close: float) -> dict:
    return {"ts_ms": ENTRY_TS + (i + 1) * BAR_MS, "high": high, "low": low,
            "close": close, "quote_volume": 1000.0}  # tiny -> micro tier


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect all llm_trader state files into tmp (never touch real state)."""
    monkeypatch.setattr(lt, "ACCOUNT", tmp_path / "account.json")
    monkeypatch.setattr(lt, "POSITIONS", tmp_path / "positions.jsonl")
    monkeypatch.setattr(lt, "CLOSED", tmp_path / "closed.jsonl")
    monkeypatch.setattr(lt, "MEMORY", tmp_path / "memory.jsonl")
    return tmp_path


def _run(monkeypatch, position: dict, bars: list[dict],
         funding: list[dict] | None = None) -> dict:
    """Write one position, resolve against fake bars/funding, return closed rec."""
    monkeypatch.setattr(lt.of, "fetch_klines_with_flow",
                        lambda symbol, timeframe, **kw: bars)
    monkeypatch.setattr(lt.of, "fetch_funding_series",
                        lambda symbol, **kw: list(funding or []))
    lt._append(lt.POSITIONS, position)
    now_ms = ENTRY_TS + 40 * BAR_MS
    assert lt.resolve(client=None, now_ms=now_ms) == 1
    rows = [json.loads(x) for x in lt.CLOSED.read_text().splitlines()]
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# (a) forced liquidation exists and outranks SL (pessimistic)
# ---------------------------------------------------------------------------
def test_liquidation_is_emitted_and_nets_full_margin(env, monkeypatch):
    # LONG x10, alt mmr 1% -> liq at 100*(1-0.1+0.01)=91.0; sl=95 sits ABOVE liq.
    # One bar sweeps low=90: touches BOTH sl and liq -> must resolve liquidation.
    rec = _run(monkeypatch, _pos(side="LONG", entry=100.0, lev=10, sl=95.0, tp=110.0),
               bars=[_bar(0, high=100.5, low=90.0, close=92.0)])
    assert rec["reason"] == "liquidation"
    assert rec["exit"] == pytest.approx(91.0)          # liq px, not the sl px
    assert rec["net"] == pytest.approx(-10.0)          # net pinned to -margin
    assert rec["r"] == pytest.approx(-1.0)
    # the scorecard's liq_count is now backed by a real producer
    assert sc.basic_metrics([rec])["liq_count"] == 1
    acct = json.loads(lt.ACCOUNT.read_text())
    assert acct["equity"] == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# (b) funding is charged as a P&L leg on multi-bar holds
# ---------------------------------------------------------------------------
def test_funding_is_charged_on_timeout_hold(env, monkeypatch):
    # LONG x5 (liq 81), sl/tp never touched -> timeout after MAX_HOLD_BARS.
    # One 0.1% funding event inside the hold: LONG pays rate*qty*entry = 0.05.
    qty = 0.5  # margin 10 * lev 5 / entry 100
    bars = [_bar(i, high=101.0, low=99.0, close=100.0) for i in range(lt.MAX_HOLD_BARS + 2)]
    funding = [{"fundingTime": ENTRY_TS + 10_000_000, "fundingRate": 0.001}]
    rec = _run(monkeypatch, _pos(side="LONG", entry=100.0, lev=5, sl=50.0, tp=200.0),
               bars=bars, funding=funding)
    assert rec["reason"] == "timeout"
    assert rec["funding"] == pytest.approx(0.001 * qty * 100.0)  # 0.05, positive = cost
    # funding is IN net (not just recorded): net = gross - fee - funding
    gross = (rec["exit"] - 100.0) * qty
    assert rec["net"] == pytest.approx(gross - rec["fee"] - rec["funding"], abs=1e-3)
    assert rec["net"] < gross - rec["fee"]  # strictly worse than the no-funding net


# ---------------------------------------------------------------------------
# (c) stop-market fills gap through the stop price (slippage)
# ---------------------------------------------------------------------------
def test_sl_fill_slips_through_stop_price(env, monkeypatch):
    # LONG x5 (liq 81, NOT touched), bar low 94 touches sl=95 only. Micro tier
    # stop slippage = (40*3 + 30) bps = 1.5% -> fill 95 * 0.985 = 93.575.
    rec = _run(monkeypatch, _pos(side="LONG", entry=100.0, lev=5, sl=95.0, tp=110.0),
               bars=[_bar(0, high=96.0, low=94.0, close=94.5)])
    assert rec["reason"] == "sl"
    assert rec["exit"] < 95.0                          # never the exact stop px
    assert rec["exit"] == pytest.approx(95.0 * (1 - 0.015))


def test_breakeven_trailing_protects_a_winner():
    """A trade that reaches +1R then reverses must NOT close at a full-stop loss —
    it exits at ~breakeven (the owner's 'won then closed at a loss' fix)."""
    import llm_trader_risk as lr

    def ratchet_exit(side, entry, sl0, tp, bars):
        liq = entry * 0.5 if side == "LONG" else entry * 1.5
        sl, risk, peak, BE = sl0, abs(entry - sl0), entry, 0.0012
        for b in bars:
            hit = lr.exit_check(b, side, liq, sl, tp)
            if hit is not None:
                px, reason = hit
                if reason == "sl" and ((side == "LONG" and sl >= entry) or (side == "SHORT" and sl <= entry)):
                    reason = "trail"
                return reason, px
            if risk > 0:
                if side == "LONG":
                    peak = max(peak, b["high"]); mr = (peak - entry) / risk
                    if mr >= 1.0: sl = max(sl, entry * (1 + BE))
                    if mr >= 2.0: sl = max(sl, peak - risk)
                else:
                    peak = min(peak, b["low"]); mr = (entry - peak) / risk
                    if mr >= 1.0: sl = min(sl, entry * (1 - BE))
                    if mr >= 2.0: sl = min(sl, peak + risk)
        return "open", None
    def bar(o, h, l, c): return {"open": o, "high": h, "low": l, "close": c}

    # LONG +1.5R then reverses -> breakeven exit, not the -2% stop
    r, px = ratchet_exit("LONG", 100, 98, 130, [bar(100, 103, 100, 102), bar(102, 102, 96, 96)])
    assert r == "trail" and px >= 100.0            # exited at/above entry, not 98
    # genuinely wrong entry (drops straight) still takes the full stop
    r2, px2 = ratchet_exit("LONG", 100, 98, 130, [bar(100, 100, 97, 97)])
    assert r2 == "sl" and px2 <= 98.0
