"""Generate paper-only trade candidates from the current market snapshot.

This daemon is the bridge between market observation and the autonomous paper
brain. It never places orders and never reads exchange keys. It only writes
candidate JSON and queue jobs for paper/shadow evaluation.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from agent_work_queue import enqueue_job
from atomic_state import append_jsonl, read_json, write_json_atomic
from instrument_registry import QUALITY_PATH as REGISTRY_QUALITY_PATH, REGISTRY_PATH, load_registry, normalize_symbol, summarize_registry
from market_feature_store import compute_market_features
from timebase import parse_utc, seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PID_FILE = STATE_DIR / "paper_candidate_feeder.pid"
HEARTBEAT_PATH = STATE_DIR / "paper_candidate_feeder_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_PAPER_CANDIDATE_FEEDER"
MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
LATEST_PATH = MEMORY_DIR / "paper_candidate_feeder_latest.json"
HISTORY_PATH = MEMORY_DIR / "paper_candidate_feeder_history.jsonl"
CANDIDATES_PATH = MEMORY_DIR / "paper_candidates_latest.json"
SETUP_RANKINGS_PATH = MEMORY_DIR / "setup_rankings_latest.json"
MICROSTRUCTURE_FLOW_LATEST = MEMORY_DIR / "microstructure_flow_latest.json"
DEFAULT_PAPER_FUTURES_LEVERAGE = 5
PREFERRED_FUNDING_THRESHOLD = 0.15
PAPER_SCALP_STOP_CAPS = {
    "exhaustion_fade": 0.035,
    "funding_squeeze": 0.025,
}
PAPER_SCALP_REWARD_MULTIPLIERS = {
    "exhaustion_fade": 1.15,
    "funding_squeeze": 1.05,
}
TICKER_PROXY_CHART_CAPABILITY_MASK = {
    "feature_family": "chart_intelligence",
    "action": "skip",
    "missing_required": ["closed_multi_timeframe_candles"],
    "source_confidence": 0.0,
    "value_errors": ["ticker_24h_proxy_not_chart_candles"],
}

def f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default

def candidate_id(row: dict[str, Any], snapshot_ts: str) -> str:
    raw = f"{snapshot_ts}:{row.get('symbol')}:{row.get('side')}:{row.get('setup_id')}:{row.get('entry')}"
    return "paper_candidate_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def feature_candles_from_market_row(row: dict[str, Any], snapshot_ts: str) -> list[dict[str, Any]]:
    price = f(row.get("price"))
    high = f(row.get("high"), price)
    low = f(row.get("low"), price)
    change_pct = f(row.get("change_pct"))
    quote_volume = f(row.get("quote_volume"))
    base_dt = parse_utc(snapshot_ts) or parse_utc(utc_now())
    if not base_dt:
        raise ValueError("invalid_snapshot_ts")
    denominator = 1.0 + change_pct / 100.0
    previous = price / denominator if price > 0 and denominator > 0 else price
    mid = (previous + price) / 2.0 if price > 0 else previous
    vol = max(0.0, quote_volume / 3.0)
    rows = []
    points = [
        (base_dt - timedelta(minutes=2), previous, mid, max(previous, mid), min(previous, mid)),
        (base_dt - timedelta(minutes=1), mid, price, max(mid, price), min(mid, price)),
        (base_dt, price, price, high, low),
    ]
    for ts_dt, open_price, close_price, high_price, low_price in points:
        ts = ts_dt.isoformat(timespec="seconds")
        rows.append(
            {
                "ts": ts,
                "candle_close_time": ts,
                "open": open_price,
                "high": max(high_price, open_price, close_price),
                "low": min(low_price, open_price, close_price),
                "close": close_price,
                "volume": vol,
                "available_at": ts,
                "known_at": ts,
                "ingested_at": snapshot_ts if parse_utc(snapshot_ts) else utc_now(),
                "finalized_at": ts,
                "is_synthetic_chart_proxy": True,
                "chart_decision_eligible": False,
                "chart_candle_source": "ticker_24h_proxy",
            }
        )
    return rows


def flow_features_for_symbol(symbol: str, flow_bundle: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(flow_bundle, dict):
        return {}
    symbol_up = str(symbol or "").upper()
    symbols = flow_bundle.get("symbols") if isinstance(flow_bundle.get("symbols"), dict) else {}
    by_symbol = flow_bundle.get("by_symbol") if isinstance(flow_bundle.get("by_symbol"), dict) else {}
    row = symbols.get(symbol_up) if isinstance(symbols, dict) else {}
    social = by_symbol.get(symbol_up) if isinstance(by_symbol, dict) else {}
    merged = {}
    if isinstance(row, dict):
        merged.update(row)
        components = row.get("components") if isinstance(row.get("components"), dict) else {}
        if isinstance(components.get("social"), dict):
            social = {**components["social"], **(social if isinstance(social, dict) else {})}
    if isinstance(social, dict):
        merged.update(social)
    return merged


def feature_row_for_market_row(row: dict[str, Any], market: dict[str, Any], snapshot_ts: str, flow_features: dict[str, Any] | None = None) -> dict[str, Any]:
    funding = row.get("funding_pct")
    derivatives = None
    if funding is not None:
        derivatives = {
            "funding_pct": funding,
            "quote_volume": row.get("quote_volume"),
            "trade_count": row.get("trade_count"),
            "microstructure_flow": flow_features or {},
            "confidence": 0.55,
            "updated_at": snapshot_ts,
            "available_at": snapshot_ts,
            "known_at": snapshot_ts,
            "ingested_at": snapshot_ts,
            "finalized_at": snapshot_ts,
        }
    source_ids = market.get("source_ids") if isinstance(market.get("source_ids"), list) else ["local_state"]
    source_manifest_ids = [str(value) for value in (market.get("provenance_id"), market.get("snapshot_id"), market.get("event_id")) if value]
    input_event_ids = [str(value) for value in market.get("event_ids", [])] if isinstance(market.get("event_ids"), list) else []
    return compute_market_features(
        str(row.get("symbol") or ""),
        "ticker_24h_proxy",
        feature_candles_from_market_row(row, snapshot_ts),
        derivatives=derivatives,
        source_ids=source_ids,
        input_event_ids=input_event_ids,
        source_manifest_ids=source_manifest_ids,
        decision_cutoff=snapshot_ts if parse_utc(snapshot_ts) else utc_now(),
        latency_buffer_seconds=0,
        fit_metadata={"fit_window": "none_ticker_proxy", "train_partition": "none_runtime_transform"},
    )

def paper_scalp_geometry(side: str, price: float, raw_sl: float, raw_tp: float, setup_id: str) -> tuple[float, float]:
    """Cap paper scalp stop distance so $100 futures sizing is realistic.

    Candidate source rows often contain a full 24h high/low. Using that as SL
    creates 9-15% stops, which forces tiny notional under the risk gate. The
    paper learner is testing short futures reactions, so the simulated setup
    uses a bounded scalp invalidation while keeping the same side and R target.
    """
    if price <= 0:
        return raw_sl, raw_tp
    side_up = str(side or "").upper()
    stop_cap = PAPER_SCALP_STOP_CAPS.get(setup_id, 0.03)
    reward_multiple = PAPER_SCALP_REWARD_MULTIPLIERS.get(setup_id, 1.1)
    if side_up == "LONG":
        sl = max(raw_sl, price * (1.0 - stop_cap))
        risk = price - sl
        if risk <= 0:
            return raw_sl, raw_tp
        tp = min(raw_tp, price + risk * reward_multiple) if raw_tp > price else price + risk * reward_multiple
        return sl, tp
    if side_up == "SHORT":
        sl = min(raw_sl, price * (1.0 + stop_cap))
        risk = sl - price
        if risk <= 0:
            return raw_sl, raw_tp
        tp = max(raw_tp, price - risk * reward_multiple) if raw_tp < price else price - risk * reward_multiple
        return sl, tp
    return raw_sl, raw_tp

def setup_routing_from_rankings(payload: dict[str, Any] | None) -> dict[str, Any]:
    rankings = payload.get("rankings") if isinstance(payload, dict) and isinstance(payload.get("rankings"), list) else []
    preferred: set[str] = set()
    blocked: dict[str, list[str]] = {}
    rows: dict[str, dict[str, Any]] = {}
    for item in rankings:
        if not isinstance(item, dict):
            continue
        setup_id = str(item.get("setup_id") or "")
        if not setup_id:
            continue
        rows[setup_id] = item
        reasons = [str(value) for value in item.get("rank_reasons", []) if value]
        expectancy = f(item.get("evidence_expectancy"), f(item.get("expectancy")))
        hint = str(item.get("allocation_hint") or "")
        if item.get("paper_only_retired") or expectancy <= 0 or hint == "skip" or "non_positive_evidence_expectancy" in reasons:
            blocked[setup_id] = reasons or ["setup_not_tradeable"]
            continue
        if hint == "normal" or expectancy > 0:
            preferred.add(setup_id)
    return {"preferred": preferred, "blocked": blocked, "rows": rows}

def whale_flow_context(symbol: str, side: str, payload: dict[str, Any] | None, *, as_of: str | None = None) -> dict[str, Any]:
    age = seconds_between(payload.get("updated_at") if isinstance(payload, dict) else None, as_of or utc_now()) if isinstance(payload, dict) else None
    if age is None or age > 900:
        return {"alignment": "none", "score_bonus": 0.0, "reasons": ["whale_flow_missing_or_stale"] if payload else [], "stale": True, "age_seconds": age}
    by_symbol = payload.get("by_symbol") if isinstance(payload, dict) and isinstance(payload.get("by_symbol"), dict) else {}
    row = by_symbol.get(str(symbol or "").upper()) or by_symbol.get("MARKET")
    if not isinstance(row, dict) and isinstance(payload, dict) and "pressure_side" in payload:
        row = payload
    if not isinstance(row, dict):
        return {"alignment": "none", "score_bonus": 0.0, "reasons": []}
    if row.get("too_late_to_copy"):
        return {"alignment": "none", "score_bonus": 0.0, "reasons": ["whale_flow_too_late_to_copy"], "too_late_to_copy": True}
    pressure_side = str(row.get("pressure_side") or "NEUTRAL").upper()
    side_up = str(side or "").upper()
    score = f(row.get("pressure_score"))
    source_quorum_passed = bool(row.get("source_quorum_passed") or payload.get("source_quorum_passed"))
    market_confirmed = bool(row.get("market_confirmed") or payload.get("market_confirmed"))
    can_rank_up = source_quorum_passed and market_confirmed
    reasons: list[str] = []
    bonus = 0.0
    alignment = "neutral"
    if pressure_side in {"LONG", "SHORT"} and side_up in {"LONG", "SHORT"}:
        if pressure_side == side_up:
            alignment = "aligned"
            if can_rank_up:
                bonus = min(0.6, abs(score) * 1.2)
                reasons.append("whale_flow_aligned")
            else:
                bonus = 0.0
                reasons.append("whale_flow_shadow_only_no_rank_up")
        else:
            alignment = "conflict"
            bonus = -min(1.0, abs(score) * 1.5 + 0.2)
            reasons.append("whale_flow_conflict")
    if row.get("squeeze_risk") and row.get("squeeze_risk") != "none":
        reasons.append(str(row.get("squeeze_risk")))
    return {
        "alignment": alignment,
        "score_bonus": round(bonus, 4),
        "reasons": reasons,
        "symbol": row.get("symbol"),
        "pressure_side": pressure_side,
        "pressure_score": row.get("pressure_score"),
        "crowd_bias": row.get("crowd_bias"),
        "squeeze_risk": row.get("squeeze_risk"),
        "event_count": row.get("event_count"),
        "source_quorum_passed": source_quorum_passed,
        "market_confirmed": market_confirmed,
        "allowed_effect": row.get("allowed_effect") or payload.get("allowed_effect") or "shadow_only",
    }

def funding_squeeze_candidate(
    row: dict[str, Any],
    snapshot_ts: str,
    *,
    price: float,
    high: float,
    low: float,
    range_pos: float,
    quote_volume: float,
    funding_pct: float,
    preferred: bool = False,
    whale_flow: dict[str, Any] | None = None,
    feature_row: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    threshold = PREFERRED_FUNDING_THRESHOLD if preferred else 0.25
    if abs(funding_pct) < threshold or quote_volume < 20_000_000:
        return None
    setup_id = "funding_squeeze"
    reason: list[str] = []
    if funding_pct < 0 and range_pos <= 0.45:
        side = "LONG"
        raw_sl = min(low * 0.997, price * 0.99)
        raw_tp = min(high, price + (price - raw_sl) * 1.05)
        sl, tp = paper_scalp_geometry(side, price, raw_sl, raw_tp, setup_id)
        reason.extend(["negative_funding_crowded", "possible_long_squeeze"])
    elif funding_pct > 0 and range_pos >= 0.55:
        side = "SHORT"
        raw_sl = max(high * 1.003, price * 1.01)
        raw_tp = max(low, price - (raw_sl - price) * 1.05)
        sl, tp = paper_scalp_geometry(side, price, raw_sl, raw_tp, setup_id)
        reason.extend(["positive_funding_crowded", "possible_short_squeeze"])
    else:
        return None
    return build_candidate_payload(row, snapshot_ts, setup_id, side, price, sl, tp, reason, setup_bonus=0.6 if preferred else 0.0, whale_flow=whale_flow, feature_row=feature_row)

def build_candidate_payload(
    row: dict[str, Any],
    snapshot_ts: str,
    setup_id: str,
    side: str,
    price: float,
    sl: float,
    tp: float,
    reason: list[str],
    *,
    setup_bonus: float = 0.0,
    blocked_reasons: list[str] | None = None,
    whale_flow: dict[str, Any] | None = None,
    feature_row: dict[str, Any] | None = None,
    chart_score: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if side == "LONG" and not (sl < price < tp):
        return None
    if side == "SHORT" and not (tp < price < sl):
        return None
    change = f(row.get("change_pct"))
    quote_volume = f(row.get("quote_volume"))
    funding_pct = f(row.get("funding_pct"))
    flow_as_of = snapshot_ts if parse_utc(snapshot_ts) else None
    flow = whale_flow_context(str(row.get("symbol") or ""), side, whale_flow, as_of=flow_as_of)
    score = min(10.0, max(0.0, 5.0 + min(2.5, abs(change) / 18.0) + min(1.5, quote_volume / 400_000_000) + min(1.0, abs(funding_pct) / 0.5) + setup_bonus + f(flow.get("score_bonus"))))
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": "",
        "generated_at": utc_now(),
        "market_snapshot_ts": snapshot_ts,
        "symbol": str(row.get("symbol") or "").upper(),
        "side": side,
        "setup_id": setup_id,
        "score": round(score, 4),
        "entry": round(price, 10),
        "sl": round(sl, 10),
        "tp": round(tp, 10),
        "leverage": DEFAULT_PAPER_FUTURES_LEVERAGE,
        "exploration_allowed": True,
        "source": "paper_candidate_feeder",
        "producer_id": "paper_candidate_feeder",
        "source_ids": (feature_row or {}).get("source_ids") or ["market_observer"],
        "allowed_effect": "feature_input",
        "taint_classes": ["public_market"] + (["external_social"] if f(flow.get("score_bonus")) > 0 else []),
        "source_quorum_passed": bool(flow.get("source_quorum_passed")),
        "market_confirmed": bool(flow.get("market_confirmed")),
        "provenance_status": (feature_row or {}).get("feature_status") or "ok",
        "feature_id": (feature_row or {}).get("feature_id"),
        "feature_manifest_id": (feature_row or {}).get("manifest_id"),
        "canonical_instrument_id": ((feature_row or {}).get("microstructure_flow") or {}).get("canonical_instrument_id"),
        "instrument_snapshot_id": ((feature_row or {}).get("microstructure_flow") or {}).get("instrument_snapshot_id"),
        "price_basis": ((feature_row or {}).get("microstructure_flow") or {}).get("price_basis"),
        "feature_status": (feature_row or {}).get("feature_status"),
        "feature_artifact_digest": (feature_row or {}).get("artifact_digest"),
        "feature_cutoff_proof": (feature_row or {}).get("cutoff_proof"),
        "decision_data_capability_mask": (feature_row or {}).get("decision_data_capability_mask"),
        "decision_regime_state": (feature_row or {}).get("decision_regime_state"),
        "chart_intelligence_id": (chart_score or {}).get("chart_intelligence_id") or (chart_score or {}).get("score_id"),
        "chart_score": chart_score,
        "chart_score_value": (chart_score or {}).get("score"),
        "chart_score_tier": (chart_score or {}).get("tier"),
        "chart_data_status": "ok" if isinstance(chart_score, dict) and chart_score.get("degradation_state") == "ok" else "not_available",
        "chart_decision_eligible": bool(isinstance(chart_score, dict) and chart_score.get("capability_mask", {}).get("action") != "skip"),
        "chart_data_capability_mask": (chart_score or {}).get("capability_mask") if isinstance(chart_score, dict) else TICKER_PROXY_CHART_CAPABILITY_MASK,
        "reason": reason + list(flow.get("reasons") or []),
        "setup_routing": {"setup_bonus": round(setup_bonus, 4), "blocked_reasons": blocked_reasons or []},
        "external_flow": flow,
        "market_features": {
            "change_pct": change,
            "range_pos": f(row.get("range_pos"), 0.5),
            "quote_volume": quote_volume,
            "funding_pct": funding_pct,
            "hot_score": f(row.get("hot_score")),
            "trade_count": int(f(row.get("trade_count"))),
        },
        "can_place_live_orders": False,
    }
    if f(flow.get("score_bonus")) > 0 and not (flow.get("source_quorum_passed") and flow.get("market_confirmed")):
        candidate["allowed_effect"] = "shadow_only"
    if feature_row and not feature_row.get("usable_for_paper"):
        candidate["allowed_effect"] = "shadow_only"
        candidate["reason"] = candidate["reason"] + ["feature_row_not_usable_for_paper"]
    candidate["candidate_id"] = candidate_id(candidate, snapshot_ts)
    return candidate

def candidate_from_market_row(row: dict[str, Any], snapshot_ts: str, setup_routing: dict[str, Any] | None = None, whale_flow: dict[str, Any] | None = None, feature_row: dict[str, Any] | None = None) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or "").upper()
    price = f(row.get("price"))
    high = f(row.get("high"))
    low = f(row.get("low"))
    change = f(row.get("change_pct"))
    range_pos = f(row.get("range_pos"), 0.5)
    quote_volume = f(row.get("quote_volume"))
    funding_pct = f(row.get("funding_pct"))
    if not symbol or price <= 0 or high <= 0 or low <= 0:
        return None
    routing = setup_routing or {}
    preferred = routing.get("preferred") if isinstance(routing.get("preferred"), set) else set()
    blocked = routing.get("blocked") if isinstance(routing.get("blocked"), dict) else {}
    funding_first = "funding_squeeze" in preferred
    if funding_first:
        candidate = funding_squeeze_candidate(row, snapshot_ts, price=price, high=high, low=low, range_pos=range_pos, quote_volume=quote_volume, funding_pct=funding_pct, preferred=True, whale_flow=whale_flow, feature_row=feature_row)
        if candidate:
            return candidate
    side = None
    setup_id = "exhaustion_fade"
    reason = []
    if change >= 18 and range_pos >= 0.72:
        side = "SHORT"
        raw_sl = max(high * 1.003, price * 1.012)
        raw_tp = max(low, price - (raw_sl - price) * 1.15)
        sl, tp = paper_scalp_geometry(side, price, raw_sl, raw_tp, setup_id)
        reason.extend(["overextended_gainer", "fade_after_extreme"])
    elif change <= -18 and range_pos <= 0.28:
        side = "LONG"
        raw_sl = min(low * 0.997, price * 0.988)
        raw_tp = min(high, price + (price - raw_sl) * 1.15)
        sl, tp = paper_scalp_geometry(side, price, raw_sl, raw_tp, setup_id)
        reason.extend(["overextended_loser", "snapback_after_extreme"])
    else:
        candidate = funding_squeeze_candidate(row, snapshot_ts, price=price, high=high, low=low, range_pos=range_pos, quote_volume=quote_volume, funding_pct=funding_pct, whale_flow=whale_flow, feature_row=feature_row)
        if candidate:
            return candidate
        return None
    blocked_reasons = blocked.get(setup_id, [])
    setup_bonus = -1.2 if blocked_reasons else 0.0
    return build_candidate_payload(row, snapshot_ts, setup_id, side, price, sl, tp, reason, setup_bonus=setup_bonus, blocked_reasons=blocked_reasons, whale_flow=whale_flow, feature_row=feature_row)

def build_candidates(market: dict[str, Any], limit: int = 8, setup_rankings: dict[str, Any] | None = None, whale_flow: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    snapshot_ts = str(market.get("ts") or market.get("updated_at") or utc_now())
    setup_routing = setup_routing_from_rankings(setup_rankings)
    raw_rows = []
    for key in ("hot", "top_gainers", "top_losers", "funding_extremes"):
        rows = market.get(key) if isinstance(market.get(key), list) else []
        raw_rows.extend(row for row in rows if isinstance(row, dict))
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in by_symbol:
            by_symbol[symbol] = row
    candidates = []
    for row in by_symbol.values():
        flow_features = flow_features_for_symbol(str(row.get("symbol") or ""), whale_flow)
        try:
            feature_row = feature_row_for_market_row(row, market, snapshot_ts, flow_features=flow_features)
        except Exception as exc:
            feature_row = {"feature_status": "quarantined", "usable_for_paper": False, "decision_data_capability_mask": {"action": "skip", "value_errors": [f"feature_build_error:{str(exc)[:80]}"]}}
        candidate = candidate_from_market_row(row, snapshot_ts, setup_routing, whale_flow=whale_flow, feature_row=feature_row)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=lambda row: (f(row.get("score")), f((row.get("market_features") or {}).get("quote_volume"))), reverse=True)
    return candidates[:limit]

def tick_size_for_price(price: float) -> str:
    if price >= 1000:
        return "0.1"
    if price >= 10:
        return "0.001"
    if price >= 1:
        return "0.0001"
    if price >= 0.01:
        return "0.00001"
    return "0.00000001"

def bootstrap_paper_instrument_registry(market: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or REGISTRY_PATH
    registry = load_registry(path)
    instruments = registry.get("instruments") if isinstance(registry.get("instruments"), dict) else {}
    added = 0
    for key in ("hot", "top_gainers", "top_losers", "top_volume", "funding_extremes", "majors"):
        rows = market.get(key) if isinstance(market.get(key), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = normalize_symbol(row.get("symbol"))
            if not symbol or symbol in instruments:
                continue
            price = f(row.get("price"), 1.0)
            instruments[symbol] = {
                "schema_version": SCHEMA_VERSION,
                "symbol": symbol,
                "status": "shadow_only",
                "tick_size": tick_size_for_price(price),
                "step_size": "0.001",
                "min_notional": "0.01",
                "max_leverage": "20",
                "source": "market_snapshot_paper_bootstrap",
                "allowed_effect": "shadow_only",
                "updated_at": utc_now(),
            }
            added += 1
    payload = {"schema_version": SCHEMA_VERSION, "registry_version": utc_now(), "updated_at": utc_now(), "source": "paper_candidate_feeder", "instruments": instruments}
    write_json_atomic(path, payload)
    write_json_atomic(REGISTRY_QUALITY_PATH, summarize_registry(payload))
    return {"registry": payload, "added": added, "instrument_count": len(instruments)}

def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> None:
    write_json_atomic(HEARTBEAT_PATH, {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})})

def run_once(limit: int = 8, enqueue: bool = True) -> dict[str, Any]:
    market = read_json(MARKET_LATEST, default={})
    setup_rankings = read_json(SETUP_RANKINGS_PATH, default={})
    whale_flow = read_json(MICROSTRUCTURE_FLOW_LATEST, default={})
    registry_update = bootstrap_paper_instrument_registry(market)
    candidates = build_candidates(market, limit=limit, setup_rankings=setup_rankings, whale_flow=whale_flow)
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "market_ts": market.get("ts"), "candidate_count": len(candidates), "registry_update": {"added": registry_update["added"], "instrument_count": registry_update["instrument_count"]}, "setup_routing": {"top_setup_id": setup_rankings.get("top_setup_id"), "ranking_updated_at": setup_rankings.get("updated_at")}, "whale_flow_context": {"updated_at": whale_flow.get("updated_at"), "event_count": whale_flow.get("event_count")}, "candidates": candidates, "can_place_live_orders": False}
    write_json_atomic(CANDIDATES_PATH, payload)
    write_json_atomic(LATEST_PATH, payload)
    append_jsonl(HISTORY_PATH, payload)
    queued = []
    if enqueue:
        for candidate in candidates[:3]:
            job = enqueue_job("setup_review", {"candidate": candidate, "candidates": [candidate], "source": "paper_candidate_feeder"}, priority=int(f(candidate.get("score")) * 10), job_id=f"job_{candidate.get('candidate_id')}_{candidate.get('market_snapshot_ts')}")
            queued.append(job)
    write_heartbeat("ok" if candidates else "waiting", {"candidate_count": len(candidates), "queued_count": sum(1 for row in queued if row.get("ok"))})
    return {**payload, "queued": queued}

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate paper-only candidates from market snapshots")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.limit <= 0:
        parser.error("--limit must be positive")
    return args

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        result = run_once(limit=args.limit)
        print(f"paper_candidate_feeder candidates={result.get('candidate_count')} queued={len(result.get('queued') or [])}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
