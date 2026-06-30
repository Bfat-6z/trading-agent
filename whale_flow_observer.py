"""Public Telegram whale/liquidation flow observer.

This observer reads public t.me channel pages and turns whale/liquidation posts
into deterministic market-context signals. It is advisory only: no live orders,
no private Telegram API, no exchange execution.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, write_json_atomic
from data_trust import latency_fields, sanitize_external_text
from source_provenance import build_provenance
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PID_FILE = STATE_DIR / "whale_flow_observer.pid"
HEARTBEAT_PATH = STATE_DIR / "whale_flow_observer_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_WHALE_FLOW_OBSERVER"
LATEST_PATH = MEMORY_DIR / "whale_flow_latest.json"
HISTORY_PATH = MEMORY_DIR / "whale_flow_history.jsonl"
EVENTS_PATH = MEMORY_DIR / "whale_flow_events.jsonl"

DEFAULT_CHANNELS = (
    "BinanceLiquidations",
    "WhaleBotAlerts",
    "kpbtcsignal",
    "WhaleSniper",
    "whale_alert_io",
    "cointrendz_whalehunter",
    "cointrendz_pumpdetector",
    "BinanceWhalesTracker",
)
BASE_URL = "https://t.me/s/{channel}"
MESSAGE_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
AMOUNT_PATTERNS = (
    re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KMB])?", re.I),
    re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KMB])?\s*(?:USDT|USD)", re.I),
)
SYMBOL_RE = re.compile(r"(?:[$#]\s*)?\b([A-Z0-9]{2,16})(?:/)?(USDT|USD|PERP)?\b")
PAIR_RE = re.compile(r"\b([A-Z0-9]{2,12})(USDT|USD|PERP)\b")
EXCLUDED_TOKENS = {
    "ALL",
    "ALERT",
    "ALERTS",
    "BINANCE",
    "BOT",
    "BUY",
    "CHANNEL",
    "CRYPTO",
    "FUTURE",
    "FUTURES",
    "LONG",
    "MAINTAINED",
    "OFFICIAL",
    "SELL",
    "SHORT",
    "SIGNAL",
    "TELEGRAM",
    "THE",
    "TRACKER",
    "USDT",
    "USD",
    "VIEW",
    "WHALE",
    "WHALES",
}

def normalize_channel(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = raw.rstrip("/")
    if "t.me/" in raw:
        raw = raw.split("t.me/", 1)[1]
    if raw.startswith("s/"):
        raw = raw[2:]
    return raw.strip("/")

def configured_channels(env: dict[str, str] | None = None) -> tuple[str, ...]:
    env = env or os.environ
    raw = env.get("WHALE_FLOW_CHANNELS") or env.get("TELEGRAM_WHALE_CHANNELS")
    if not raw:
        return DEFAULT_CHANNELS
    channels = tuple(channel for item in raw.split(",") if (channel := normalize_channel(item)))
    return channels or DEFAULT_CHANNELS

def strip_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()

def parse_telegram_messages(channel: str, html_text: str, limit: int = 30) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in MESSAGE_RE.finditer(html_text or ""):
        prefix = (html_text or "")[max(0, match.start() - 1200) : match.start()]
        post_match = re.search(r'data-post="([^"]+)"', prefix)
        time_match = re.search(r'<time[^>]+datetime="([^"]+)"', prefix, re.I)
        text = strip_html(match.group(1))
        if not text:
            continue
        post_ref = post_match.group(1) if post_match else ""
        rows.append(
            {
                "channel": channel,
                "text": text,
                "source_posted_at": time_match.group(1) if time_match else None,
                "message_ref": post_ref,
                "permalink": f"https://t.me/{post_ref}" if post_ref else None,
            }
        )
    return rows[-limit:]

def amount_to_float(value: str, suffix: str | None = None) -> float:
    try:
        amount = float(str(value).replace(",", ""))
    except Exception:
        return 0.0
    mult = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}.get(str(suffix or "").upper(), 1.0)
    return amount * mult

def extract_notional(text: str) -> float:
    best = 0.0
    for pattern in AMOUNT_PATTERNS:
        for match in pattern.finditer(text or ""):
            best = max(best, amount_to_float(match.group(1), match.group(2)))
    return best

def extract_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    for match in PAIR_RE.finditer(text or ""):
        base = match.group(1).upper()
        if base not in EXCLUDED_TOKENS:
            symbol = f"{base}USDT"
            if symbol not in symbols:
                symbols.append(symbol)
    for match in SYMBOL_RE.finditer(text or ""):
        base = match.group(1).upper()
        suffix = (match.group(2) or "").upper()
        if base in EXCLUDED_TOKENS or len(base) < 2:
            continue
        if base.endswith(("USDT", "USD", "PERP")) and not suffix:
            continue
        if suffix in {"USDT", "USD", "PERP"}:
            symbol = f"{base}USDT"
        elif match.group(0).strip().startswith(("$", "#")) or base in {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"}:
            symbol = f"{base}USDT"
        else:
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols or ["MARKET"]

def side_from_text(text: str) -> tuple[str, str]:
    lower = (text or "").lower()
    liquidation = "liquidat" in lower or "rekt" in lower
    long_hits = len(re.findall(r"\blongs?\b|\bbuy\b|\bbought\b|\bbullish\b", lower))
    short_hits = len(re.findall(r"\bshorts?\b|\bsell\b|\bsold\b|\bbearish\b", lower))
    if long_hits and short_hits:
        side = "MIXED"
    elif long_hits:
        side = "LONG"
    elif short_hits:
        side = "SHORT"
    else:
        side = "UNKNOWN"
    kind = "liquidation" if liquidation else "whale_order" if side != "UNKNOWN" else "volume_alert"
    return side, kind

def event_id(channel: str, text: str, symbol: str) -> str:
    raw = f"{channel}:{symbol}:{text}"
    return "whale_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

def classify_message(message: dict[str, Any], observed_at: str | None = None) -> list[dict[str, Any]]:
    sanitized = sanitize_external_text(message.get("text") or "")
    text = sanitized["text"]
    channel = str(message.get("channel") or "unknown")
    side, kind = side_from_text(text)
    notional = extract_notional(text)
    symbols = extract_symbols(text)
    confidence = 0.35 + (0.25 if notional > 0 else 0.0) + (0.2 if side != "UNKNOWN" else 0.0) + (0.1 if symbols != ["MARKET"] else 0.0)
    rows = []
    for symbol in symbols:
        source_posted_at = message.get("source_posted_at") or observed_at or utc_now()
        latency = latency_fields(source_posted_at, observed_at or utc_now(), ttl_seconds=900)
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": event_id(channel, text, symbol),
                "observed_at": observed_at or utc_now(),
                "channel": channel,
                "source": f"telegram:{channel}",
                "source_id": "telegram_public_whale_flow",
                "source_identity": {"provider": "telegram_public", "channel": channel, "message_ref": message.get("message_ref"), "permalink": message.get("permalink")},
                "permalink": message.get("permalink"),
                "text_hash": sanitized["content_hash"],
                "sanitize_flags": sanitized["flags"],
                "taint_class": "external_social",
                "allowed_effect": "shadow_only",
                "source_quorum_passed": False,
                "market_confirmed": False,
                "parse_confidence": round(min(1.0, confidence), 4),
                **latency,
                "symbol": symbol,
                "kind": kind,
                "reported_side": side,
                "notional": round(notional, 4),
                "confidence": round(min(1.0, confidence), 4),
                "text": text[:500],
                "can_place_live_orders": False,
            }
        )
    return rows

def aggregate_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_symbol: dict[str, dict[str, Any]] = {}
    for event in events:
        symbol = str(event.get("symbol") or "MARKET").upper()
        row = by_symbol.setdefault(
            symbol,
            {
                "symbol": symbol,
                "event_count": 0,
                "long_flow_notional": 0.0,
                "short_flow_notional": 0.0,
                "long_liquidation_notional": 0.0,
                "short_liquidation_notional": 0.0,
                "unknown_notional": 0.0,
                "sources": [],
                "claim_hashes": [],
                "source_quorum_passed": False,
                "market_confirmed": False,
                "allowed_effect": "shadow_only",
            },
        )
        row["event_count"] += 1
        notional = float(event.get("notional") or 0.0)
        side = str(event.get("reported_side") or "UNKNOWN").upper()
        kind = str(event.get("kind") or "")
        if event.get("source") not in row["sources"]:
            row["sources"].append(event.get("source"))
        if event.get("text_hash") and event.get("text_hash") not in row["claim_hashes"]:
            row["claim_hashes"].append(event.get("text_hash"))
        if event.get("too_late_to_copy"):
            row["too_late_to_copy"] = True
        row["source_quorum_passed"] = len(row["sources"]) >= 2 and len(row["claim_hashes"]) >= 2
        row["market_confirmed"] = bool(row.get("market_confirmed") or event.get("market_confirmed"))
        if kind == "liquidation" and side == "LONG":
            row["long_liquidation_notional"] += notional
        elif kind == "liquidation" and side == "SHORT":
            row["short_liquidation_notional"] += notional
        elif side == "LONG":
            row["long_flow_notional"] += notional
        elif side == "SHORT":
            row["short_flow_notional"] += notional
        else:
            row["unknown_notional"] += notional
    for row in by_symbol.values():
        long_flow = row["long_flow_notional"]
        short_flow = row["short_flow_notional"]
        long_liq = row["long_liquidation_notional"]
        short_liq = row["short_liquidation_notional"]
        total = max(1.0, long_flow + short_flow + long_liq + short_liq + row["unknown_notional"])
        pressure_score = ((long_flow - short_flow) + (short_liq - long_liq)) / total
        row["pressure_score"] = round(pressure_score, 6)
        row["pressure_side"] = "LONG" if pressure_score > 0.15 else "SHORT" if pressure_score < -0.15 else "NEUTRAL"
        row["crowd_bias"] = "LONG" if long_flow > short_flow * 1.25 else "SHORT" if short_flow > long_flow * 1.25 else "MIXED"
        row["squeeze_risk"] = "kill_longs" if long_liq > short_liq * 1.25 and long_liq > 0 else "squeeze_shorts" if short_liq > long_liq * 1.25 and short_liq > 0 else "none"
        for key in ("long_flow_notional", "short_flow_notional", "long_liquidation_notional", "short_liquidation_notional", "unknown_notional"):
            row[key] = round(float(row[key]), 4)
    sorted_rows = sorted(by_symbol.values(), key=lambda row: (abs(float(row.get("pressure_score") or 0)), row.get("event_count", 0)), reverse=True)
    return {"by_symbol": {row["symbol"]: row for row in sorted_rows}, "top_symbols": sorted_rows[:12]}

def fetch_channel(channel: str, timeout: float = 12.0) -> str:
    response = requests.get(BASE_URL.format(channel=channel), timeout=timeout, headers={"User-Agent": "Mozilla/5.0 trading-agent whale-flow"})
    response.raise_for_status()
    return response.text

def run_once(fetcher: Callable[[str], str] | None = None, channels: Iterable[str] | None = None, limit_per_channel: int = 30) -> dict[str, Any]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    fetcher = fetcher or fetch_channel
    channel_rows = []
    events: list[dict[str, Any]] = []
    observed_at = utc_now()
    for channel in channels or configured_channels():
        channel = normalize_channel(channel)
        if not channel:
            continue
        try:
            messages = parse_telegram_messages(channel, fetcher(channel), limit=limit_per_channel)
            channel_rows.append({"channel": channel, "status": "ok", "message_count": len(messages), "error": None})
            for message in messages:
                events.extend(classify_message(message, observed_at=observed_at))
        except Exception as exc:
            channel_rows.append({"channel": channel, "status": "error", "message_count": 0, "error": str(exc)[:180]})
    deduped = {str(event.get("event_id")): event for event in events}
    event_rows = list(deduped.values())[-200:]
    aggregate = aggregate_events(event_rows)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "status": "ok" if event_rows else "waiting_for_flow",
        "event_count": len(event_rows),
        "source_health": channel_rows,
        "channels": [row["channel"] for row in channel_rows],
        "top_events": event_rows[-25:],
        **aggregate,
        "contract": {"paper_shadow_only": True, "read_only": True},
        "can_place_live_orders": False,
    }
    write_json_atomic(LATEST_PATH, payload)
    append_jsonl(HISTORY_PATH, payload)
    for event in event_rows[-50:]:
        append_jsonl(EVENTS_PATH, event)
        try:
            from event_store import append_event_envelope

            provenance = build_provenance("social_post", [event.get("source_id") or "telegram_public_whale_flow"], input_ids=[event["event_id"]], metadata={"channel": event.get("channel")})
            append_event_envelope(
                "social.post.created",
                {"post_id": event["event_id"], "source": event.get("source"), "text_hash": event.get("text_hash"), "channel": event.get("channel"), "permalink": event.get("permalink")},
                "whale_flow_observer",
                event.get("source_id") or "telegram_public_whale_flow",
                event["event_id"],
                provenance_id=provenance["provenance_id"],
            )
        except Exception:
            pass
    write_heartbeat(payload["status"], {"event_count": len(event_rows), "channels": len(channel_rows)})
    return payload

def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> None:
    write_json_atomic(HEARTBEAT_PATH, {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})})

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe public Telegram whale/liquidation flow")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=180.0)
    parser.add_argument("--limit-per-channel", type=int, default=30)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.limit_per_channel <= 0:
        parser.error("--limit-per-channel must be positive")
    return args

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        row = run_once(limit_per_channel=args.limit_per_channel)
        print(f"whale_flow_observer status={row.get('status')} events={row.get('event_count')}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
