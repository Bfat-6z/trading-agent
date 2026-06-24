"""Preflight gate before paper actions and future live-review actions."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from instrument_registry import can_trade_paper, load_registry
from live_permission_firewall import evaluate_live_permission
from runtime_config import evaluate_mode, load_runtime_config
from timebase import parse_utc, seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
PRELIGHT_PATH = STATE_DIR / "preflight_latest.json"
MEMORY_DIR = STATE_DIR / "agent_memory"

DEFAULT_FRESHNESS = {
    "market_observer": 15 * 60,
    "news_observer": 60 * 60,
    "trade_lifecycle": 15 * 60,
}


def json_age_seconds(path: Path) -> float | None:
    payload = read_json(path, default={})
    ts = payload.get("updated_at") or payload.get("validated_at") or payload.get("checked_at") or payload.get("ts")
    if not parse_utc(ts):
        return None
    return seconds_between(ts, utc_now())


def check_freshness(paths: dict[str, Path], limits: dict[str, int] | None = None) -> tuple[list[str], list[str], dict[str, Any]]:
    limits = limits or DEFAULT_FRESHNESS
    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}
    for name, path in paths.items():
        if not path.exists():
            warnings.append(f"missing_{name}")
            details[name] = {"exists": False}
            continue
        age = json_age_seconds(path)
        details[name] = {"exists": True, "age_seconds": age}
        max_age = limits.get(name)
        if age is None:
            warnings.append(f"invalid_{name}_timestamp")
        elif max_age is not None and age > max_age:
            errors.append(f"stale_{name}")
    return errors, warnings, details


def run_preflight(
    action: dict[str, Any] | None = None,
    symbol: str | None = None,
    config: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
    output_path: Path = PRELIGHT_PATH,
) -> dict[str, Any]:
    action = action or {"action": "paper_decision"}
    config_eval = evaluate_mode(config or load_runtime_config())
    firewall = evaluate_live_permission(action, config_eval)
    errors: list[str] = []
    warnings: list[str] = []
    if config_eval.get("status") != "ok":
        errors.extend(config_eval.get("errors") or [])
        warnings.extend(config_eval.get("warnings") or [])
    if not firewall.get("allowed"):
        errors.extend(firewall.get("errors") or [])
    instrument_decision = None
    if symbol:
        instrument_decision = can_trade_paper(symbol, registry or load_registry())
        errors.extend(instrument_decision.get("errors") or [])
        warnings.extend(instrument_decision.get("warnings") or [])

    freshness_paths = {
        "trade_lifecycle": MEMORY_DIR / "trade_lifecycle_latest.json",
        "market_observer": STATE_DIR / "market_updates_latest.json",
        "news_observer": MEMORY_DIR / "news_latest.json",
    }
    fresh_errors, fresh_warnings, freshness = check_freshness(freshness_paths)
    warnings.extend(fresh_warnings)
    if action.get("requires_fresh_market", True):
        errors.extend(error for error in fresh_errors if error == "stale_market_observer")
    if action.get("requires_lifecycle_clean", True):
        lifecycle = read_json(freshness_paths["trade_lifecycle"], default={})
        if lifecycle and not lifecycle.get("learning_allowed", False):
            errors.append("trade_lifecycle_not_clean")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "action": action,
        "symbol": str(symbol or "").upper() or None,
        "allowed": not errors,
        "mode": config_eval.get("mode"),
        "reason": "ok" if not errors else ";".join(sorted(set(errors))),
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "firewall": firewall,
        "instrument": instrument_decision,
        "freshness": freshness,
    }
    write_json_atomic(output_path, payload)
    return payload


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local preflight gate")
    parser.add_argument("--symbol")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_preflight(symbol=args.symbol)
    print(result)
    return 0 if result["allowed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
