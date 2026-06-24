"""Deterministic market-learning benchmark for the paper agent.

The benchmark is an exam harness, not an executor. It scores simple market
scenarios against conservative expected actions and records lessons that other
learning loops can consume. It never opens paper or live orders.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import time
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
LATEST_PATH = MEMORY_DIR / "learning_exam_benchmark_latest.json"
HISTORY_PATH = MEMORY_DIR / "learning_exam_benchmark_history.jsonl"
HEARTBEAT_PATH = STATE_DIR / "learning_exam_benchmark_heartbeat.json"
PID_FILE = STATE_DIR / "learning_exam_benchmark.pid"
STOP_FILE = STATE_DIR / "STOP_LEARNING_EXAM_BENCHMARK"

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default

def scenario_id(name: str) -> str:
    return "scenario_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]

def default_scenarios() -> list[dict[str, Any]]:
    return [
        {
            "scenario_id": scenario_id("funding_squeeze_negative_funding_reversal"),
            "name": "funding_squeeze_negative_funding_reversal",
            "setup_id": "funding_squeeze",
            "features": {"funding_pct": -0.28, "range_pos": 0.08, "quote_volume": 180_000_000, "btc_regime": "neutral", "liquidity": "normal"},
            "expected_action": "paper_long",
            "lesson_on_fail": "Negative funding near range low with normal liquidity should stay eligible for paper-long testing.",
            "next_action_on_fail": "Review funding_squeeze entry filters and avoid routing this to exhaustion_fade.",
        },
        {
            "scenario_id": scenario_id("funding_squeeze_negative_funding_fail"),
            "name": "funding_squeeze_negative_funding_fail",
            "setup_id": "funding_squeeze",
            "features": {"funding_pct": -0.31, "range_pos": 0.62, "quote_volume": 170_000_000, "btc_regime": "bearish", "liquidity": "normal"},
            "expected_action": "skip",
            "lesson_on_fail": "Negative funding alone is not enough when price is not near the low and BTC regime conflicts.",
            "next_action_on_fail": "Add a regime/range-position block before paper-long funding squeeze entries.",
        },
        {
            "scenario_id": scenario_id("exhaustion_fade_overextended_short"),
            "name": "exhaustion_fade_overextended_short",
            "setup_id": "exhaustion_fade",
            "features": {"change_pct": 34.0, "range_pos": 0.91, "quote_volume": 220_000_000, "btc_regime": "neutral", "skill_blocked": True},
            "expected_action": "shadow_only",
            "lesson_on_fail": "Weak or patched exhaustion_fade evidence should stay shadow-only even when raw overextension looks attractive.",
            "next_action_on_fail": "Keep min-score/paper-only patch enforcement ahead of raw candidate score.",
        },
        {
            "scenario_id": scenario_id("thin_liquidity_no_trade"),
            "name": "thin_liquidity_no_trade",
            "setup_id": "funding_squeeze",
            "features": {"funding_pct": -0.35, "range_pos": 0.05, "quote_volume": 2_000_000, "btc_regime": "neutral", "liquidity": "thin"},
            "expected_action": "skip",
            "lesson_on_fail": "Strong funding signal should still skip when liquidity is too thin for realistic fills.",
            "next_action_on_fail": "Require liquidity/volume gate before paper orders.",
        },
        {
            "scenario_id": scenario_id("btc_regime_conflict_no_trade"),
            "name": "btc_regime_conflict_no_trade",
            "setup_id": "funding_squeeze",
            "features": {"funding_pct": -0.22, "range_pos": 0.09, "quote_volume": 120_000_000, "btc_regime": "hard_down", "liquidity": "normal"},
            "expected_action": "skip",
            "lesson_on_fail": "Alt long setups should skip when BTC regime is hard down unless separate reclaim evidence exists.",
            "next_action_on_fail": "Add BTC regime conflict to funding_squeeze paper gate.",
        },
    ]

def decide_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    features = scenario.get("features") if isinstance(scenario.get("features"), dict) else {}
    setup_id = str(scenario.get("setup_id") or "unknown")
    funding = safe_float(features.get("funding_pct"))
    range_pos = safe_float(features.get("range_pos"), 0.5)
    quote_volume = safe_float(features.get("quote_volume"))
    change_pct = safe_float(features.get("change_pct"))
    btc_regime = str(features.get("btc_regime") or "neutral")
    liquidity = str(features.get("liquidity") or "normal")
    skill_blocked = bool(features.get("skill_blocked"))
    reasons: list[str] = []
    action = "skip"

    if liquidity == "thin" or quote_volume < 20_000_000:
        reasons.append("liquidity_too_thin")
    elif btc_regime in {"hard_down", "hard_up"}:
        reasons.append("btc_regime_conflict")
    elif setup_id == "funding_squeeze" and funding <= -0.15 and range_pos <= 0.45:
        action = "paper_long"
        reasons.append("negative_funding_near_range_low")
    elif setup_id == "funding_squeeze" and funding >= 0.15 and range_pos >= 0.55:
        action = "paper_short"
        reasons.append("positive_funding_near_range_high")
    elif setup_id == "exhaustion_fade" and skill_blocked:
        action = "shadow_only"
        reasons.append("setup_evidence_blocked")
    elif setup_id == "exhaustion_fade" and change_pct >= 18 and range_pos >= 0.72:
        action = "paper_short"
        reasons.append("overextended_gainer")
    else:
        reasons.append("no_tradeable_edge")

    return {"action": action, "reasons": reasons}

def grade_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    decision = decide_scenario(scenario)
    expected = str(scenario.get("expected_action") or "skip")
    passed = decision["action"] == expected
    return {
        "scenario_id": scenario.get("scenario_id"),
        "name": scenario.get("name"),
        "setup_id": scenario.get("setup_id"),
        "expected_action": expected,
        "actual_action": decision["action"],
        "passed": passed,
        "reasons": decision["reasons"],
        "lesson": "" if passed else scenario.get("lesson_on_fail"),
        "next_action": "" if passed else scenario.get("next_action_on_fail"),
        "can_place_live_orders": False,
    }

def run_benchmark(scenarios: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = [grade_scenario(row) for row in (scenarios or default_scenarios())]
    passed = sum(1 for row in rows if row.get("passed"))
    failed = len(rows) - passed
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "scenario_count": len(rows),
        "passed_count": passed,
        "failed_count": failed,
        "score": round(passed / len(rows), 4) if rows else 1.0,
        "rows": rows,
        "lessons": [row for row in rows if not row.get("passed")],
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    return payload

def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> None:
    write_json_atomic(HEARTBEAT_PATH, {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})})

def run_once(output_path: Path = LATEST_PATH, history_path: Path = HISTORY_PATH) -> dict[str, Any]:
    payload = run_benchmark()
    write_json_atomic(output_path, payload)
    append_jsonl(history_path, payload)
    write_heartbeat("ok", {"score": payload["score"], "failed_count": payload["failed_count"]})
    return payload

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic learning exam benchmark")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=3600.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    return args

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        result = run_once()
        print(f"learning_exam_benchmark score={result.get('score')} failed={result.get('failed_count')}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
