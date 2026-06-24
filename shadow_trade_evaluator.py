"""Evaluate shadow would-trades against public market candles.

This module is intentionally read-only toward exchanges. It uses public USD-M
futures market data, never loads account keys, and never imports order helpers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from event_store import safe_append_event

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
REPORTS_DIR = ROOT / "plans" / "reports"
SHADOW_JSONL = MEMORY_DIR / "shadow_trades.jsonl"
SCALP_JSONL = STATE_DIR / "scalp_autotrader.jsonl"
SHADOW_CLOSE_JSONL = MEMORY_DIR / "shadow_closes.jsonl"
SHADOW_PERFORMANCE_JSON = MEMORY_DIR / "shadow_performance_latest.json"
KLINE_CACHE_DIR = STATE_DIR / "market_data_cache" / "klines"
RATE_LIMIT_STATE_JSON = STATE_DIR / "shadow_evaluator_rate_limit.json"

SCHEMA_VERSION = 1
BINANCE_USDM_KLINES = "https://fapi.binance.com/fapi/v1/klines"
MAX_BINANCE_LIMIT = 1500
DEFAULT_FRESH_WINDOW_START = "2026-06-24T00:00:00+00:00"


class MarketDataError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def rate_limited(self) -> bool:
        return self.status_code in {418, 429}


@dataclass(frozen=True)
class Assumptions:
    interval: str = "1m"
    fee_rate: str = "0.0005"
    slippage_bps: str = "2"
    max_hold_seconds: int = 180
    ambiguity_policy: str = "sl_first"
    skip_entry_partial: bool = True
    allow_incomplete_candle: bool = False

    def payload(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "interval": self.interval,
            "fee_rate": self.fee_rate,
            "slippage_bps": self.slippage_bps,
            "max_hold_seconds": self.max_hold_seconds,
            "ambiguity_policy": self.ambiguity_policy,
            "skip_entry_partial": self.skip_entry_partial,
            "allow_incomplete_candle": self.allow_incomplete_candle,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%y%m%d-%H%M%S")


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_short(value: str, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def assumption_hash(assumptions: Assumptions) -> str:
    return sha256_short(canonical_json(assumptions.payload()), 16)


def close_id(shadow_id: str, assumptions_hash: str) -> str:
    return "shadow_close_" + sha256_short(f"{shadow_id}:{assumptions_hash}", 24)


def safe_decimal(value: object, default: str = "0") -> Decimal:
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def dec_str(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def ts_ms(value: object) -> int | None:
    parsed = parse_ts(value)
    if not parsed:
        return None
    return int(parsed.timestamp() * 1000)


def row_event_ms(row: dict) -> int | None:
    return ts_ms(row.get("entry_ts") or row.get("close_ts") or row.get("ts") or row.get("created_at"))


def rows_since(rows: list[dict], start_ts: str | None) -> list[dict]:
    start_ms = ts_ms(start_ts)
    if start_ms is None:
        return list(rows)
    return [row for row in rows if (row_event_ms(row) or 0) >= start_ms]


def ms_iso(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
        except Exception:
            continue
    return rows


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(canonical_json(row) + "\n")


def existing_close_ids(path: Path) -> set[str]:
    return {str(row.get("close_id")) for row in read_jsonl(path) if row.get("close_id")}


def rate_limit_backoff(path: Path = RATE_LIMIT_STATE_JSON, now: float | None = None) -> dict:
    payload = read_json(path)
    now = time.time() if now is None else now
    until_epoch = safe_float(payload.get("backoff_until_epoch"))
    if until_epoch > now:
        return {
            "active": True,
            "backoff_until_epoch": until_epoch,
            "backoff_until": payload.get("backoff_until"),
            "reason": payload.get("reason") or "rate_limited",
            "last_status_code": payload.get("last_status_code"),
        }
    return {"active": False}


def record_rate_limit_backoff(
    error: str,
    status_code: int | None,
    cooldown_seconds: int,
    path: Path = RATE_LIMIT_STATE_JSON,
    now: float | None = None,
) -> dict:
    now = time.time() if now is None else now
    until_epoch = now + max(60, int(cooldown_seconds))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "reason": str(error)[:240],
        "last_status_code": status_code,
        "cooldown_seconds": max(60, int(cooldown_seconds)),
        "backoff_until_epoch": until_epoch,
        "backoff_until": datetime.fromtimestamp(until_epoch, tz=timezone.utc).isoformat(timespec="seconds"),
    }
    write_json_atomic(path, payload)
    return payload


def read_shadow_opens(paths: list[Path]) -> tuple[list[dict], dict]:
    seen: set[str] = set()
    rows: list[dict] = []
    stats = Counter()
    for path in paths:
        for row in read_jsonl(path):
            event = row.get("event")
            if event and event != "shadow_open":
                continue
            if row.get("status") and row.get("status") != "open":
                continue
            shadow_id = row.get("shadow_id")
            if not shadow_id:
                stats["missing_shadow_id"] += 1
                continue
            if shadow_id in seen:
                stats["duplicates"] += 1
                continue
            seen.add(str(shadow_id))
            rows.append(row)
    stats["opens"] = len(rows)
    return rows, dict(stats)


def parse_kline(row: object) -> dict | None:
    try:
        if isinstance(row, dict):
            return {
                "open_time": int(row["open_time"]),
                "open": safe_decimal(row["open"]),
                "high": safe_decimal(row["high"]),
                "low": safe_decimal(row["low"]),
                "close": safe_decimal(row["close"]),
                "close_time": int(row["close_time"]),
            }
        if isinstance(row, list) and len(row) >= 7:
            return {
                "open_time": int(row[0]),
                "open": safe_decimal(row[1]),
                "high": safe_decimal(row[2]),
                "low": safe_decimal(row[3]),
                "close": safe_decimal(row[4]),
                "close_time": int(row[6]),
            }
    except Exception:
        return None
    return None


def parse_klines(rows: list[object]) -> list[dict]:
    parsed = [parse_kline(row) for row in rows]
    return sorted([row for row in parsed if row], key=lambda item: item["open_time"])


def interval_ms(interval: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    try:
        return int(interval[:-1]) * units[interval[-1]]
    except Exception:
        return 60_000


def cache_path(symbol: str, interval: str, start_ms: int, end_ms: int) -> Path:
    safe = f"{symbol.upper()}-{interval}-{start_ms}-{end_ms}.json"
    return KLINE_CACHE_DIR / safe


def fetch_klines(
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str = "1m",
    timeout: int = 10,
    use_cache: bool = True,
    sleep_seconds: float = 0.05,
) -> list[dict]:
    if end_ms <= start_ms:
        return []
    path = cache_path(symbol, interval, start_ms, end_ms)
    if use_cache and path.exists():
        return parse_klines(json.loads(path.read_text(encoding="utf-8", errors="ignore")))
    raw: list[object] = []
    cursor = start_ms
    step_ms = interval_ms(interval)
    while cursor <= end_ms:
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": MAX_BINANCE_LIMIT,
        }
        req = Request(f"{BINANCE_USDM_KLINES}?{urlencode(params)}", headers={"User-Agent": "trading-agent-shadow-evaluator/1.0"})
        try:
            with urlopen(req, timeout=timeout) as response:
                page = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:180]
            except Exception:
                detail = ""
            raise MarketDataError(f"http_{exc.code} {detail}".strip(), exc.code) from exc
        except URLError as exc:
            raise MarketDataError(f"url_error {str(exc)[:180]}") from exc
        if not isinstance(page, list) or not page:
            break
        raw.extend(page)
        last = parse_kline(page[-1])
        if not last:
            break
        next_cursor = int(last["open_time"]) + step_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(page) < MAX_BINANCE_LIMIT:
            break
        time.sleep(max(0.0, sleep_seconds))
    if use_cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
    return parse_klines(raw)


def malformed_close(shadow: dict, run_id: str, assumptions: Assumptions, reason: str, error: str | None = None) -> dict:
    ahash = assumption_hash(assumptions)
    shadow_id = str(shadow.get("shadow_id") or "missing")
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "shadow_close",
        "run_id": run_id,
        "assumption_hash": ahash,
        "close_id": close_id(shadow_id, ahash),
        "shadow_id": shadow.get("shadow_id"),
        "symbol": ((shadow.get("signal") or {}).get("symbol") or shadow.get("symbol")),
        "side": ((shadow.get("signal") or {}).get("side") or shadow.get("side")),
        "entry_ts": shadow.get("ts"),
        "close_ts": None,
        "entry": str(shadow.get("entry") or ""),
        "close": None,
        "stop": str(shadow.get("stop") or ""),
        "take_profit": str(shadow.get("take_profit") or ""),
        "reason": reason,
        "status": "skipped",
        "gross": "0",
        "fees": "0",
        "slippage": "0",
        "net": "0",
        "duration_seconds": None,
        "data_quality": {"interval": assumptions.interval, "candle_count": 0, "ambiguous": False, "incomplete_last_candle": False, "source": "binance_usdm_klines", "error": error},
        "assumptions": assumptions.payload(),
    }


def adverse_close_price(side: str, reason: str, price: Decimal, slippage_bps: Decimal) -> tuple[Decimal, Decimal]:
    multiplier = slippage_bps / Decimal("10000")
    if multiplier <= 0:
        return price, Decimal("0")
    if side == "LONG":
        adjusted = price * (Decimal("1") - multiplier) if reason == "tp" else price * (Decimal("1") - multiplier)
    else:
        adjusted = price * (Decimal("1") + multiplier) if reason == "tp" else price * (Decimal("1") + multiplier)
    return adjusted, abs(adjusted - price)


def pnl_for(side: str, entry: Decimal, close: Decimal, notional: Decimal) -> Decimal:
    if not entry:
        return Decimal("0")
    pct = (close - entry) / entry if side == "LONG" else (entry - close) / entry
    return notional * pct


def hit_flags(side: str, candle: dict, stop: Decimal, tp: Decimal) -> tuple[bool, bool]:
    if side == "LONG":
        return candle["high"] >= tp, candle["low"] <= stop
    return candle["low"] <= tp, candle["high"] >= stop


def evaluate_against_candles(shadow: dict, candles: list[dict], assumptions: Assumptions | None = None, run_id: str | None = None) -> dict:
    assumptions = assumptions or Assumptions()
    run_id = run_id or utc_stamp()
    ahash = assumption_hash(assumptions)
    shadow_id = str(shadow.get("shadow_id") or "")
    signal = shadow.get("signal") if isinstance(shadow.get("signal"), dict) else {}
    order_plan = shadow.get("order_plan") if isinstance(shadow.get("order_plan"), dict) else {}
    symbol = str(signal.get("symbol") or shadow.get("symbol") or "").upper()
    side = str(signal.get("side") or shadow.get("side") or "").upper()
    entry_ts = shadow.get("ts")
    entry_ms = ts_ms(entry_ts)
    entry = safe_decimal(shadow.get("entry"))
    stop = safe_decimal(shadow.get("stop"))
    tp = safe_decimal(shadow.get("take_profit"))
    notional = safe_decimal(order_plan.get("notional"), "1")
    if not shadow_id or side not in {"LONG", "SHORT"} or not symbol or not entry_ms or entry <= 0 or stop <= 0 or tp <= 0 or notional <= 0:
        return malformed_close(shadow, run_id, assumptions, "malformed")

    now_ms = int(time.time() * 1000)
    parsed = parse_klines(candles)
    incomplete_last = False
    if parsed and parsed[-1]["close_time"] > now_ms and not assumptions.allow_incomplete_candle:
        incomplete_last = True
        parsed = parsed[:-1]
    eval_candles: list[dict] = []
    entry_partial_skipped = False
    for candle in parsed:
        if candle["close_time"] < entry_ms:
            continue
        if assumptions.skip_entry_partial and candle["open_time"] < entry_ms <= candle["close_time"]:
            entry_partial_skipped = True
            continue
        eval_candles.append(candle)

    reason = "unresolved"
    status = "open"
    close_price: Decimal | None = None
    close_ts: int | None = None
    ambiguous = False
    max_hold_ms = max(0, int(assumptions.max_hold_seconds)) * 1000
    timeout_at = entry_ms + max_hold_ms if max_hold_ms else None
    for candle in eval_candles:
        hit_tp, hit_sl = hit_flags(side, candle, stop, tp)
        if hit_tp and hit_sl:
            ambiguous = True
            if assumptions.ambiguity_policy == "tp_first":
                reason = "tp"
                close_price = tp
            else:
                reason = "ambiguous_sl_first"
                close_price = stop
            status = "closed"
            close_ts = candle["close_time"]
            break
        if hit_tp:
            reason = "tp"
            status = "closed"
            close_price = tp
            close_ts = candle["close_time"]
            break
        if hit_sl:
            reason = "sl"
            status = "closed"
            close_price = stop
            close_ts = candle["close_time"]
            break
        if timeout_at and candle["close_time"] >= timeout_at:
            reason = "timeout"
            status = "closed"
            close_price = candle["close"]
            close_ts = candle["close_time"]
            break

    fee_rate = safe_decimal(assumptions.fee_rate)
    slippage_bps = safe_decimal(assumptions.slippage_bps)
    gross = Decimal("0")
    fees = Decimal("0")
    slippage_cost = Decimal("0")
    adjusted_close: Decimal | None = None
    if close_price is not None:
        raw_gross = pnl_for(side, entry, close_price, notional)
        adjusted_close, slip_price = adverse_close_price(side, "tp" if reason == "tp" else "sl", close_price, slippage_bps)
        gross = pnl_for(side, entry, adjusted_close, notional)
        slippage_cost = abs(raw_gross - gross) if slip_price else Decimal("0")
        fees = notional * fee_rate * Decimal("2")
    net = gross - fees
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "shadow_close",
        "run_id": run_id,
        "assumption_hash": ahash,
        "close_id": close_id(shadow_id, ahash),
        "shadow_id": shadow_id,
        "symbol": symbol,
        "side": side,
        "entry_ts": entry_ts,
        "close_ts": ms_iso(close_ts),
        "entry": dec_str(entry),
        "close": dec_str(adjusted_close) if adjusted_close is not None else None,
        "stop": dec_str(stop),
        "take_profit": dec_str(tp),
        "reason": reason,
        "status": status,
        "gross": dec_str(gross),
        "fees": dec_str(fees),
        "slippage": dec_str(slippage_cost),
        "net": dec_str(net),
        "duration_seconds": int((close_ts - entry_ms) / 1000) if close_ts else None,
        "score": signal.get("score"),
        "block_reason": shadow.get("block_reason"),
        "order_plan": order_plan,
        "signal": signal,
        "data_quality": {
            "interval": assumptions.interval,
            "candle_count": len(eval_candles),
            "ambiguous": ambiguous,
            "entry_partial_skipped": entry_partial_skipped,
            "incomplete_last_candle": incomplete_last,
            "source": "binance_usdm_klines",
        },
        "assumptions": assumptions.payload(),
    }


def score_bucket(score: object) -> str:
    value = safe_float(score, -1)
    if value < 0:
        return "unknown"
    if value <= 5:
        return "0-5"
    if value < 8:
        return str(int(value))
    return "8+"


def empty_stats() -> dict:
    return {
        "trades": 0,
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "gross": 0.0,
        "fees": 0.0,
        "slippage": 0.0,
        "net": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "expectancy": 0.0,
        "profit_factor": 0.0,
        "max_drawdown": 0.0,
        "avg_time_to_exit_seconds": 0.0,
        "ambiguous_count": 0,
        "malformed_count": 0,
        "skipped_count": 0,
        "unresolved_count": 0,
        "timeout_count": 0,
        "api_error_count": 0,
        "confidence": "low",
    }


def finalize_stats(rows: list[dict]) -> dict:
    stats = empty_stats()
    wins: list[float] = []
    losses: list[float] = []
    durations: list[float] = []
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for row in rows:
        stats["trades"] += 1
        reason = str(row.get("reason") or "")
        status = str(row.get("status") or "")
        dq = row.get("data_quality") if isinstance(row.get("data_quality"), dict) else {}
        if dq.get("ambiguous") or reason.startswith("ambiguous"):
            stats["ambiguous_count"] += 1
        if reason == "malformed":
            stats["malformed_count"] += 1
        if reason == "api_error":
            stats["api_error_count"] += 1
        if status == "skipped":
            stats["skipped_count"] += 1
            continue
        if status == "open" or reason == "unresolved":
            stats["unresolved_count"] += 1
            continue
        if reason == "timeout":
            stats["timeout_count"] += 1
        stats["closed"] += 1
        net = safe_float(row.get("net"))
        gross = safe_float(row.get("gross"))
        fees = safe_float(row.get("fees"))
        slip = safe_float(row.get("slippage"))
        stats["net"] += net
        stats["gross"] += gross
        stats["fees"] += fees
        stats["slippage"] += slip
        if row.get("duration_seconds") is not None:
            durations.append(safe_float(row.get("duration_seconds")))
        equity += net
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        if net > 0:
            stats["wins"] += 1
            wins.append(net)
        elif net < 0:
            stats["losses"] += 1
            losses.append(net)
    closed = stats["closed"]
    stats["win_rate"] = round(stats["wins"] / closed, 4) if closed else 0.0
    stats["avg_win"] = round(sum(wins) / len(wins), 8) if wins else 0.0
    stats["avg_loss"] = round(sum(losses) / len(losses), 8) if losses else 0.0
    stats["expectancy"] = round(stats["net"] / closed, 8) if closed else 0.0
    stats["profit_factor"] = round(sum(wins) / abs(sum(losses)), 4) if losses else (999.0 if wins else 0.0)
    stats["max_drawdown"] = round(abs(max_dd), 8)
    stats["avg_time_to_exit_seconds"] = round(sum(durations) / len(durations), 2) if durations else 0.0
    for key in ("gross", "fees", "slippage", "net"):
        stats[key] = round(stats[key], 8)
    if closed >= 50 and stats["unresolved_count"] <= closed and (stats["ambiguous_count"] / max(1, closed)) <= 0.25:
        stats["confidence"] = "high"
    elif closed >= 20:
        stats["confidence"] = "medium"
    return stats


def segment_rows(rows: list[dict], key_func: Callable[[dict], str]) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[key_func(row)].append(row)
    result = []
    for key, items in buckets.items():
        stats = finalize_stats(items)
        stats["key"] = key
        result.append(stats)
    result.sort(key=lambda item: (item["closed"], item["expectancy"]), reverse=True)
    return result


def aggregate_window(rows: list[dict], run_id: str | None = None) -> dict:
    assumption_counts = Counter(str(row.get("assumption_hash") or "unknown") for row in rows)
    selected_hash = assumption_counts.most_common(1)[0][0] if assumption_counts else "none"
    selected = [row for row in rows if str(row.get("assumption_hash") or "unknown") == selected_hash]
    overall = finalize_stats(selected)
    segments = {
        "by_symbol": segment_rows(selected, lambda row: str(row.get("symbol") or "unknown")),
        "by_side": segment_rows(selected, lambda row: str(row.get("side") or "unknown")),
        "by_score_bucket": segment_rows(selected, lambda row: score_bucket(row.get("score") or (row.get("signal") or {}).get("score"))),
        "by_block_reason": segment_rows(selected, lambda row: str(row.get("block_reason") or "unknown")),
    }
    kill_candidates = []
    promotion_candidates = []
    for group_name, items in segments.items():
        for item in items:
            enriched = {"group": group_name, **item}
            if item["closed"] >= 20 and (item["expectancy"] < 0 or item["profit_factor"] < 1 or item["win_rate"] < 0.45):
                kill_candidates.append(enriched)
            if item["closed"] >= 50 and item["expectancy"] > 0 and item["profit_factor"] >= 1.5 and item["confidence"] in {"medium", "high"}:
                promotion_candidates.append(enriched)
    data_quality = {
        "assumption_hash_counts": dict(assumption_counts),
        "selected_assumption_hash": selected_hash,
        "mixed_assumptions": len(assumption_counts) > 1,
        "total_rows": len(rows),
        "selected_rows": len(selected),
        "confidence": overall["confidence"],
        "unresolved_count": overall["unresolved_count"],
        "ambiguous_count": overall["ambiguous_count"],
        "skipped_count": overall["skipped_count"],
        "api_error_count": overall["api_error_count"],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "run_id": run_id or (selected[-1].get("run_id") if selected else None),
        "assumption_hash": selected_hash,
        "metric_mode": "closed_only",
        "overall": overall,
        "segments": segments,
        "data_quality": data_quality,
        "kill_candidates": kill_candidates[:25],
        "promotion_candidates": promotion_candidates[:25],
    }


def aggregate_performance(rows: list[dict], run_id: str | None = None, fresh_start_ts: str | None = DEFAULT_FRESH_WINDOW_START) -> dict:
    performance = aggregate_window(rows, run_id)
    fresh_rows = rows_since(rows, fresh_start_ts)
    fresh = aggregate_window(fresh_rows, run_id)
    performance["fresh_window"] = {
        "start_ts": fresh_start_ts,
        "row_count": len(fresh_rows),
        "assumption_hash": fresh.get("assumption_hash"),
        "overall": fresh.get("overall") or {},
        "segments": fresh.get("segments") or {},
        "data_quality": fresh.get("data_quality") or {},
        "kill_candidates": fresh.get("kill_candidates") or [],
        "promotion_candidates": fresh.get("promotion_candidates") or [],
    }
    return performance


def render_markdown_report(performance: dict) -> str:
    overall = performance.get("overall") or {}
    dq = performance.get("data_quality") or {}
    fresh = performance.get("fresh_window") if isinstance(performance.get("fresh_window"), dict) else {}
    fresh_overall = fresh.get("overall") if isinstance(fresh.get("overall"), dict) else {}
    fresh_dq = fresh.get("data_quality") if isinstance(fresh.get("data_quality"), dict) else {}
    lines = [
        "# Shadow Performance Report",
        "",
        f"Generated: {performance.get('updated_at')}",
        f"Assumption hash: `{performance.get('assumption_hash')}`",
        f"Metric mode: `{performance.get('metric_mode')}`",
        "",
        "## Overall",
        "",
        f"- closed={overall.get('closed', 0)} wins={overall.get('wins', 0)} losses={overall.get('losses', 0)} win_rate={overall.get('win_rate', 0)}",
        f"- net={overall.get('net', 0):+.8f} expectancy={overall.get('expectancy', 0):+.8f} profit_factor={overall.get('profit_factor', 0)} max_drawdown={overall.get('max_drawdown', 0)}",
        f"- unresolved={dq.get('unresolved_count', 0)} ambiguous={dq.get('ambiguous_count', 0)} skipped={dq.get('skipped_count', 0)} api_errors={dq.get('api_error_count', 0)} confidence={dq.get('confidence')}",
        "",
        "## Fresh Window",
        "",
        f"- start={fresh.get('start_ts')} rows={fresh.get('row_count', 0)} closed={fresh_overall.get('closed', 0)} confidence={fresh_dq.get('confidence', 'low')}",
        f"- net={fresh_overall.get('net', 0):+.8f} expectancy={fresh_overall.get('expectancy', 0):+.8f} profit_factor={fresh_overall.get('profit_factor', 0)} api_errors={fresh_dq.get('api_error_count', 0)} unresolved={fresh_dq.get('unresolved_count', 0)}",
        "",
        "## Top Segments",
        "",
    ]
    for group, rows in (performance.get("segments") or {}).items():
        lines.append(f"### {group}")
        for row in rows[:8]:
            lines.append(
                f"- `{row.get('key')}` closed={row.get('closed')} wr={row.get('win_rate')} "
                f"net={row.get('net'):+.8f} exp={row.get('expectancy'):+.8f} pf={row.get('profit_factor')} confidence={row.get('confidence')}"
            )
        lines.append("")
    lines.append("## Kill Candidates")
    for row in (performance.get("kill_candidates") or [])[:12]:
        lines.append(f"- `{row.get('group')}:{row.get('key')}` closed={row.get('closed')} exp={row.get('expectancy'):+.8f} pf={row.get('profit_factor')} wr={row.get('win_rate')}")
    if not performance.get("kill_candidates"):
        lines.append("- none")
    lines.append("")
    lines.append("## Promotion Candidates")
    for row in (performance.get("promotion_candidates") or [])[:12]:
        lines.append(f"- `{row.get('group')}:{row.get('key')}` closed={row.get('closed')} exp={row.get('expectancy'):+.8f} pf={row.get('profit_factor')} wr={row.get('win_rate')}")
    if not performance.get("promotion_candidates"):
        lines.append("- none")
    return "\n".join(lines) + "\n"


def write_performance_outputs(rows: list[dict], run_id: str, json_path: Path = SHADOW_PERFORMANCE_JSON, reports_dir: Path = REPORTS_DIR) -> dict:
    performance = aggregate_performance(rows, run_id)
    write_json_atomic(json_path, performance)
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{utc_stamp()}-shadow-performance.md"
    report_path.write_text(render_markdown_report(performance), encoding="utf-8")
    performance["report_path"] = str(report_path)
    return performance


def evaluate_many(
    shadows: list[dict],
    assumptions: Assumptions,
    run_id: str,
    fetcher: Callable[[str, int, int, str], list[dict]],
    max_trades: int | None = None,
    backoff_path: Path | None = None,
    rate_limit_cooldown_seconds: int = 900,
) -> list[dict]:
    rows: list[dict] = []
    selected_shadows = shadows[: max_trades or len(shadows)]
    if backoff_path is not None:
        backoff = rate_limit_backoff(backoff_path)
        if backoff.get("active"):
            error = f"rate_limited_backoff_until {backoff.get('backoff_until')}"
            return [malformed_close(shadow, run_id, assumptions, "api_error", error) for shadow in selected_shadows]
    rate_limit_error: str | None = None
    for shadow in selected_shadows:
        if rate_limit_error:
            rows.append(malformed_close(shadow, run_id, assumptions, "api_error", rate_limit_error))
            continue
        entry_ms = ts_ms(shadow.get("ts"))
        signal = shadow.get("signal") if isinstance(shadow.get("signal"), dict) else {}
        symbol = str(signal.get("symbol") or shadow.get("symbol") or "").upper()
        if not entry_ms or not symbol:
            rows.append(malformed_close(shadow, run_id, assumptions, "malformed"))
            continue
        end_ms = entry_ms + max(1, assumptions.max_hold_seconds or 180) * 1000 + interval_ms(assumptions.interval)
        try:
            candles = fetcher(symbol, entry_ms, end_ms, assumptions.interval)
        except MarketDataError as exc:
            error = str(exc)[:240]
            rows.append(malformed_close(shadow, run_id, assumptions, "api_error", error))
            if exc.rate_limited:
                rate_limit_error = error
                if backoff_path is not None:
                    record_rate_limit_backoff(error, exc.status_code, rate_limit_cooldown_seconds, backoff_path)
            continue
        except Exception:
            rows.append(malformed_close(shadow, run_id, assumptions, "api_error", "unknown_fetch_error"))
            continue
        rows.append(evaluate_against_candles(shadow, candles, assumptions, run_id))
    return rows


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate shadow would-trades with public Binance USD-M futures candles")
    parser.add_argument("--shadow-path", default=str(SHADOW_JSONL))
    parser.add_argument("--scalp-path", default=str(SCALP_JSONL))
    parser.add_argument("--output", default=str(SHADOW_CLOSE_JSONL))
    parser.add_argument("--performance-json", default=str(SHADOW_PERFORMANCE_JSON))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-trades", type=int)
    parser.add_argument("--max-age-hours", type=float)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--fee-rate", default="0.0005")
    parser.add_argument("--slippage-bps", default="2")
    parser.add_argument("--max-hold-seconds", type=int, default=180)
    parser.add_argument("--ambiguity-policy", choices=["sl_first", "tp_first"], default="sl_first")
    parser.add_argument("--include-entry-partial", action="store_true")
    parser.add_argument("--allow-incomplete-candle", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--rate-limit-cooldown-seconds", type=int, default=900)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    assumptions = Assumptions(
        interval=args.interval,
        fee_rate=str(args.fee_rate),
        slippage_bps=str(args.slippage_bps),
        max_hold_seconds=int(args.max_hold_seconds),
        ambiguity_policy=args.ambiguity_policy,
        skip_entry_partial=not args.include_entry_partial,
        allow_incomplete_candle=bool(args.allow_incomplete_candle),
    )
    shadows, read_stats = read_shadow_opens([Path(args.shadow_path), Path(args.scalp_path)])
    if args.max_age_hours is not None:
        cutoff = int((time.time() - args.max_age_hours * 3600) * 1000)
        shadows = [row for row in shadows if (ts_ms(row.get("ts")) or 0) >= cutoff]
    run_id = "shadow_eval_" + utc_stamp()

    def live_fetcher(symbol: str, start_ms: int, end_ms: int, interval: str) -> list[dict]:
        return fetch_klines(symbol, start_ms, end_ms, interval, use_cache=not args.no_cache)

    rows = evaluate_many(
        shadows,
        assumptions,
        run_id,
        live_fetcher,
        args.max_trades,
        backoff_path=RATE_LIMIT_STATE_JSON,
        rate_limit_cooldown_seconds=args.rate_limit_cooldown_seconds,
    )
    output = Path(args.output)
    existing = existing_close_ids(output)
    unique_rows = [row for row in rows if row.get("close_id") not in existing]
    if not args.dry_run:
        append_jsonl(output, unique_rows)
        for row in unique_rows:
            safe_append_event("shadow_trade_evaluator", "shadow_close", row, ts=row.get("close_ts") or utc_now())
        all_rows = read_jsonl(output)
        performance = write_performance_outputs(all_rows, run_id, Path(args.performance_json), REPORTS_DIR)
    else:
        performance = aggregate_performance(rows, run_id)
    summary = {
        "dry_run": bool(args.dry_run),
        "run_id": run_id,
        "read_stats": read_stats,
        "evaluated": len(rows),
        "new_rows": len(unique_rows),
        "duplicate_rows": len(rows) - len(unique_rows),
        "performance": performance.get("overall"),
        "data_quality": performance.get("data_quality"),
        "assumption_hash": assumption_hash(assumptions),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
