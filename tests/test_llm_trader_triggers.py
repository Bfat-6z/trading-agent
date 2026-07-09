"""R1 trigger engine tests — dark measurement must be correct AND unbreakable (fail-soft)."""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm_trader_triggers as ltt
from llm_trader_learning import _trigger_key

NOW_MS = 1_800_000_000_000


def _iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def _ctx(sym="AAAUSDT", **kw):
    base = {"symbol": sym, "trend": "up", "htf_1h_trend": "flat", "htf_4h_trend": "up",
            "ema_stack": "mixed", "funding_rate": 0.0001, "ret5_pct": 0.5, "vol_ratio": 1.0,
            "whale": None}
    base.update(kw)
    return base


# ---------------------------------------------------------------- read_news
def test_read_news_missing_file_is_neutral(tmp_path):
    r = ltt.read_news(tmp_path / "nope.json", NOW_MS)
    assert r["fresh"] is False and r["events"] == []


def test_read_news_stale_is_neutral(tmp_path):
    p = tmp_path / "news.json"
    p.write_text(json.dumps({"ts": _iso(NOW_MS - 3 * 3600 * 1000), "catalyst_score": 0.9,
                             "top_events": [{"title": "x", "symbols": ["BTC"], "catalyst": 0.9}]}),
                 encoding="utf-8")
    assert ltt.read_news(p, NOW_MS)["fresh"] is False


def test_read_news_fresh_parses_and_drops_tainted(tmp_path):
    p = tmp_path / "news.json"
    p.write_text(json.dumps({"ts": _iso(NOW_MS - 60_000), "catalyst_score": 0.4, "macro_risk_score": 0.5,
                             "top_events": [
                                 {"title": "clean", "symbols": ["BTC"], "catalyst": 0.6, "freshness": 0.9},
                                 {"title": "tainted", "symbols": ["ETH"], "catalyst": 0.9,
                                  "sanitize_flags": ["suspicious"]}]}),
                 encoding="utf-8")
    r = ltt.read_news(p, NOW_MS)
    assert r["fresh"] is True and len(r["events"]) == 1 and r["events"][0]["symbols"] == ["BTC"]


def test_read_news_corrupt_is_neutral(tmp_path):
    p = tmp_path / "news.json"
    p.write_text("{not json", encoding="utf-8")
    assert ltt.read_news(p, NOW_MS)["fresh"] is False


def test_read_news_naive_ts_is_treated_as_utc(tmp_path):
    # Opus review M1: a tz-naive ts must NOT be read in local tz (UTC+7 would silently
    # zero the news path forever). Naive ts 60s old == fresh.
    naive = datetime.datetime.fromtimestamp(NOW_MS / 1000 - 60,
                                            datetime.timezone.utc).replace(tzinfo=None).isoformat()
    p = tmp_path / "news.json"
    p.write_text(json.dumps({"ts": naive, "catalyst_score": 0.2,
                             "top_events": [{"title": "x", "symbols": ["BTC"], "catalyst": 0.5}]}),
                 encoding="utf-8")
    assert ltt.read_news(p, NOW_MS)["fresh"] is True


# ---------------------------------------------------------------- evaluate: each path
def test_chart_align_up_fires_only_when_all_three_agree_and_stack_confirms():
    row = _ctx(trend="up", htf_1h_trend="up", htf_4h_trend="up", ema_stack="bull_stack")
    hit = ltt.evaluate([row], {})
    assert hit["AAAUSDT"]["paths"] == ["chart_align"]
    assert hit["AAAUSDT"]["vals"]["chart_align"]["dir"] == "up"
    # one TF disagrees -> no fire
    assert ltt.evaluate([_ctx(trend="up", htf_1h_trend="down", htf_4h_trend="up",
                              ema_stack="bull_stack")], {}) == {}
    # stack not confirming -> no fire
    assert ltt.evaluate([_ctx(trend="up", htf_1h_trend="up", htf_4h_trend="up",
                              ema_stack="mixed")], {}) == {}


def test_chart_align_down_fires_with_bear_stack():
    row = _ctx(trend="down", htf_1h_trend="down", htf_4h_trend="down", ema_stack="bear_stack")
    assert ltt.evaluate([row], {})["AAAUSDT"]["vals"]["chart_align"]["dir"] == "down"


def test_funding_extreme_fires():
    hit = ltt.evaluate([_ctx(funding_rate=0.001)], {})
    assert hit["AAAUSDT"]["paths"] == ["funding_extreme"]
    assert hit["AAAUSDT"]["vals"]["funding_extreme"]["rate"] == 0.001


def test_flush_proxy_fires_and_is_labeled_no_oi():
    hit = ltt.evaluate([_ctx(ret5_pct=-4.2, vol_ratio=3.1)], {})
    assert hit["AAAUSDT"]["paths"] == ["flush_no_oi"]
    # weak flush (low volume) -> no fire
    assert ltt.evaluate([_ctx(ret5_pct=-4.2, vol_ratio=1.2)], {}) == {}


def test_funding_and_flush_are_independent_hypotheses():
    # both can fire on the same coin (extreme funding DURING a flush) — measured separately
    hit = ltt.evaluate([_ctx(funding_rate=0.002, ret5_pct=-5.0, vol_ratio=4.0)], {})
    assert set(hit["AAAUSDT"]["paths"]) == {"funding_extreme", "flush_no_oi"}


def test_num_rejects_inf():
    hit = ltt.evaluate([_ctx(funding_rate=float("inf"))], {})
    assert hit == {}   # inf must not fire funding_extreme (and never reaches trigger_log)


def test_whale_fires_on_strong_directional_pressure():
    hit = ltt.evaluate([_ctx(whale={"side": "LONG", "score": 0.8})], {})
    assert hit["AAAUSDT"]["vals"]["whale"]["side"] == "LONG"
    assert ltt.evaluate([_ctx(whale={"side": "MIXED", "score": 0.9})], {}) == {}
    assert ltt.evaluate([_ctx(whale={"side": "LONG", "score": 0.1})], {}) == {}


def test_news_symbol_match_fires():
    news = {"fresh": True, "events": [{"title": "AAA lists", "symbols": ["AAA"], "catalyst": 0.5}]}
    hit = ltt.evaluate([_ctx()], news)
    assert "news" in hit["AAAUSDT"]["paths"]


def test_news_macro_routes_to_majors_only():
    news = {"fresh": True, "events": [{"title": "Fed shock", "symbols": [], "catalyst": 0.9}]}
    hit = ltt.evaluate([_ctx(sym="BTCUSDT"), _ctx(sym="AAAUSDT")], news)
    assert "BTCUSDT" in hit and hit["BTCUSDT"]["vals"]["news"].get("macro") is True
    assert "AAAUSDT" not in hit


def test_stale_news_never_fires():
    news = {"fresh": False, "events": [{"title": "AAA", "symbols": ["AAA"], "catalyst": 0.9}]}
    assert ltt.evaluate([_ctx()], news) == {}


# ---------------------------------------------------------------- fail-soft + logging
def test_evaluate_never_raises_on_garbage():
    assert ltt.evaluate(None, None) == {}
    assert ltt.evaluate([{"symbol": None}, "junk", {}, {"symbol": "BBBUSDT", "whale": "?",
                                                       "funding_rate": "nan"}], {"fresh": True}) == {}


def test_multi_path_tags_all():
    row = _ctx(trend="up", htf_1h_trend="up", htf_4h_trend="up", ema_stack="bull_stack",
               funding_rate=0.002)
    paths = ltt.evaluate([row], {})["AAAUSDT"]["paths"]
    assert set(paths) == {"funding_extreme", "chart_align"}


def test_log_cycle_writes_one_valid_jsonl_line(tmp_path):
    p = tmp_path / "trigger_log.jsonl"
    ltt.log_cycle(p, NOW_MS, {"AAAUSDT": {"paths": ["news"], "vals": {}}}, 30)
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["n_hit"] == 1 and rec["n_ctx"] == 30 and "thresholds" in rec


def test_log_cycle_failure_is_silent(tmp_path):
    ltt.log_cycle(tmp_path, NOW_MS, {}, 0)   # path is a DIRECTORY -> open fails -> must not raise


# ---------------------------------------------------------------- learning grouping
def test_trigger_key_grouping():
    assert _trigger_key({"trigger_paths": ["whale", "chart_align"]}) == "chart_align+whale"
    assert _trigger_key({"trigger_paths": []}) == "none"
    assert _trigger_key({}) == "none"
    assert _trigger_key({"trigger_paths": None}) == "none"
