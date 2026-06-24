"""Shadow trade records for blocked or sleeping executor states.

Shadow trades are would-trade observations only. They never place orders and
never mutate live or paper positions. The goal is to collect learning samples
when risk gates correctly prevent execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from event_store import safe_append_event

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
SHADOW_JSONL = MEMORY_DIR / "shadow_trades.jsonl"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def as_str(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def safe_decimal(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def make_shadow_id(signal: dict, ts: str, block_reason: str) -> str:
    raw = canonical_json(
        {
            "ts": ts,
            "symbol": signal.get("symbol"),
            "side": signal.get("side"),
            "price": signal.get("price"),
            "score": signal.get("score"),
            "block_reason": block_reason,
        }
    )
    return "shadow_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_shadow_open(
    signal: dict,
    order_plan: dict,
    entry: Decimal | str | float,
    stop: Decimal | str | float,
    take_profit: Decimal | str | float,
    block_reason: str,
    critic: dict | None = None,
    ts: str | None = None,
) -> dict:
    row_ts = ts or utc_now()
    entry_dec = safe_decimal(entry)
    stop_dec = safe_decimal(stop)
    tp_dec = safe_decimal(take_profit)
    side = str(signal.get("side") or "").upper()
    if side == "LONG":
        risk_pct = (entry_dec - stop_dec) / entry_dec * Decimal("100") if entry_dec else Decimal("0")
        reward_pct = (tp_dec - entry_dec) / entry_dec * Decimal("100") if entry_dec else Decimal("0")
    else:
        risk_pct = (stop_dec - entry_dec) / entry_dec * Decimal("100") if entry_dec else Decimal("0")
        reward_pct = (entry_dec - tp_dec) / entry_dec * Decimal("100") if entry_dec else Decimal("0")
    payload = {
        "shadow_id": make_shadow_id(signal, row_ts, block_reason),
        "ts": row_ts,
        "status": "open",
        "block_reason": str(block_reason or "unknown"),
        "signal": signal,
        "order_plan": order_plan,
        "entry": as_str(entry_dec),
        "stop": as_str(stop_dec),
        "take_profit": as_str(tp_dec),
        "risk_pct": float(round(risk_pct, 6)),
        "reward_pct": float(round(reward_pct, 6)),
        "critic": critic or {},
        "no_execution": True,
    }
    return payload


def evaluate_shadow_trade(shadow: dict, mark: Decimal | str | float, fee_rate: Decimal | str | float = "0.0005") -> dict:
    mark_dec = safe_decimal(mark)
    entry = safe_decimal(shadow.get("entry"))
    stop = safe_decimal(shadow.get("stop"))
    tp = safe_decimal(shadow.get("take_profit"))
    side = str((shadow.get("signal") or {}).get("side") or "").upper()
    notional = safe_decimal((shadow.get("order_plan") or {}).get("notional"), "1")
    fee = safe_decimal(fee_rate) * Decimal("2")
    if side == "LONG":
        hit_tp = mark_dec >= tp
        hit_sl = mark_dec <= stop
        pnl_pct = (mark_dec - entry) / entry if entry else Decimal("0")
    else:
        hit_tp = mark_dec <= tp
        hit_sl = mark_dec >= stop
        pnl_pct = (entry - mark_dec) / entry if entry else Decimal("0")
    gross = notional * pnl_pct
    fees = notional * fee
    net = gross - fees
    return {
        **shadow,
        "status": "closed" if hit_tp or hit_sl else "open",
        "mark": as_str(mark_dec),
        "close_reason": "tp" if hit_tp else "sl" if hit_sl else None,
        "gross": as_str(gross),
        "fees": as_str(fees),
        "net": as_str(net),
    }


def append_shadow(path: Path, shadow: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(canonical_json(shadow) + "\n")
    safe_append_event("shadow_trade_logger", "shadow_open", shadow, ts=shadow.get("ts"))


def read_tail(path: Path = SHADOW_JSONL, max_lines: int = 20) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]:
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
        except Exception:
            continue
    return rows


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect shadow would-trade samples")
    parser.add_argument("--tail", type=int, default=10)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    print(json.dumps(read_tail(SHADOW_JSONL, args.tail), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
