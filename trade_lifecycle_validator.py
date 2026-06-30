"""Validate paper/shadow trade lifecycle events before learning uses them.

This module is deterministic and local-only. It never imports exchange clients
or order helpers; its job is to keep bad simulated trade rows out of memory,
skill forge, and promotion metrics.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION, validate_contract
from atomic_state import read_json, read_jsonl, write_json_atomic
from timebase import parse_utc, seconds_between, utc_now, validate_event_order

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
DEFAULT_EVENT_PATHS = [
    MEMORY_DIR / "paper_trades.jsonl",
    STATE_DIR / "paper_trades.jsonl",
    STATE_DIR / "scalp_autotrader.jsonl",
    MEMORY_DIR / "shadow_closes.jsonl",
]
LATEST_PATH = MEMORY_DIR / "trade_lifecycle_latest.json"
TRADE_LIFECYCLE_QUARANTINE = MEMORY_DIR / "trade_lifecycle_quarantine.jsonl"
PAPER_ACCOUNT = STATE_DIR / "paper_account.json"

OPEN_EVENTS = {"paper_open", "paper_trade_open", "trade_open", "open"}
CLOSE_EVENTS = {"paper_close", "paper_trade_close", "trade_close", "shadow_close", "close"}
OPEN_STATUSES = {"open", "opened"}
CLOSE_STATUSES = {"closed", "close", "tp", "sl", "timeout", "liquidated"}
ACTIVE_QUARANTINE_STATUSES = {"active", "approved"}


def safe_decimal(value: Any) -> Decimal | None:
    try:
        if value in (None, ""):
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def infer_trade_id(row: dict[str, Any]) -> str | None:
    for key in ("trade_id", "paper_trade_id", "close_id", "shadow_id"):
        if row.get(key):
            return str(row[key])
    return None


def infer_kind(row: dict[str, Any]) -> str | None:
    event = str(row.get("event") or "").lower()
    status = str(row.get("status") or "").lower()
    if event in OPEN_EVENTS or (status in OPEN_STATUSES and not row.get("close_ts")):
        return "open"
    if event in CLOSE_EVENTS or status in CLOSE_STATUSES or row.get("close_ts") or row.get("exit"):
        return "close"
    return None

def active_quarantined_trade_ids(entries: Iterable[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for row in entries:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").lower() not in ACTIVE_QUARANTINE_STATUSES:
            continue
        trade_id = row.get("trade_id")
        if trade_id:
            ids.add(str(trade_id))
    return ids

def quarantine_match(event: dict[str, Any], kind: str, entries: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    trade_id = str(event.get("trade_id") or "")
    if not trade_id:
        return None
    for row in entries:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").lower() not in ACTIVE_QUARANTINE_STATUSES:
            continue
        if str(row.get("trade_id") or "") != trade_id:
            continue
        kinds = row.get("kinds") if isinstance(row.get("kinds"), list) else []
        kind_value = str(row.get("kind") or "")
        scope = str(row.get("scope") or "trade")
        if scope == "event" and kinds and kind not in {str(item) for item in kinds}:
            continue
        if scope == "event" and kind_value and kind_value != kind:
            continue
        open_ts = row.get("open_ts")
        if open_ts and str(open_ts) != str(event.get("open_ts") or ""):
            continue
        return row
    return None


def canonical_trade_event(row: dict[str, Any], kind: str) -> dict[str, Any]:
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
    order_plan = row.get("order_plan") if isinstance(row.get("order_plan"), dict) else {}
    trade_id = infer_trade_id(row)
    entry = row.get("entry") or row.get("price") or signal.get("price")
    stop = row.get("sl") or row.get("stop") or row.get("stop_loss")
    tp = row.get("tp") or row.get("take_profit")
    close = row.get("exit") or row.get("close") or row.get("mark")
    mode = str(row.get("mode") or ("shadow" if row.get("shadow_id") else "paper"))
    base = {
        "schema_version": row.get("schema_version", SCHEMA_VERSION),
        "trade_id": trade_id,
        "mode": mode,
        "symbol": str(row.get("symbol") or signal.get("symbol") or "").upper(),
        "side": str(row.get("side") or signal.get("side") or "").upper(),
        "setup_id": row.get("setup_id") or signal.get("setup_id") or row.get("setup") or "unknown",
        "open_ts": row.get("open_ts") or row.get("entry_ts") or row.get("ts"),
        "entry": entry,
        "qty": row.get("qty") or row.get("quantity") or order_plan.get("qty") or order_plan.get("quantity"),
        "margin": row.get("margin") or order_plan.get("margin"),
        "leverage": row.get("leverage") or order_plan.get("leverage"),
        "sl": stop,
        "tp": tp,
        "risk_decision_id": row.get("risk_decision_id") or order_plan.get("risk_decision_id") or "unknown",
        "status": row.get("status") or ("closed" if kind == "close" else "open"),
        "market_snapshot_id": row.get("market_snapshot_id"),
        "market_snapshot_ts": row.get("market_snapshot_ts"),
        "news_snapshot_id": row.get("news_snapshot_id"),
        "news_snapshot_ts": row.get("news_snapshot_ts"),
        "reasoning_id": row.get("reasoning_id"),
    }
    if kind == "close":
        base.update(
            {
                "close_ts": row.get("close_ts") or row.get("exit_ts"),
                "exit": close,
                "fee": row.get("fee") or row.get("fees") or "0",
                "slippage": row.get("slippage") or "0",
            }
        )
    return base


def check_numeric(event: dict[str, Any], kind: str) -> list[str]:
    errors: list[str] = []
    positive_fields = ["entry", "qty", "margin", "leverage"]
    if kind == "close":
        positive_fields.append("exit")
    for field in positive_fields:
        value = safe_decimal(event.get(field))
        if value is None:
            errors.append(f"invalid_{field}")
        elif value <= 0:
            errors.append(f"non_positive_{field}")
    leverage = safe_decimal(event.get("leverage"))
    if leverage is not None and leverage > Decimal("125"):
        errors.append("leverage_above_exchange_sanity_cap")
    return errors


def check_price_geometry(event: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    entry = safe_decimal(event.get("entry"))
    sl = safe_decimal(event.get("sl"))
    tp = safe_decimal(event.get("tp"))
    side = str(event.get("side") or "").upper()
    if not entry or not sl or not tp:
        return warnings
    if side == "LONG" and not (sl < entry < tp):
        warnings.append("long_sl_tp_geometry_suspicious")
    if side == "SHORT" and not (tp < entry < sl):
        warnings.append("short_sl_tp_geometry_suspicious")
    return warnings


def check_snapshot_staleness(event: dict[str, Any], kind: str, max_snapshot_age_seconds: int) -> list[str]:
    errors: list[str] = []
    reference_ts = event.get("close_ts") if kind == "close" else event.get("open_ts")
    reference_label = "trade_close" if kind == "close" else "trade_open"
    for label in ("market", "news"):
        snapshot_ts = event.get(f"{label}_snapshot_ts")
        if not snapshot_ts:
            continue
        age = seconds_between(snapshot_ts, reference_ts)
        if age is None:
            errors.append(f"invalid_{label}_snapshot_ts")
        elif age < 0:
            errors.append(f"{label}_snapshot_after_{reference_label}")
        elif age > max_snapshot_age_seconds:
            errors.append(f"stale_{label}_snapshot")
    return errors


def validate_trade_events(
    rows: list[dict[str, Any]],
    max_open_age_seconds: int = 6 * 60 * 60,
    max_snapshot_age_seconds: int = 15 * 60,
    min_open_ts: Any = None,
    now: datetime | None = None,
    quarantine_entries: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    min_open = parse_utc(min_open_ts)
    quarantines = list(quarantine_entries or [])
    open_trades: dict[str, dict[str, Any]] = {}
    close_trades: set[str] = set()
    invalid_events: list[dict[str, Any]] = []
    quarantined_events: list[dict[str, Any]] = []
    valid_events = 0
    ignored_events = 0
    duplicate_opens = 0
    orphan_closes = 0

    for index, row in enumerate(rows):
        kind = infer_kind(row)
        if kind is None:
            ignored_events += 1
            continue
        event = canonical_trade_event(row, kind)
        opened_at = parse_utc(event.get("open_ts"))
        if min_open and opened_at and opened_at < min_open:
            ignored_events += 1
            continue
        trade_id = event.get("trade_id")
        quarantine = quarantine_match(event, kind, quarantines)
        if quarantine:
            quarantined_events.append(
                {
                    "index": index,
                    "trade_id": trade_id,
                    "kind": kind,
                    "reason": quarantine.get("reason") or "quarantined_trade_lifecycle",
                    "quarantine_id": quarantine.get("quarantine_id"),
                }
            )
            continue
        errors: list[str] = []
        warnings: list[str] = []
        contract = validate_contract("paper_close_event" if kind == "close" else "paper_trade_event", event)
        errors.extend(contract.errors)
        warnings.extend(contract.warnings)
        errors.extend(validate_event_order(event.get("open_ts"), event.get("close_ts") if kind == "close" else None))
        errors.extend(check_numeric(event, kind))
        errors.extend(check_snapshot_staleness(event, kind, max_snapshot_age_seconds))
        warnings.extend(check_price_geometry(event))
        if event.get("side") not in {"LONG", "SHORT"}:
            errors.append("invalid_side")
        if trade_id and kind == "open":
            if trade_id in open_trades:
                duplicate_opens += 1
                errors.append("duplicate_open")
            open_trades[str(trade_id)] = event
        if trade_id and kind == "close":
            if trade_id not in open_trades and not row.get("shadow_id"):
                orphan_closes += 1
                errors.append("orphan_close")
            close_trades.add(str(trade_id))
        if errors:
            invalid_events.append({"index": index, "trade_id": trade_id, "kind": kind, "errors": sorted(set(errors)), "warnings": warnings})
        else:
            valid_events += 1

    stale_open_trades: list[dict[str, Any]] = []
    for trade_id, event in open_trades.items():
        if trade_id in close_trades:
            continue
        opened = parse_utc(event.get("open_ts"))
        if opened and (current - opened).total_seconds() > max_open_age_seconds:
            stale_open_trades.append({"trade_id": trade_id, "open_ts": event.get("open_ts"), "symbol": event.get("symbol"), "side": event.get("side")})

    considered = valid_events + len(invalid_events)
    completeness = round(valid_events / considered, 6) if considered else 1.0
    status = "ok"
    if invalid_events or stale_open_trades:
        status = "degraded"
    if completeness < 0.99:
        status = "blocked_for_learning"
    return {
        "schema_version": SCHEMA_VERSION,
        "validated_at": utc_now(),
        "min_open_ts": min_open_ts,
        "status": status,
        "trade_lifecycle_completeness": completeness,
        "total_rows": len(rows),
        "considered_events": considered,
        "valid_events": valid_events,
        "invalid_events_count": len(invalid_events),
        "quarantined_events_count": len(quarantined_events),
        "ignored_events": ignored_events,
        "duplicate_opens": duplicate_opens,
        "orphan_closes": orphan_closes,
        "stale_open_trades": stale_open_trades,
        "invalid_events": invalid_events[:100],
        "quarantined_events": quarantined_events[:100],
        "learning_allowed": completeness >= 0.99 and not stale_open_trades,
    }


def read_trade_rows(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(read_jsonl(path))
    return rows


def write_latest_report(
    paths: Iterable[Path] = DEFAULT_EVENT_PATHS,
    output_path: Path = LATEST_PATH,
    min_open_ts: Any = None,
    quarantine_path: Path | None = None,
) -> dict[str, Any]:
    report = validate_trade_events(
        read_trade_rows(paths),
        min_open_ts=min_open_ts,
        quarantine_entries=read_jsonl(quarantine_path or TRADE_LIFECYCLE_QUARANTINE),
    )
    write_json_atomic(output_path, report)
    return report


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate paper/shadow trade lifecycle rows")
    parser.add_argument("--output", default=str(LATEST_PATH))
    parser.add_argument("--min-open-ts")
    parser.add_argument("--all-history", action="store_true")
    parser.add_argument("paths", nargs="*")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    paths = [Path(item) for item in args.paths] if args.paths else DEFAULT_EVENT_PATHS
    min_open_ts = None if args.all_history else args.min_open_ts or read_json(PAPER_ACCOUNT, default={}).get("created_at")
    report = write_latest_report(paths, Path(args.output), min_open_ts=min_open_ts)
    print(report)
    return 0 if report["status"] != "blocked_for_learning" else 2


if __name__ == "__main__":
    raise SystemExit(main())
