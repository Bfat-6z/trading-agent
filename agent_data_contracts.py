"""Versioned data contracts for learning artifacts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]

    def payload(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


REQUIRED_FIELDS: dict[str, set[str]] = {
    "paper_trade_event": {
        "trade_id",
        "mode",
        "symbol",
        "side",
        "setup_id",
        "open_ts",
        "entry",
        "qty",
        "margin",
        "leverage",
        "sl",
        "tp",
        "risk_decision_id",
        "status",
    },
    "paper_close_event": {
        "trade_id",
        "mode",
        "symbol",
        "side",
        "setup_id",
        "open_ts",
        "close_ts",
        "entry",
        "exit",
        "qty",
        "margin",
        "leverage",
        "fee",
        "slippage",
        "risk_decision_id",
        "status",
    },
    "episode": {"episode_id", "trigger", "goal", "decision", "actions", "outcome", "quality"},
    "risk_decision": {"risk_decision_id", "can_open_paper", "reason"},
    "instrument": {"symbol", "status", "tick_size", "step_size", "min_notional", "max_leverage"},
}


def validate_contract(kind: str, payload: dict[str, Any]) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    required = REQUIRED_FIELDS.get(kind)
    if required is None:
        return ValidationResult(False, ["unknown_contract"], [])
    missing = sorted(field for field in required if payload.get(field) in (None, ""))
    if missing:
        errors.append("missing:" + ",".join(missing))
    version = payload.get("schema_version")
    if version is not None and int(version) > SCHEMA_VERSION:
        errors.append("future_schema_version")
    if version is None:
        warnings.append("missing_schema_version")
    return ValidationResult(not errors, errors, warnings)


def require_contract(kind: str, payload: dict[str, Any]) -> None:
    result = validate_contract(kind, payload)
    if not result.ok:
        raise ValueError(f"contract {kind} failed: {result.errors}")
