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
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from agent_work_queue import enqueue_job
from atomic_state import append_jsonl, read_json, write_json_atomic
from instrument_registry import QUALITY_PATH as REGISTRY_QUALITY_PATH, REGISTRY_PATH, load_registry, normalize_symbol, summarize_registry
from timebase import utc_now

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
DEFAULT_PAPER_FUTURES_LEVERAGE = 5
PAPER_SCALP_STOP_CAPS = {
    "exhaustion_fade": 0.035,
    "funding_squeeze": 0.025,
}
PAPER_SCALP_REWARD_MULTIPLIERS = {
    "exhaustion_fade": 1.15,
    "funding_squeeze": 1.05,
}

def f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default

def candidate_id(row: dict[str, Any], snapshot_ts: str) -> str:
    raw = f"{snapshot_ts}:{row.get('symbol')}:{row.get('side')}:{row.get('setup_id')}:{row.get('entry')}"
    return "paper_candidate_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

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

def candidate_from_market_row(row: dict[str, Any], snapshot_ts: str) -> dict[str, Any] | None:
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
    elif abs(funding_pct) >= 0.25 and quote_volume >= 20_000_000:
        setup_id = "funding_squeeze"
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
    else:
        return None
    if side == "LONG" and not (sl < price < tp):
        return None
    if side == "SHORT" and not (tp < price < sl):
        return None
    score = min(10.0, 5.0 + min(2.5, abs(change) / 18.0) + min(1.5, quote_volume / 400_000_000) + min(1.0, abs(funding_pct) / 0.5))
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": "",
        "generated_at": utc_now(),
        "market_snapshot_ts": snapshot_ts,
        "symbol": symbol,
        "side": side,
        "setup_id": setup_id,
        "score": round(score, 4),
        "entry": round(price, 10),
        "sl": round(sl, 10),
        "tp": round(tp, 10),
        "leverage": DEFAULT_PAPER_FUTURES_LEVERAGE,
        "exploration_allowed": True,
        "source": "paper_candidate_feeder",
        "reason": reason,
        "market_features": {
            "change_pct": change,
            "range_pos": range_pos,
            "quote_volume": quote_volume,
            "funding_pct": funding_pct,
            "hot_score": f(row.get("hot_score")),
            "trade_count": int(f(row.get("trade_count"))),
        },
        "can_place_live_orders": False,
    }
    candidate["candidate_id"] = candidate_id(candidate, snapshot_ts)
    return candidate

def build_candidates(market: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    snapshot_ts = str(market.get("ts") or market.get("updated_at") or utc_now())
    raw_rows = []
    for key in ("hot", "top_gainers", "top_losers", "funding_extremes"):
        rows = market.get(key) if isinstance(market.get(key), list) else []
        raw_rows.extend(row for row in rows if isinstance(row, dict))
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in by_symbol:
            by_symbol[symbol] = row
    candidates = [candidate for row in by_symbol.values() if (candidate := candidate_from_market_row(row, snapshot_ts))]
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
                "status": "paper_allowed",
                "tick_size": tick_size_for_price(price),
                "step_size": "0.001",
                "min_notional": "0.01",
                "max_leverage": "20",
                "source": "market_snapshot_paper_bootstrap",
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
    registry_update = bootstrap_paper_instrument_registry(market)
    candidates = build_candidates(market, limit=limit)
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "market_ts": market.get("ts"), "candidate_count": len(candidates), "registry_update": {"added": registry_update["added"], "instrument_count": registry_update["instrument_count"]}, "candidates": candidates, "can_place_live_orders": False}
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
