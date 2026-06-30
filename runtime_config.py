"""Runtime mode governance for paper/shadow learning."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

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
ENV_ALLOWLIST = {
    "TRADING_AGENT_LIVE_ORDERS",
    "TRADING_AGENT_MODE",
    "TRADING_AGENT_PAPER_ACCOUNT_USDT",
    "TRADING_AGENT_PAPER_EXPLORATION",
}
LIVE_CREDENTIAL_ENV_KEYS = {
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_FUTURES_API_KEY",
    "BINANCE_FUTURES_API_SECRET",
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "PRIVATE_KEY",
    "WALLET_PRIVATE_KEY",
}


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="ignore")).hexdigest()[:12]


def _bool_env(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def _config_path(path: Path | None) -> Path:
    selected = path or CONFIG_PATH
    if selected.is_absolute():
        return selected.resolve()
    return (ROOT / selected).resolve()


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


def load_runtime_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> dict:
    env = env or os.environ
    config = default_config()
    config_path = _config_path(path)
    file_config = read_json(config_path, default={})
    if isinstance(file_config, dict):
        config.update(file_config)
        if isinstance(file_config.get("feature_flags"), dict):
            config["feature_flags"] = {**default_config()["feature_flags"], **file_config["feature_flags"]}

    warnings: list[str] = []
    errors: list[str] = []
    flags = dict(config.get("feature_flags") or {})
    env_used: dict[str, str] = {}
    blocked_live_env = []
    for key, value in env.items():
        if key in LIVE_CREDENTIAL_ENV_KEYS and value:
            blocked_live_env.append({"key": key, "value_fingerprint": _fingerprint(value)})
        if key not in ENV_ALLOWLIST:
            continue
        env_used[key] = _fingerprint(value)
        if key == "TRADING_AGENT_MODE" and value:
            config["mode"] = str(value)
        elif key == "TRADING_AGENT_LIVE_ORDERS":
            parsed = _bool_env(value)
            if parsed is True:
                flags["live_orders"] = True
            elif parsed is None:
                warnings.append("invalid_env_TRADING_AGENT_LIVE_ORDERS")
        elif key == "TRADING_AGENT_PAPER_EXPLORATION":
            parsed = _bool_env(value)
            if parsed is not None:
                flags["paper_exploration"] = parsed
            else:
                warnings.append("invalid_env_TRADING_AGENT_PAPER_EXPLORATION")
        elif key == "TRADING_AGENT_PAPER_ACCOUNT_USDT":
            try:
                config["paper_account_usdt"] = float(value)
            except (TypeError, ValueError):
                warnings.append("invalid_env_TRADING_AGENT_PAPER_ACCOUNT_USDT")

    if blocked_live_env:
        errors.append("live_trading_env_keys_present_phase_a")
    config["feature_flags"] = flags
    config["runtime_config"] = {
        "config_source_path": str(config_path),
        "dotenv_loaded": False,
        "implicit_cwd_dotenv_allowed": False,
        "env_allowlist": sorted(ENV_ALLOWLIST),
        "env_used_fingerprints": env_used,
        "blocked_live_env_fingerprints": blocked_live_env,
        "source_fingerprint": _fingerprint({"path": str(config_path), "file": file_config, "env": env_used}),
    }
    config["config_errors"] = errors
    config["config_warnings"] = warnings
    return config


def evaluate_mode(config: dict[str, Any]) -> dict:
    errors: list[str] = list(config.get("config_errors") or [])
    warnings: list[str] = list(config.get("config_warnings") or [])
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
    effective = {
        **config,
        "mode": mode,
        "feature_flags": flags,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "evaluated_at": utc_now(),
        "live_permission": False,
        "can_place_live_orders": False,
    }
    effective["status"] = "degraded" if errors else "ok"
    return effective


def write_effective_config(path: Path = EFFECTIVE_CONFIG_PATH) -> dict:
    effective = evaluate_mode(load_runtime_config())
    write_json_atomic(path, effective)
    return effective
