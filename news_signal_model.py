"""Deterministic news/macro risk scoring for the trading agent.

The model is intentionally simple and auditable. It turns normalized headline
events into context that other agents can use to tighten or block risk, never
to loosen execution controls.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

from data_trust import sanitize_external_text, source_policy

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
LATEST_JSON = MEMORY_DIR / "news_latest.json"
LATEST_MD = MEMORY_DIR / "news_latest.md"

MAJOR_SYMBOLS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON",
    "SUI", "HYPE", "ENA", "PEPE", "WIF", "NEAR", "ARB", "OP", "TRX", "LTC",
}

STABLE_SYMBOLS = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "BUSD"}

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "macro_rates": ("fed", "fomc", "rate cut", "rate hike", "interest rate", "yields", "treasury", "cpi", "inflation", "jobs report", "nonfarm", "unemployment"),
    "liquidity": ("liquidity", "qt", "qe", "dollar index", "dxy", "risk assets", "etf inflow", "etf outflow"),
    "regulation": ("sec", "cftc", "doj", "lawsuit", "sues", "charged", "settlement", "regulation", "regulatory", "ban", "crackdown", "mifid", "mica"),
    "exchange_risk": ("binance", "coinbase", "okx", "bybit", "kraken", "exchange", "withdrawals halted", "outage", "maintenance"),
    "security_risk": ("hack", "exploit", "bridge exploit", "stolen", "drained", "phishing", "vulnerability", "attack"),
    "stablecoin_risk": ("depeg", "de-pegged", "reserve", "attestation", "stablecoin", "tether", "circle"),
    "token_supply": ("unlock", "vesting", "airdrop", "emission", "supply", "burn"),
    "listing_catalyst": ("listing", "listed", "launchpool", "perpetual", "futures listing", "spot listing", "delisting"),
    "chain_health": ("network outage", "halted", "validator", "reorg", "downtime", "congestion", "finality"),
    "geopolitics": ("war", "missile", "sanction", "tariff", "election", "capital controls", "ceasefire"),
    "social_pump": ("whale", "kol", "rumor", "viral", "meme", "pump", "short squeeze"),
}

BEARISH_WORDS = (
    "hack", "exploit", "sues", "lawsuit", "ban", "crackdown", "outflow", "outage",
    "delisting", "depeg", "halted", "stolen", "liquidation", "selloff", "dump",
)
BULLISH_WORDS = (
    "approval", "approved", "inflow", "listing", "launch", "partnership", "upgrade",
    "accumulation", "buy", "breakout", "record inflow", "rate cut",
)
HIGH_RISK_TOPICS = {"regulation", "security_risk", "stablecoin_risk", "chain_health", "geopolitics"}

SOURCE_QUALITY_HINTS = (
    (("sec.gov", "cftc.gov", "federalreserve.gov", "treasury.gov", "whitehouse.gov"), 0.96),
    (("reuters", "bloomberg", "wsj", "ft.com", "apnews"), 0.9),
    (("coindesk", "theblock", "cointelegraph", "decrypt", "cryptoslate", "bitcoinmagazine"), 0.78),
    (("cryptopanic", "yfinance", "finance.yahoo"), 0.68),
    (("reddit", "x.com", "twitter", "stocktwits"), 0.38),
)

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        pass
    try:
        parsed = parsedate_to_datetime(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None

def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))

def stable_event_id(title: str, source: str = "", url: str = "", published_at: str = "") -> str:
    raw = json.dumps(
        {
            "title": normalize_text(title),
            "source": str(source or "").lower().strip(),
            "url": str(url or "").lower().strip(),
            "published_at": str(published_at or "")[:19],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())

def source_quality(source: object, source_type: object = "") -> float:
    haystack = normalize_text(f"{source} {source_type}")
    for hints, score in SOURCE_QUALITY_HINTS:
        if any(hint in haystack for hint in hints):
            return score
    if "rss" in haystack or "news" in haystack:
        return 0.62
    if "social" in haystack:
        return 0.35
    return 0.55

def freshness_score(published_at: object, seen_at: object | None = None, now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    ts = parse_ts(published_at) or parse_ts(seen_at)
    if not ts:
        return 0.25
    age_hours = max(0.0, (current - ts).total_seconds() / 3600.0)
    if age_hours <= 1:
        return 1.0
    if age_hours <= 6:
        return 0.85
    if age_hours <= 24:
        return 0.6
    if age_hours <= 72:
        return 0.32
    return 0.12

def classify_topics(text: str) -> list[str]:
    haystack = normalize_text(text)
    topics = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            topics.append(topic)
    return topics

def extract_symbols(text: str, explicit: Iterable[object] | None = None) -> list[str]:
    symbols = {str(item).upper().replace("USDT", "") for item in (explicit or []) if item}
    haystack = f" {str(text or '').upper()} "
    for symbol in MAJOR_SYMBOLS | STABLE_SYMBOLS:
        if re.search(rf"(?<![A-Z0-9])\$?{re.escape(symbol)}(?![A-Z0-9])", haystack):
            symbols.add(symbol)
    return sorted(symbols)

def event_topic_score(topics: list[str]) -> float:
    score = 0.0
    for topic in topics:
        if topic in HIGH_RISK_TOPICS:
            score += 0.28
        elif topic in {"macro_rates", "liquidity", "exchange_risk"}:
            score += 0.18
        else:
            score += 0.12
    return clamp(score)

def event_direction(text: str) -> tuple[float, float, list[str]]:
    haystack = normalize_text(text)
    bearish_hits = [word for word in BEARISH_WORDS if word in haystack]
    bullish_hits = [word for word in BULLISH_WORDS if word in haystack]
    bearish = clamp(0.18 * len(bearish_hits))
    bullish = clamp(0.16 * len(bullish_hits))
    reasons = [f"bearish:{word}" for word in bearish_hits[:4]] + [f"bullish:{word}" for word in bullish_hits[:4]]
    return bullish, bearish, reasons

def normalize_event(raw: dict, now: str | None = None) -> dict:
    raw_title = str(raw.get("title") or raw.get("headline") or "").strip()
    raw_summary = str(raw.get("summary") or "")
    title_clean = sanitize_external_text(raw_title, max_chars=240)
    summary_clean = sanitize_external_text(raw_summary, max_chars=800)
    title = title_clean["text"]
    source = str(raw.get("source") or raw.get("publisher") or raw.get("domain") or "unknown").strip()
    published_at = str(raw.get("published_at") or raw.get("pub_date") or raw.get("created_at") or raw.get("ts") or "")
    url = str(raw.get("url") or raw.get("link") or "").strip()
    text = f"{title} {summary_clean['text']}"
    topics = sorted(set(raw.get("topics") or classify_topics(text)))
    symbols = extract_symbols(text, raw.get("symbols") or raw.get("currencies") or [])
    event_id = str(raw.get("event_id") or stable_event_id(title, source, url, published_at))
    policy = source_policy(str(raw.get("source_type") or "news"))
    return {
        "event_id": event_id,
        "ts_seen": str(raw.get("ts_seen") or now or utc_now()),
        "available_at": str(raw.get("available_at") or raw.get("ts_seen") or now or utc_now()),
        "known_at": str(raw.get("known_at") or raw.get("available_at") or raw.get("ts_seen") or now or utc_now()),
        "ingested_at": str(raw.get("ingested_at") or raw.get("known_at") or raw.get("available_at") or raw.get("ts_seen") or now or utc_now()),
        "published_at": published_at,
        "source": source,
        "source_type": str(raw.get("source_type") or "news"),
        "title": title,
        "summary": summary_clean["text"],
        "url": url,
        "symbols": symbols,
        "topics": topics,
        "raw_sentiment": str(raw.get("raw_sentiment") or raw.get("sentiment") or ""),
        "raw_importance": raw.get("raw_importance") if raw.get("raw_importance") is not None else raw.get("importance"),
        "fetch_status": str(raw.get("fetch_status") or "ok"),
        "text_hash": title_clean["content_hash"],
        "summary_hash": summary_clean["content_hash"],
        "sanitize_flags": sorted(set(title_clean["flags"] + summary_clean["flags"])),
        "taint_class": policy["taint_class"],
        "allowed_effect": policy["allowed_effect"],
        "source_identity": {"provider": source, "url": url, "published_at": published_at},
        "parse_confidence": 0.8 if not title_clean["flags"] else 0.55,
    }

def event_before_cutoff(event: dict, decision_cutoff: datetime, latency_buffer_seconds: int = 0) -> bool:
    deadline = decision_cutoff - timedelta(seconds=max(0, int(latency_buffer_seconds)))
    times = [parse_ts(event.get(field)) for field in ("available_at", "known_at", "ingested_at", "ts_seen")]
    clean = [ts for ts in times if ts is not None]
    return bool(clean) and max(clean) <= deadline


def score_events(events: Iterable[dict], now: datetime | None = None, source_health: list[dict] | None = None, decision_cutoff: object | None = None, latency_buffer_seconds: int = 0) -> dict:
    current = now or datetime.now(timezone.utc)
    normalized = [normalize_event(event, now=current.isoformat(timespec="seconds")) for event in events if isinstance(event, dict)]
    cutoff_dt = parse_ts(decision_cutoff) if decision_cutoff else None
    cutoff_filtered_count = 0
    seen: set[str] = set()
    unique = []
    for event in normalized:
        if not event["title"] or event["event_id"] in seen:
            continue
        if cutoff_dt and not event_before_cutoff(event, cutoff_dt, latency_buffer_seconds):
            cutoff_filtered_count += 1
            continue
        seen.add(event["event_id"])
        unique.append(event)

    top_events = []
    macro = regulatory = catalyst = chaos = quality_total = fresh_total = 0.0
    symbol_impacts: dict[str, dict] = {}
    for event in unique:
        text = f"{event['title']} {event.get('summary', '')}"
        topics = event.get("topics") or classify_topics(text)
        quality = source_quality(event.get("source"), event.get("source_type"))
        fresh = freshness_score(event.get("published_at"), event.get("ts_seen"), current)
        bullish, bearish, direction_reasons = event_direction(text)
        topic_score = event_topic_score(topics)
        weight = quality * fresh
        risk = clamp(topic_score * weight + bearish * 0.45 * fresh)
        event_catalyst = clamp((bullish + (0.08 if "listing_catalyst" in topics else 0.0)) * weight)
        event_chaos = clamp((0.16 * len(topics) + bearish * 0.2) * (1.1 - quality) * fresh)
        if {"macro_rates", "liquidity", "geopolitics"} & set(topics):
            macro = max(macro, risk)
        if "regulation" in topics:
            regulatory = max(regulatory, risk)
        catalyst = max(catalyst, event_catalyst)
        chaos = max(chaos, event_chaos)
        quality_total += quality
        fresh_total += fresh
        reasons = topics[:5] + direction_reasons[:5]
        top_events.append(
            {
                "event_id": event["event_id"],
                "title": event["title"],
                "source": event["source"],
                "published_at": event.get("published_at"),
                "symbols": event.get("symbols", []),
                "topics": topics,
                "risk": round(risk, 4),
                "catalyst": round(event_catalyst, 4),
                "freshness": round(fresh, 4),
                "source_quality": round(quality, 4),
                "reasons": reasons[:8],
                "url": event.get("url", ""),
                "text_hash": event.get("text_hash"),
                "summary_hash": event.get("summary_hash"),
                "taint_class": event.get("taint_class"),
                "allowed_effect": event.get("allowed_effect"),
                "sanitize_flags": event.get("sanitize_flags", []),
                "source_identity": event.get("source_identity", {}),
            }
        )
        for symbol in event.get("symbols", []):
            row = symbol_impacts.setdefault(symbol, {"bullish": 0.0, "bearish": 0.0, "risk": 0.0, "confidence": 0.0, "reasons": [], "event_ids": []})
            row["bullish"] = max(row["bullish"], event_catalyst)
            row["bearish"] = max(row["bearish"], bearish * weight)
            row["risk"] = max(row["risk"], risk)
            row["confidence"] = max(row["confidence"], clamp(weight))
            row["reasons"].extend(reason for reason in reasons if reason not in row["reasons"])
            row["event_ids"].append(event["event_id"])

    top_events.sort(key=lambda item: (item["risk"], item["catalyst"], item["freshness"]), reverse=True)
    for row in symbol_impacts.values():
        row["bullish"] = round(row["bullish"], 4)
        row["bearish"] = round(row["bearish"], 4)
        row["risk"] = round(row["risk"], 4)
        row["confidence"] = round(row["confidence"], 4)
        row["reasons"] = row["reasons"][:10]
        row["event_ids"] = row["event_ids"][:10]
    count = len(unique)
    health = source_health or []
    failed_sources = [row for row in health if row.get("status") not in {"ok", "skipped"}]
    return {
        "ts": current.isoformat(timespec="seconds"),
        "event_count": count,
        "cutoff_filtered_event_count": cutoff_filtered_count,
        "decision_cutoff": cutoff_dt.isoformat(timespec="seconds") if cutoff_dt else None,
        "latency_buffer_seconds": latency_buffer_seconds if cutoff_dt else 0,
        "macro_risk_score": round(clamp(macro), 4),
        "crypto_regulatory_risk": round(clamp(regulatory), 4),
        "catalyst_score": round(clamp(catalyst), 4),
        "headline_chaos": round(clamp(max(chaos, min(0.35, 0.06 * len(failed_sources))))),
        "source_quality_score": round(quality_total / count, 4) if count else 0.0,
        "freshness_score": round(fresh_total / count, 4) if count else 0.0,
        "symbol_impacts": dict(sorted(symbol_impacts.items())),
        "top_events": top_events[:20],
        "source_health": health,
        "risk_contract": "tighten_only",
        "can_place_orders": False,
        "can_loosen_risk": False,
    }

def render_markdown(result: dict) -> str:
    lines = [
        "# News Macro State",
        f"Generated: {result.get('ts')}",
        "",
        "## Scores",
        f"- Macro risk: {result.get('macro_risk_score', 0)}",
        f"- Crypto regulatory risk: {result.get('crypto_regulatory_risk', 0)}",
        f"- Catalyst score: {result.get('catalyst_score', 0)}",
        f"- Headline chaos: {result.get('headline_chaos', 0)}",
        f"- Source quality: {result.get('source_quality_score', 0)}",
        f"- Freshness: {result.get('freshness_score', 0)}",
        "",
        "## Top Events",
        "| Source | Risk | Catalyst | Symbols | Title |",
        "|---|---:|---:|---|---|",
    ]
    for event in result.get("top_events", [])[:12]:
        title = str(event.get("title") or "").replace("|", " ")[:140]
        lines.append(f"| {event.get('source')} | {event.get('risk')} | {event.get('catalyst')} | {', '.join(event.get('symbols') or [])} | {title} |")
    lines += ["", "## Source Health", "| Source | Status | Count | Error |", "|---|---|---:|---|"]
    for source in result.get("source_health", []):
        err = str(source.get("error") or "").replace("|", " ")[:120]
        lines.append(f"| {source.get('source')} | {source.get('status')} | {source.get('count', 0)} | {err} |")
    lines += ["", "Risk contract: tighten-only. This module cannot place orders or loosen risk."]
    return "\n".join(lines) + "\n"

def save_latest(result: dict, latest_json: Path = LATEST_JSON, latest_md: Path = LATEST_MD) -> None:
    latest_json.parent.mkdir(parents=True, exist_ok=True)
    latest_json.write_text(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    latest_md.write_text(render_markdown(result), encoding="utf-8")
