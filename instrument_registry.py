"""Local instrument registry for paper-trading eligibility."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION, validate_contract
from atomic_state import read_json, write_json_atomic
from timebase import parse_utc, seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
REGISTRY_PATH = STATE_DIR / "instrument_registry.json"
QUALITY_PATH = STATE_DIR / "agent_memory" / "universe_quality_latest.json"

DEFAULT_STALE_SECONDS = 24 * 60 * 60


def registry_version(payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    return str(payload.get("registry_version") or payload.get("updated_at") or "unversioned")


def canonical_instrument_id(symbol: str, contract_type: str = "PERPETUAL", venue: str = "binance_usdm") -> str:
    return f"{venue}:{normalize_symbol(symbol)}:{str(contract_type or 'PERPETUAL').upper()}"


def instrument_snapshot_id(symbol: str, registry: dict[str, Any], row: dict[str, Any] | None = None) -> str:
    material = {
        "registry_version": registry_version(registry),
        "symbol": normalize_symbol(symbol),
        "row": row or (registry.get("instruments") or {}).get(normalize_symbol(symbol)) or {},
    }
    raw = json.dumps(material, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))
    return "instrument_snapshot_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").upper().replace("/", "").replace(":", "")


def default_registry() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "registry_version": "empty", "updated_at": utc_now(), "instruments": {}}


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    payload = read_json(path, default={})
    if not isinstance(payload, dict) or not payload:
        return default_registry()
    if isinstance(payload.get("instruments"), list):
        payload["instruments"] = {normalize_symbol(item.get("symbol")): item for item in payload["instruments"] if isinstance(item, dict)}
    payload.setdefault("instruments", {})
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("registry_version", registry_version(payload))
    return payload


def validate_instrument(row: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    contract = validate_contract("instrument", row)
    errors = list(contract.errors)
    warnings = list(contract.warnings)
    if str(row.get("status") or "").lower() not in {"trading", "paper_allowed"}:
        errors.append("instrument_not_trading")
    for field in ("tick_size", "step_size", "min_notional", "max_leverage"):
        try:
            if float(row.get(field)) <= 0:
                errors.append(f"non_positive_{field}")
        except Exception:
            errors.append(f"invalid_{field}")
    return not errors, sorted(set(errors)), warnings


def get_instrument(symbol: str, registry: dict[str, Any] | None = None) -> dict[str, Any] | None:
    registry = registry or load_registry()
    return (registry.get("instruments") or {}).get(normalize_symbol(symbol))


def is_registry_stale(registry: dict[str, Any], stale_seconds: int = DEFAULT_STALE_SECONDS) -> bool:
    updated_at = registry.get("updated_at")
    if not parse_utc(updated_at):
        return True
    age = seconds_between(updated_at, utc_now())
    return age is None or age > stale_seconds


def can_trade_paper(symbol: str, registry: dict[str, Any] | None = None, allow_missing: bool = False) -> dict[str, Any]:
    registry = registry or load_registry()
    symbol_up = normalize_symbol(symbol)
    row = get_instrument(symbol_up, registry)
    errors: list[str] = []
    warnings: list[str] = []
    if is_registry_stale(registry):
        errors.append("instrument_registry_stale")
    if row is None:
        if allow_missing:
            warnings.append("instrument_missing_allowed_for_shadow")
        else:
            errors.append("instrument_missing")
    else:
        ok, row_errors, row_warnings = validate_instrument(row)
        if not ok:
            errors.extend(row_errors)
        warnings.extend(row_warnings)
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "symbol": symbol_up,
        "registry_version": registry_version(registry),
        "canonical_instrument_id": canonical_instrument_id(symbol_up, str((row or {}).get("contract_type") or "PERPETUAL")),
        "instrument_snapshot_id": instrument_snapshot_id(symbol_up, registry, row),
        "price_basis_contract": {"fills": "BOOK_MID/LAST+slippage", "mark": "MARK", "candles": "CANDLE_CLOSE"},
        "can_trade_paper": not errors,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "instrument": row,
    }


def write_registry(instruments: list[dict[str, Any]], path: Path = REGISTRY_PATH) -> dict[str, Any]:
    rows = {normalize_symbol(item.get("symbol")): {**item, "symbol": normalize_symbol(item.get("symbol"))} for item in instruments}
    payload = {"schema_version": SCHEMA_VERSION, "registry_version": utc_now(), "updated_at": utc_now(), "instruments": rows}
    write_json_atomic(path, payload)
    write_json_atomic(QUALITY_PATH, summarize_registry(payload))
    return payload

def filter_value(filters: list[dict[str, Any]], filter_type: str, key: str, default: Any = None) -> Any:
    for item in filters:
        if isinstance(item, dict) and item.get("filterType") == filter_type:
            return item.get(key, default)
    return default

def instruments_from_exchange_info(exchange_info: dict[str, Any], leverage_brackets: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    brackets = leverage_brackets or {}
    rows: list[dict[str, Any]] = []
    for raw in exchange_info.get("symbols", []) if isinstance(exchange_info.get("symbols"), list) else []:
        if not isinstance(raw, dict):
            continue
        symbol = normalize_symbol(raw.get("symbol"))
        if not symbol:
            continue
        filters = raw.get("filters") if isinstance(raw.get("filters"), list) else []
        bracket = brackets.get(symbol) if isinstance(brackets.get(symbol), dict) else {}
        row = {
            "schema_version": SCHEMA_VERSION,
            "symbol": symbol,
            "canonical_instrument_id": canonical_instrument_id(symbol, str(raw.get("contractType") or raw.get("contract_type") or "PERPETUAL")),
            "contract_type": str(raw.get("contractType") or raw.get("contract_type") or "PERPETUAL").upper(),
            "base_asset": raw.get("baseAsset") or raw.get("base_asset"),
            "quote_asset": raw.get("quoteAsset") or raw.get("quote_asset"),
            "margin_asset": raw.get("marginAsset") or raw.get("margin_asset") or raw.get("quoteAsset") or raw.get("quote_asset"),
            "status": str(raw.get("status") or "unknown").lower(),
            "tick_size": filter_value(filters, "PRICE_FILTER", "tickSize", raw.get("tick_size") or "0"),
            "step_size": filter_value(filters, "LOT_SIZE", "stepSize", raw.get("step_size") or "0"),
            "min_notional": filter_value(filters, "MIN_NOTIONAL", "notional", raw.get("min_notional") or "5"),
            "max_leverage": bracket.get("max_leverage") or raw.get("max_leverage") or "20",
            "leverage_bracket_source": bracket.get("source", "metadata_default"),
        }
        rows.append(row)
    return rows

def normalize_leverage_brackets(payload: Any) -> dict[str, dict[str, Any]]:
    rows = payload if isinstance(payload, list) else payload.get("brackets", []) if isinstance(payload, dict) else []
    result: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = normalize_symbol(item.get("symbol"))
        brackets = item.get("brackets") if isinstance(item.get("brackets"), list) else []
        initial = item.get("initialLeverage") or item.get("max_leverage")
        if brackets and isinstance(brackets[0], dict):
            initial = brackets[0].get("initialLeverage") or initial
        if symbol and initial:
            result[symbol] = {"max_leverage": str(initial), "source": "exchange_leverage_brackets"}
    return result

def refresh_registry_from_exchange_info(exchange_info: dict[str, Any], leverage_payload: Any | None = None, path: Path = REGISTRY_PATH) -> dict[str, Any]:
    brackets = normalize_leverage_brackets(leverage_payload or {})
    instruments = instruments_from_exchange_info(exchange_info, brackets)
    payload = write_registry(instruments, path=path)
    payload["source"] = "exchange_info_payload"
    payload["leverage_bracket_count"] = len(brackets)
    write_json_atomic(path, payload)
    write_json_atomic(QUALITY_PATH, summarize_registry(payload))
    return payload


def summarize_registry(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_registry()
    instruments = registry.get("instruments") or {}
    invalid = []
    for symbol, row in instruments.items():
        ok, errors, _ = validate_instrument(row)
        if not ok:
            invalid.append({"symbol": symbol, "errors": errors})
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "registry_version": registry_version(registry),
        "instrument_count": len(instruments),
        "invalid_count": len(invalid),
        "stale": is_registry_stale(registry),
        "invalid": invalid[:100],
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect local instrument registry")
    parser.add_argument("--symbol")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.symbol:
        print(can_trade_paper(args.symbol))
    else:
        summary = summarize_registry()
        write_json_atomic(QUALITY_PATH, summary)
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
