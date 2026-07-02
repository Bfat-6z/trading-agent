"""Tests for llm_trader_memory (plan 260702, checklist #10/#11, acceptance #6).

Run: cd E:\\keo-moi-mail\\trading-agent && venv\\Scripts\\python.exe -m pytest tests\\test_llm_trader_memory.py -q

All grouping numbers in HAND8 are verified by hand in the comments — the point
of these tests is that a human re-derived n/wins/win_rate/mean_r per group,
not that the code agrees with itself.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llm_trader_memory as ltm


def _t(symbol, side, regime, hour, lev, net, r, **kw):
    row = {"symbol": symbol, "side": side, "regime": regime, "hour_utc": hour,
           "entry": 100.0, "exit": 100.0, "reason": "sl", "net": net, "r": r,
           "leverage": lev, "rationale": "ctx", "closed_ts": 1_760_000_000_000}
    row.update(kw)
    return row


# 8 hand-built trades (see per-group derivations in the tests below).
HAND8 = [
    _t("SOLUSDT", "SHORT", "choppy",   2,  5, -1.0, -1.0),   # 1
    _t("SOLUSDT", "SHORT", "choppy",   5,  5, -0.5, -0.5),   # 2
    _t("SOLUSDT", "SHORT", "trending", 6, 10,  2.0,  1.0),   # 3
    _t("SOLUSDT", "LONG",  "trending", 13, 10,  1.5,  0.8),  # 4
    _t("BTCUSDT", "LONG",  "trending", 23,  5,  1.0,  0.5),  # 5
    _t("BTCUSDT", "LONG",  "choppy",   18,  5, -0.8, -0.4),  # 6
    _t("ETHUSDT", "SHORT", "mixed",    12, 10,  0.6,  0.3),  # 7
    _t("ETHUSDT", "SHORT", "mixed",     7, 10, -0.6, -0.3),  # 8
]


def synthetic_closed(n=100):
    """Deterministic 100-trade fixture with realistic-length rationales."""
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "LINKUSDT"]
    regimes = ["trending", "choppy", "mixed"]
    reasons = ["sl", "tp", "timeout"]
    rationales = [
        "funding flipped positive while CVD kept falling on the 15m",
        "clean breakout retest with ADX rising and efficiency above 0.4",
        "chop range fade at upper band, expecting mean reversion",
        "momentum continuation after squeeze, trend up on EMAs",
    ]
    rows = []
    for i in range(n):
        r = round((i % 7 - 3) * 0.45 + (0.1 if i % 2 else -0.05), 3)
        rows.append({
            "symbol": syms[i % len(syms)],
            "side": "LONG" if i % 3 else "SHORT",
            "regime": regimes[i % 3],
            "hour_utc": (i * 5) % 24,
            "entry": 100.0 + i, "exit": 100.0 + i + r,
            "reason": reasons[i % 3],
            "net": round(r * 5.0, 4), "r": r,
            "leverage": 5 if i % 2 else 10,
            "rationale": rationales[i % 4] + f" #{i}",
            "closed_ts": 1_760_000_000_000 + i * 900_000,
        })
    return rows


# ---------------------------------------------------------------------------
# aggregate_stats: grouping correctness, hand-verified
# ---------------------------------------------------------------------------
def test_by_symbol_hand_verified():
    s = ltm.aggregate_stats(HAND8)["by_symbol"]
    # SOL: trades 1-4, wins {+2.0,+1.5}, mean r (-1-0.5+1+0.8)/4=0.075, net 2.0
    assert s["SOLUSDT"] == {"n": 4, "wins": 2, "win_rate": 0.5,
                            "mean_r": 0.075, "total_net": 2.0}
    # BTC: trades 5-6, mean r (0.5-0.4)/2=0.05, net 0.2
    assert s["BTCUSDT"] == {"n": 2, "wins": 1, "win_rate": 0.5,
                            "mean_r": 0.05, "total_net": 0.2}
    # ETH: trades 7-8, mean r (0.3-0.3)/2=0, net 0
    assert s["ETHUSDT"] == {"n": 2, "wins": 1, "win_rate": 0.5,
                            "mean_r": 0.0, "total_net": 0.0}


def test_by_regime_hand_verified():
    s = ltm.aggregate_stats(HAND8)["by_regime"]
    # choppy: trades 1,2,6 all losses; mean r -1.9/3=-0.633; net -2.3
    assert s["choppy"] == {"n": 3, "wins": 0, "win_rate": 0.0,
                           "mean_r": -0.633, "total_net": -2.3}
    # trending: trades 3,4,5 all wins; mean r 2.3/3=0.767; net 4.5
    assert s["trending"] == {"n": 3, "wins": 3, "win_rate": 1.0,
                             "mean_r": 0.767, "total_net": 4.5}
    # mixed: trades 7,8; mean r 0; net 0
    assert s["mixed"] == {"n": 2, "wins": 1, "win_rate": 0.5,
                          "mean_r": 0.0, "total_net": 0.0}


def test_by_hour_bucket_hand_verified():
    s = ltm.aggregate_stats(HAND8)["by_hour_bucket"]
    # hours 2,5 -> 0-5 (trades 1,2): mean r -0.75, net -1.5
    assert s["0-5"] == {"n": 2, "wins": 0, "win_rate": 0.0,
                        "mean_r": -0.75, "total_net": -1.5}
    # hours 6,7 -> 6-11 (trades 3,8): mean r (1.0-0.3)/2=0.35, net 1.4
    assert s["6-11"] == {"n": 2, "wins": 1, "win_rate": 0.5,
                         "mean_r": 0.35, "total_net": 1.4}
    # hours 13,12 -> 12-17 (trades 4,7): mean r (0.8+0.3)/2=0.55, net 2.1
    assert s["12-17"] == {"n": 2, "wins": 2, "win_rate": 1.0,
                          "mean_r": 0.55, "total_net": 2.1}
    # hours 23,18 -> 18-23 (trades 5,6): mean r 0.05, net 0.2
    assert s["18-23"] == {"n": 2, "wins": 1, "win_rate": 0.5,
                          "mean_r": 0.05, "total_net": 0.2}


def test_by_side_hand_verified():
    s = ltm.aggregate_stats(HAND8)["by_side"]
    # SHORT: trades 1,2,3,7,8; wins {+2.0,+0.6}; mean r -0.5/5=-0.1; net 0.5
    assert s["SHORT"] == {"n": 5, "wins": 2, "win_rate": 0.4,
                          "mean_r": -0.1, "total_net": 0.5}
    # LONG: trades 4,5,6; wins {+1.5,+1.0}; mean r 0.9/3=0.3; net 1.7
    assert s["LONG"] == {"n": 3, "wins": 2, "win_rate": 0.667,
                         "mean_r": 0.3, "total_net": 1.7}


def test_by_leverage_hand_verified():
    s = ltm.aggregate_stats(HAND8)["by_leverage"]
    # x5: trades 1,2,5,6; win {+1.0}; mean r -1.4/4=-0.35; net -1.3
    assert s["x5"] == {"n": 4, "wins": 1, "win_rate": 0.25,
                       "mean_r": -0.35, "total_net": -1.3}
    # x10: trades 3,4,7,8; wins {+2.0,+1.5,+0.6}; mean r 1.8/4=0.45; net 3.5
    assert s["x10"] == {"n": 4, "wins": 3, "win_rate": 0.75,
                        "mean_r": 0.45, "total_net": 3.5}


def test_by_symbol_side_hand_verified():
    s = ltm.aggregate_stats(HAND8)["by_symbol_side"]
    # SOL SHORT: trades 1,2,3; win {+2.0}; mean r -0.5/3=-0.167; net 0.5
    assert s["SOLUSDT SHORT"] == {"n": 3, "wins": 1, "win_rate": 0.333,
                                  "mean_r": -0.167, "total_net": 0.5}
    assert s["BTCUSDT LONG"] == {"n": 2, "wins": 1, "win_rate": 0.5,
                                 "mean_r": 0.05, "total_net": 0.2}
    assert s["ETHUSDT SHORT"] == {"n": 2, "wins": 1, "win_rate": 0.5,
                                  "mean_r": 0.0, "total_net": 0.0}


def test_n_at_least_2_filter():
    # SOLUSDT LONG appears exactly once in HAND8 -> filtered from symbol_side.
    s = ltm.aggregate_stats(HAND8)
    assert "SOLUSDT LONG" not in s["by_symbol_side"]
    # Standalone: 2x ADA + 1x DOT -> DOT (n=1) dropped from every grouping.
    rows = [_t("ADAUSDT", "LONG", "trending", 1, 5, 1.0, 0.5),
            _t("ADAUSDT", "LONG", "trending", 2, 5, -1.0, -0.5),
            _t("DOTUSDT", "SHORT", "choppy", 9, 10, 1.0, 0.5)]
    s2 = ltm.aggregate_stats(rows)
    assert set(s2["by_symbol"]) == {"ADAUSDT"}
    assert set(s2["by_symbol_side"]) == {"ADAUSDT LONG"}
    assert set(s2["by_regime"]) == {"trending"}
    assert set(s2["by_hour_bucket"]) == {"0-5"}
    assert set(s2["by_side"]) == {"LONG"}
    assert set(s2["by_leverage"]) == {"x5"}


def test_hour_bucket_edges():
    # Direct helper: the plan's exact edge cases.
    assert ltm.hour_bucket(5) == "0-5"
    assert ltm.hour_bucket(6) == "6-11"
    assert ltm.hour_bucket(23) == "18-23"
    assert ltm.hour_bucket(0) == "0-5"
    assert ltm.hour_bucket(11) == "6-11"
    assert ltm.hour_bucket(12) == "12-17"
    assert ltm.hour_bucket(17) == "12-17"
    assert ltm.hour_bucket(18) == "18-23"
    for bad in (24, -1, None, "abc", float("nan")):
        assert ltm.hour_bucket(bad) is None
    # And through aggregate_stats (pairs so the n>=2 filter keeps them).
    rows = [_t("AUSDT", "LONG", "mixed", h, 5, 1.0, 0.5)
            for h in (5, 5, 6, 6, 23, 23)]
    buckets = ltm.aggregate_stats(rows)["by_hour_bucket"]
    assert set(buckets) == {"0-5", "6-11", "18-23"}
    assert all(g["n"] == 2 for g in buckets.values())


def test_malformed_rows_skipped_never_raise():
    rows = [
        "garbage", None, 42, [], {},                                # not rows
        {"symbol": "XUSDT", "side": "LONG"},                        # no r/net
        {"symbol": "YUSDT", "side": "SHORT", "r": "abc", "net": 1}, # bad r
        {"symbol": "ZUSDT", "side": "LONG", "r": float("nan"), "net": 2.0},
        {"symbol": "WUSDT", "side": "SHORT", "r": 0.5, "net": None},
        # valid rows, but with bad/missing per-grouping keys:
        {"symbol": "OKUSDT", "side": "LONG", "r": 0.5, "net": 1.0},
        {"symbol": "OKUSDT", "side": "LONG", "r": 0.7, "net": 1.4,
         "hour_utc": 99, "regime": "", "leverage": 0},
    ]
    s = ltm.aggregate_stats(rows)
    assert s["by_symbol"] == {"OKUSDT": {"n": 2, "wins": 2, "win_rate": 1.0,
                                         "mean_r": 0.6, "total_net": 2.4}}
    assert s["by_regime"] == {}          # "" regime is invalid
    assert s["by_hour_bucket"] == {}     # hour 99 out of range, other missing
    assert s["by_leverage"] == {}        # leverage 0 invalid, other missing
    assert set(s["by_side"]) == {"LONG"}
    assert set(s["by_symbol_side"]) == {"OKUSDT LONG"}
    # The other entry points must also survive the same garbage.
    assert isinstance(ltm.recent_trades(rows), list)
    assert isinstance(ltm.build_memory_context(rows), dict)
    assert ltm.distill_lessons("not a dict") == []


# ---------------------------------------------------------------------------
# distill_lessons: acceptance criterion #6 + weighting/caps
# ---------------------------------------------------------------------------
def test_lessons_have_counts_and_no_prescriptive_words():
    # Acceptance #6: lessons contain W/L counts, no "never"-style bans.
    for fixture in (HAND8, synthetic_closed(100)):
        lessons = ltm.distill_lessons(ltm.aggregate_stats(fixture))
        assert lessons, "expected at least one lesson"
        for lesson in lessons:
            assert re.search(r"\d+W/\d+L", lesson), lesson
            assert "mean" in lesson and lesson.endswith("R"), lesson
            low = lesson.lower()
            assert "never" not in low, lesson
            assert "always avoid" not in low, lesson
            assert "avoid" not in low and "ban" not in low, lesson


def test_lessons_sorted_by_evidence_weight():
    lessons = ltm.distill_lessons(ltm.aggregate_stats(HAND8), min_n=3)
    # Weights |mean_r|*n: trending 0.767*3=2.301 > choppy 0.633*3=1.899
    # > x10 0.45*4=1.8 > x5 0.35*4=1.4 (hand-derived from the stats tests).
    assert lessons[0] == "trending regime: 3W/0L, mean +0.77R"
    assert lessons[1] == "choppy regime: 0W/3L, mean -0.63R"
    assert lessons[2] == "x10 leverage: 3W/1L, mean +0.45R"
    assert lessons[3] == "x5 leverage: 1W/3L, mean -0.35R"
    # Plan's example phrasing for a symbol+side group is present.
    assert "SOLUSDT SHORT: 1W/2L, mean -0.17R" in lessons


def test_lessons_min_n_filter():
    # min_n=3 drops every hour bucket (all n=2 in HAND8)...
    lessons3 = ltm.distill_lessons(ltm.aggregate_stats(HAND8), min_n=3)
    assert not any("h UTC" in ln for ln in lessons3)
    assert len(lessons3) == 8  # trending/choppy, x5/x10, LONG/SHORT, SOL, SOL-SHORT
    # ...min_n=2 lets them through.
    lessons2 = ltm.distill_lessons(ltm.aggregate_stats(HAND8), min_n=2)
    assert "12-17h UTC: 2W/0L, mean +0.55R" in lessons2
    assert "0-5h UTC: 0W/2L, mean -0.75R" in lessons2


def test_lessons_max_lines_cap():
    stats = ltm.aggregate_stats(HAND8)
    capped = ltm.distill_lessons(stats, min_n=3, max_lines=3)
    assert capped == ["trending regime: 3W/0L, mean +0.77R",
                      "choppy regime: 0W/3L, mean -0.63R",
                      "x10 leverage: 3W/1L, mean +0.45R"]
    assert ltm.distill_lessons(stats, max_lines=0) == []
    big = ltm.distill_lessons(ltm.aggregate_stats(synthetic_closed(100)),
                              max_lines=12)
    assert len(big) == 12


# ---------------------------------------------------------------------------
# recent_trades
# ---------------------------------------------------------------------------
def _recent_fixture():
    rows = []
    for i in range(15):
        rows.append({"symbol": f"S{i}USDT", "side": "LONG" if i % 2 == 0 else "SHORT",
                     "regime": "trending", "hour_utc": i % 24, "r": round(0.1 * i, 3),
                     "net": 1.0, "reason": "tp", "rationale": f"why {i}",
                     "closed_ts": 1_760_000_000_000 + i})
    del rows[2]["symbol"]                 # malformed: must not consume a slot
    rows[14]["rationale"] = "x" * 300     # must be truncated to 120
    return rows


def test_recent_trades_last_k_and_shape():
    out = ltm.recent_trades(_recent_fixture(), k=10)
    assert len(out) == 10
    # 14 valid rows (index 2 dropped) -> last 10 start at original index 5.
    assert out[0]["symbol"] == "S5USDT"
    assert out[-1]["symbol"] == "S14USDT"
    for row in out:
        assert set(row) == {"symbol", "side", "regime", "hour", "r",
                            "reason", "rationale"}
    assert out[0] == {"symbol": "S5USDT", "side": "SHORT", "regime": "trending",
                      "hour": 5, "r": 0.5, "reason": "tp", "rationale": "why 5"}


def test_recent_trades_rationale_truncated_120():
    out = ltm.recent_trades(_recent_fixture(), k=10)
    assert out[-1]["rationale"] == "x" * 120
    assert len(out[-1]["rationale"]) == 120


def test_recent_trades_k_variants():
    rows = _recent_fixture()
    assert len(ltm.recent_trades(rows, k=3)) == 3
    assert ltm.recent_trades(rows, k=0) == []
    assert len(ltm.recent_trades(rows, k=100)) == 14  # all valid rows
    assert len(ltm.recent_trades(rows)) == 10         # default k=10


def test_recent_trades_tolerates_missing_optional_keys():
    rows = [{"symbol": "AUSDT", "side": "LONG"}]  # no r/regime/hour/reason
    out = ltm.recent_trades(rows, k=5)
    assert out == [{"symbol": "AUSDT", "side": "LONG", "regime": None,
                    "hour": None, "r": None, "reason": None, "rationale": ""}]


# ---------------------------------------------------------------------------
# build_memory_context: compactness on 100 synthetic trades
# ---------------------------------------------------------------------------
def test_build_memory_context_compact_under_2600_chars():
    ctx = ltm.build_memory_context(synthetic_closed(100))
    js = json.dumps(ctx, default=str)
    assert len(js) <= 2600, f"context too large: {len(js)} chars"
    assert len(json.dumps(ctx, separators=(",", ":"), default=str)) <= 2600


def test_build_memory_context_structure():
    ctx = ltm.build_memory_context(synthetic_closed(100))
    assert set(ctx) == {"stats", "lessons", "recent"}
    assert set(ctx["stats"]) == {"by_symbol_side", "by_regime", "by_hour_bucket"}
    for grouping, groups in ctx["stats"].items():
        assert groups, grouping
        for g in groups.values():
            assert set(g) == {"n", "mean_r"}
    assert 0 < len(ctx["lessons"]) <= 12
    assert len(ctx["recent"]) == 10
    for row in ctx["recent"]:
        assert len(row["rationale"]) <= 60  # tighter than recent_trades' 120


def test_empty_and_tiny_inputs_never_raise():
    assert ltm.aggregate_stats([]) == {g: {} for g in (
        "by_symbol", "by_regime", "by_hour_bucket", "by_side",
        "by_leverage", "by_symbol_side")}
    assert ltm.distill_lessons({}) == []
    assert ltm.recent_trades([]) == []
    ctx = ltm.build_memory_context([])
    assert ctx["lessons"] == [] and ctx["recent"] == []
    assert all(groups == {} for groups in ctx["stats"].values())
    # One lonely trade: everything filtered, still valid + compact.
    one = ltm.build_memory_context(HAND8[:1])
    assert one["recent"] and one["lessons"] == []


# ---------------------------------------------------------------------------
# REGRESSION (review 2026-07-02, "dead learning"): this module shipped with a
# green suite while llm_trader.decide() still injected only the last-8 raw
# memory rows — build_memory_context was never on the executed code path, the
# exact failure mode the NeuroCore audit flagged. These tests pin the wiring:
# decide()'s LLM prompt must carry EXACTLY this module's context, so the
# integration cannot silently disappear again.
# ---------------------------------------------------------------------------
def _decide_market_ctx(symbol="SOLUSDT"):
    """Minimal build_context-shaped row for exercising llm_trader.decide()."""
    return [{"symbol": symbol, "price": 100.0, "last8_closes": [100.0] * 8,
             "ret20_pct": 1.2, "trend": "up", "adx": 28.0, "efficiency": 0.41,
             "regime": "trending", "atr_pct": 1.1, "funding_rate": 0.0001,
             "cvd_norm": 0.2, "atr": 1.1, "_quote_vol_24h": 3e9,
             "_ts": 1_760_000_000_000}]


def _capture_llm(monkeypatch, lt, reply="[]"):
    captured = {}

    def fake_llm(system, user):
        captured["system"], captured["user"] = system, user
        return reply

    monkeypatch.setattr(lt, "_llm", fake_llm)
    return captured


def test_decide_prompt_carries_build_memory_context(tmp_path, monkeypatch):
    import llm_trader as lt
    closed = tmp_path / "closed.jsonl"
    closed.write_text("".join(json.dumps(r) + "\n" for r in HAND8),
                      encoding="utf-8")
    monkeypatch.setattr(lt, "CLOSED", closed)
    captured = _capture_llm(monkeypatch, lt)
    assert lt.decide(_decide_market_ctx(), 100.0) == []
    sent = json.loads(captured["user"])
    # The injected block is EXACTLY this module's distilled context over ALL
    # closed trades — not the old 8-raw-rows relevant_lessons payload.
    assert sent["memory"] == ltm.build_memory_context(HAND8)
    assert "SOLUSDT SHORT: 1W/2L, mean -0.17R" in sent["memory"]["lessons"]
    assert all("your_past_outcomes" not in coin for coin in sent["coins"])
    # System prompt must tell the LLM the memory exists and stays contextual
    # (checklist #12: evidence to weigh, never a blanket ban).
    assert "MEMORY" in captured["system"]
    assert "never" not in captured["system"].split("blanket-ban")[0].lower()


def test_decide_memory_fail_open_without_history(tmp_path, monkeypatch):
    # No closed.jsonl yet (fresh account): decide must still run and inject
    # an empty-but-valid memory block, not crash the loop.
    import llm_trader as lt
    monkeypatch.setattr(lt, "CLOSED", tmp_path / "missing.jsonl")
    captured = _capture_llm(monkeypatch, lt)
    assert lt.decide(_decide_market_ctx(), 100.0) == []
    sent = json.loads(captured["user"])
    assert sent["memory"] == ltm.build_memory_context([])


def test_decide_rule_enforcement_survives_memory_wiring(tmp_path, monkeypatch):
    # The wiring change must not break decision validation (x5/x10, 5-10%).
    import llm_trader as lt
    monkeypatch.setattr(lt, "CLOSED", tmp_path / "missing.jsonl")
    reply = json.dumps([{"symbol": "SOLUSDT", "action": "LONG", "leverage": 7,
                         "size_pct": 50, "sl_pct": 2, "tp_pct": 3,
                         "rationale": "test"}])
    _capture_llm(monkeypatch, lt, reply=reply)
    out = lt.decide(_decide_market_ctx(), 100.0)
    assert len(out) == 1
    assert out[0]["leverage"] == 5 and out[0]["size_pct"] == 10.0


def test_llm_trader_source_wires_memory_module():
    # Static pin (cheap, survives refactors of the runtime tests): llm_trader
    # must import this module and route decide() through build_memory_context;
    # the legacy raw-rows helper may remain defined but nothing may call it.
    src = (ROOT / "llm_trader.py").read_text(encoding="utf-8")
    assert "import llm_trader_memory" in src
    assert "build_memory_context" in src
    assert "your_past_outcomes" not in src
    assert src.count("relevant_lessons(") == 1  # the def only, zero call sites
