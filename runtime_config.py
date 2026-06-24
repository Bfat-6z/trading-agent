"""Runtime mode governance for paper/shadow learning."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from atomic_state import read_json, write_json_atomic
from timebase import utc_now


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
CONFIG_PATH = ROOT / "runtime_config.json"
EFFECTIVE_CONFIG_PATH = STATE_DIR / "runtime_config_effective.json"

MODES = {
    "observe_only",
    "shadow_learning",
    "paper_learning",
    "paper_exploration",
    "degraded_safe",
    "live_review_candidate",
    "live_execution_disabled",
}


def default_config() -> dict:
    return {
        "schema_version": 1,
        "mode": "paper_learning",
        "live_execution_enabled": False,
        "paper_account_usdt": 100.0,
        "feature_flags": {
            "paper_trading": True,
            "paper_exploration": False,
            "live_orders": False,
        },
    }


def load_runtime_config(path: Path = CONFIG_PATH, env: dict[str, str] | None = None) -> dict:
    env = env or os.environ
    config = default_config()
    file_config = read_json(path, default={})
    if isinstance(file_config, dict):
        config.update(file_config)
        if isinstance(file_config.get("feature_flags"), dict):
            config["feature_flags"] = {**default_config()["feature_flags"], **file_config["feature_flags"]}
    mode = str(env.get("TRADING_AGENT_MODE") or config.get("mode") or "paper_learning")
    flags = dict(config.get("feature_flags") or {})
    if env.get("TRADING_AGENT_LIVE_ORDERS", "").lower() in {"1", "true", "yes"}:
        flags["live_orders"] = True
    config["mode"] = mode
    config["feature_flags"] = flags
    return config


def evaluate_mode(config: dict[str, Any]) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    mode = str(config.get("mode") or "")
    flags = dict(config.get("feature_flags") or {})
    if mode not in MODES:
        errors.append("unknown_mode")
        mode = "degraded_safe"
    if flags.get("live_orders") or config.get("live_execution_enabled"):
        errors.append("live_execution_not_allowed_in_phase_a")
        flags["live_orders"] = False
        config["live_execution_enabled"] = False
        mode = "degraded_safe"
    if mode.startswith("live") and not flags.get("live_orders"):
        warnings.append("live_review_mode_without_live_orders")
    effective = {**config, "mode": mode, "feature_flags": flags, "errors": errors, "warnings": warnings, "evaluated_at": utc_now()}
    effective["status"] = "degraded" if errors else "ok"
    return effective


def write_effective_config(path: Path = EFFECTIVE_CONFIG_PATH) -> dict:
    effective = evaluate_mode(load_runtime_config())
    write_json_atomic(path, effective)
    return effective
