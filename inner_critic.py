"""Pre-entry critic for paper trading decisions.

The critic is read-only and tighten-only. It can allow, tighten, or block a
paper entry, but it never places trades and never loosens risk controls.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from event_store import safe_append_event
from market_learner import safe_float
from dont_do_memory import evaluate_candidate as evaluate_dont_do_candidate
from memory_retrieval import active_recall_for_decision
from setup_skill_library import load_library, match_setup

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
MARKET_MODEL_PATH = MEMORY_DIR / "market_model.json"
BIAS_PATH = MEMORY_DIR / "execution_bias.json"
HYPOTHESES_LATEST = MEMORY_DIR / "hypotheses_latest.json"
NEWS_LATEST = MEMORY_DIR / "news_latest.json"
MARKET_MAX_AGE_SECONDS = 15 * 60
NEWS_MAX_AGE_SECONDS = 45 * 60

VERDICTS = {"allow_paper", "tighten", "block"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None

def newest_ts(payload: dict) -> datetime | None:
    candidates = [payload.get("ts"), payload.get("updated_at"), payload.get("generated_at")] if isinstance(payload, dict) else []
    for value in candidates:
        parsed = parse_ts(value)
        if parsed:
            return parsed
    found: list[datetime] = []
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, dict):
                parsed = newest_ts(value)
                if parsed:
                    found.append(parsed)
    return max(found) if found else None

def stale_status(name: str, payload: dict, max_age_seconds: int, now: datetime | None = None) -> dict:
    ts = newest_ts(payload)
    if not ts:
        return {"name": name, "status": "unknown", "age_seconds": None, "max_age_seconds": max_age_seconds}
    current = now or datetime.now(timezone.utc)
    age_seconds = max(0, int((current - ts).total_seconds()))
    return {
        "name": name,
        "status": "stale" if age_seconds > max_age_seconds else "fresh",
        "ts": ts.isoformat(timespec="seconds"),
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
    }


def is_sleep_active(bias: dict, now: datetime | None = None) -> bool:
    sleep_until = parse_ts(bias.get("sleep_until"))
    if not sleep_until:
        return False
    current = now or datetime.now(timezone.utc)
    return sleep_until > current


def supporting_hypotheses(signal: dict, setup_ids: list[str], hypotheses: list[dict]) -> list[dict]:
    symbol = str(signal.get("symbol") or "").upper()
    side = str(signal.get("side") or "").upper()
    result = []
    for item in hypotheses:
        symbols = {str(row).upper() for row in item.get("symbols", []) if row}
        setup_id = str(item.get("setup_id") or "")
        pred_side = str((item.get("prediction") or {}).get("side") or "").upper()
        if symbols and symbol not in symbols:
            continue
        if setup_ids and setup_id not in setup_ids and setup_id != "manual_chart_thesis":
            continue
        if pred_side in {"LONG", "SHORT"} and pred_side != side:
            continue
        result.append(item)
    return result[:5]


def verdict_payload(
    verdict: str,
    reasons: list[str],
    setup_matches: list[dict],
    hypotheses: list[dict],
    min_score: int | None = None,
    stale_data: list[dict] | None = None,
    news_context: dict | None = None,
    active_recall: dict | None = None,
) -> dict:
    if verdict not in VERDICTS:
        verdict = "block"
    return {
        "ts": utc_now(),
        "verdict": verdict,
        "reasons": reasons[:12],
        "setup_matches": setup_matches[:5],
        "setup_ids": [row.get("setup_id") for row in setup_matches[:5]],
        "hypothesis_ids": [row.get("hypothesis_id") for row in hypotheses[:5]],
        "stale_data": stale_data or [],
        "news_context": news_context or {},
        "active_recall": active_recall or {},
        "memory_ids_used": (active_recall or {}).get("memory_ids_used") or [],
        "tighten_min_signal_score": min_score,
        "can_loosen": False,
    }

def news_verdict_context(news: dict, symbol: str, side: str, now: datetime | None = None) -> dict:
    if not news:
        return {"status": "missing", "reasons": [], "hard_reasons": [], "tighten_reasons": []}
    stale = stale_status("news_latest", news, NEWS_MAX_AGE_SECONDS, now)
    macro = safe_float(news.get("macro_risk_score"))
    regulatory = safe_float(news.get("crypto_regulatory_risk"))
    chaos = safe_float(news.get("headline_chaos"))
    impacts = news.get("symbol_impacts") if isinstance(news.get("symbol_impacts"), dict) else {}
    base_symbol = symbol[:-4] if symbol.endswith("USDT") else symbol
    impact = impacts.get(base_symbol) or impacts.get(symbol) or {}
    symbol_risk = safe_float(impact.get("risk")) if isinstance(impact, dict) else 0.0
    bullish = safe_float(impact.get("bullish")) if isinstance(impact, dict) else 0.0
    bearish = safe_float(impact.get("bearish")) if isinstance(impact, dict) else 0.0
    hard_reasons: list[str] = []
    tighten_reasons: list[str] = []
    if stale.get("status") == "stale" and int(news.get("event_count", 0) or 0) > 0:
        hard_reasons.append("stale_news_context")
    if max(macro, regulatory, chaos) >= 0.65:
        hard_reasons.append("high_news_macro_or_regulatory_risk")
    if symbol_risk >= 0.55:
        hard_reasons.append("high_symbol_news_risk")
    if side == "LONG" and bearish >= 0.22:
        tighten_reasons.append("news_conflicts_with_long")
    if side == "SHORT" and bullish >= 0.22:
        tighten_reasons.append("news_conflicts_with_short")
    if max(macro, regulatory, chaos, symbol_risk) >= 0.35 and not hard_reasons:
        tighten_reasons.append("elevated_news_risk")
    return {
        "status": stale.get("status", "unknown"),
        "ts": news.get("ts"),
        "macro_risk_score": macro,
        "crypto_regulatory_risk": regulatory,
        "headline_chaos": chaos,
        "symbol_risk": symbol_risk,
        "symbol_bullish": bullish,
        "symbol_bearish": bearish,
        "source_health": (news.get("source_health") if isinstance(news.get("source_health"), list) else [])[:5],
        "top_event_ids": [row.get("event_id") for row in (news.get("top_events") if isinstance(news.get("top_events"), list) else [])[:5]],
        "hard_reasons": hard_reasons,
        "tighten_reasons": tighten_reasons,
        "reasons": hard_reasons + tighten_reasons,
        "can_loosen": False,
    }


def evaluate_signal(
    signal: dict,
    bias: dict | None = None,
    snapshot: dict | None = None,
    market_model: dict | None = None,
    library: dict | None = None,
    hypotheses_result: dict | None = None,
    news_context: dict | None = None,
    active_recall_result: dict | None = None,
    now: datetime | None = None,
) -> dict:
    bias = bias if bias is not None else read_json(BIAS_PATH)
    snapshot = snapshot if snapshot is not None else read_json(MARKET_LATEST)
    market_model = market_model if market_model is not None else read_json(MARKET_MODEL_PATH)
    library = library if library is not None else load_library()
    hypotheses_result = hypotheses_result if hypotheses_result is not None else read_json(HYPOTHESES_LATEST)
    news_context = news_context if news_context is not None else read_json(NEWS_LATEST)
    hypotheses = hypotheses_result.get("hypotheses", []) if isinstance(hypotheses_result.get("hypotheses"), list) else []
    symbol = str(signal.get("symbol") or "").upper()
    side = str(signal.get("side") or "").upper()
    score = int(safe_float(signal.get("score"), 0))
    min_score = max(1, min(99, int(bias.get("min_signal_score", 6) or 6))) if bias else 6
    reasons: list[str] = []
    stale_data = [stale_status("market_snapshot", snapshot, MARKET_MAX_AGE_SECONDS, now)] if snapshot else []

    if not symbol or side not in {"LONG", "SHORT"}:
        verdict = verdict_payload("block", ["invalid_signal"], [], [], min_score, stale_data)
        safe_append_event("inner_critic", "critic_block", {"signal": signal, "critic": verdict})
        return verdict
    if any(item.get("status") == "stale" for item in stale_data):
        verdict = verdict_payload("block", ["stale_market_snapshot"], [], [], min_score, stale_data)
        safe_append_event("inner_critic", "critic_block", {"symbol": symbol, "side": side, "signal": signal, "critic": verdict})
        return verdict
    active_recall = active_recall_result if active_recall_result is not None else active_recall_for_decision(signal, decision_cutoff=(now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
    recall_delta = active_recall.get("decision_delta") if isinstance(active_recall.get("decision_delta"), dict) else {}
    dont_do = evaluate_dont_do_candidate(signal)
    if dont_do.get("action") == "block_paper":
        verdict = verdict_payload("block", ["dont_do_memory_match"], [], [], min_score, stale_data, {"dont_do": dont_do}, active_recall)
        safe_append_event("inner_critic", "critic_block", {"symbol": symbol, "side": side, "signal": signal, "critic": verdict})
        return verdict
    if recall_delta.get("action") == "block":
        verdict = verdict_payload("block", ["active_recall_block"], [], [], min_score, stale_data, {"active_recall_reason": recall_delta.get("reason")}, active_recall)
        safe_append_event("inner_critic", "critic_block", {"symbol": symbol, "side": side, "signal": signal, "critic": verdict})
        return verdict
    news_eval = news_verdict_context(news_context, symbol, side, now)
    if news_eval.get("hard_reasons"):
        verdict = verdict_payload("block", list(news_eval["hard_reasons"]), [], [], min_score, stale_data, news_eval, active_recall)
        safe_append_event("inner_critic", "critic_block", {"symbol": symbol, "side": side, "signal": signal, "critic": verdict})
        return verdict
    if is_sleep_active(bias, now):
        reasons.append("memory_sleep_active")
    if symbol in {str(item).upper() for item in bias.get("blocked_symbols", []) if item}:
        reasons.append("symbol_blocked_by_memory")
    if side in {str(item).upper() for item in bias.get("blocked_sides", []) if item}:
        reasons.append("side_blocked_by_memory")
    if score < min_score:
        reasons.append("score_below_memory_minimum")

    hard_reasons = list(reasons)
    if hard_reasons:
        verdict = verdict_payload("block", hard_reasons, [], [], min_score, stale_data, news_eval, active_recall)
        safe_append_event("inner_critic", "critic_block", {"symbol": symbol, "side": side, "signal": signal, "critic": verdict})
        return verdict

    context = {
        "market_state": market_model.get("last_market_state") if isinstance(market_model.get("last_market_state"), dict) else {},
        "tags": (market_model.get("last_market_state") or {}).get("tags", []),
    }
    setup_matches = match_setup(signal, snapshot, context=context, library=library)
    if not setup_matches:
        verdict = verdict_payload("block", ["no_setup_match"], [], [], min_score, stale_data, news_eval, active_recall)
        safe_append_event("inner_critic", "critic_block", {"symbol": symbol, "side": side, "signal": signal, "critic": verdict})
        return verdict

    setup_ids = [str(row.get("setup_id")) for row in setup_matches if row.get("setup_id")]
    supporting = supporting_hypotheses(signal, setup_ids, hypotheses)
    if not supporting:
        reasons.append("no_supporting_hypothesis")
    if setup_matches and safe_float(setup_matches[0].get("confidence")) < 0.58:
        reasons.append("weak_setup_match")
    if safe_float(signal.get("spread_pct")) > 0.06:
        reasons.append("spread_too_wide")
    if dont_do.get("action") == "shadow_only":
        reasons.append("dont_do_memory_shadow_only")
    if recall_delta.get("action") == "tighten":
        reasons.append("active_recall_tighten")
    reasons.extend(news_eval.get("tighten_reasons", []))

    verdict = "tighten" if reasons else "allow_paper"
    payload = verdict_payload(verdict, reasons or ["critic_passed"], setup_matches, supporting, max(min_score, 7) if verdict == "tighten" else min_score, stale_data, news_eval, active_recall)
    safe_append_event("inner_critic", "critic_verdict", {"symbol": symbol, "side": side, "signal": signal, "critic": payload})
    return payload
