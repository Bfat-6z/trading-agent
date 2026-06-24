"""24/7 news and macro observer for the trading agent.

This process is read-only. It fetches public/API news, normalizes headlines,
writes auditable state, and emits a heartbeat. It cannot place trades.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from event_store import safe_append_event, safe_append_snapshot, safe_upsert_heartbeat
from news_signal_model import normalize_event, save_latest, score_events, stable_event_id

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
STOP_FILE = STATE_DIR / "STOP_NEWS_OBSERVER"
PID_FILE = STATE_DIR / "news_observer.pid"
HEARTBEAT_PATH = STATE_DIR / "news_observer_heartbeat.json"
EVENTS_JSONL = MEMORY_DIR / "news_events.jsonl"
LATEST_JSON = MEMORY_DIR / "news_latest.json"
LATEST_MD = MEMORY_DIR / "news_latest.md"
TRADINGAGENTS_SRC = ROOT / "tradingagents_crypto_src"

DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "SUI", "ENA", "HYPE")
RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
)

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        return

def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")

def read_existing_ids(path: Path, tail_lines: int = 2000) -> set[str]:
    if not path.exists():
        return set()
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-tail_lines:]
    except Exception:
        return set()
    ids = set()
    for line in lines:
        try:
            row = json.loads(line)
            if row.get("event_id"):
                ids.add(str(row["event_id"]))
        except Exception:
            continue
    return ids

def parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_SYMBOLS)
    symbols = []
    for part in re.split(r"[,\s]+", raw):
        symbol = part.strip().upper().replace("USDT", "")
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols or list(DEFAULT_SYMBOLS)

def source_status(source: str, status: str, count: int = 0, error: str = "") -> dict:
    payload = {"source": source, "status": status, "count": int(count)}
    if error:
        payload["error"] = error[:220]
    return payload

def ensure_tradingagents_path() -> None:
    if TRADINGAGENTS_SRC.exists():
        raw = str(TRADINGAGENTS_SRC)
        if raw not in sys.path:
            sys.path.insert(0, raw)

def fetch_cryptopanic(symbols: list[str], limit: int) -> tuple[list[dict], dict]:
    if not os.environ.get("CRYPTOPANIC_API_KEY"):
        return [], source_status("cryptopanic", "skipped", 0, "missing_key")
    ensure_tradingagents_path()
    try:
        from tradingagents.dataflows.cryptopanic import get_macro_news, get_news_for_symbol
    except Exception as exc:
        return [], source_status("cryptopanic", "error", 0, f"import_failed:{exc}")
    events: list[dict] = []
    try:
        items = list(get_macro_news(limit=min(limit, 10)))
        per_symbol = max(1, min(4, limit // max(1, len(symbols))))
        for symbol in symbols[:8]:
            items.extend(get_news_for_symbol(symbol, limit=per_symbol))
        for item in items[: max(1, limit)]:
            events.append(
                {
                    "title": item.title,
                    "url": item.url,
                    "source": item.source or "cryptopanic",
                    "source_type": "news_api",
                    "published_at": item.published_at,
                    "symbols": [],
                    "raw_sentiment": item.sentiment,
                    "raw_importance": item.votes_important,
                    "summary": "",
                }
            )
        return events, source_status("cryptopanic", "ok", len(events))
    except Exception as exc:
        return events, source_status("cryptopanic", "error", len(events), str(exc))

def fetch_alpha_vantage(symbols: list[str], limit: int) -> tuple[list[dict], dict]:
    if not os.environ.get("ALPHA_VANTAGE_API_KEY"):
        return [], source_status("alpha_vantage", "skipped", 0, "missing_key")
    ensure_tradingagents_path()
    try:
        from tradingagents.dataflows.alpha_vantage_news import get_global_news
    except Exception as exc:
        return [], source_status("alpha_vantage", "error", 0, f"import_failed:{exc}")
    try:
        payload = get_global_news(datetime.now(timezone.utc).strftime("%Y-%m-%d"), look_back_days=2, limit=min(limit, 50))
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return [], source_status("alpha_vantage", "error", 0, "non_json_response")
        feed = payload.get("feed", []) if isinstance(payload, dict) else []
        events = []
        for item in feed[:limit]:
            events.append(
                {
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "url": item.get("url", ""),
                    "source": item.get("source", "alpha_vantage"),
                    "source_type": "news_api",
                    "published_at": item.get("time_published", ""),
                    "raw_sentiment": item.get("overall_sentiment_label", ""),
                    "raw_importance": item.get("relevance_score"),
                }
            )
        return events, source_status("alpha_vantage", "ok", len(events))
    except Exception as exc:
        return [], source_status("alpha_vantage", "error", 0, str(exc))

def parse_markdown_news(text: str, source_name: str, limit: int) -> list[dict]:
    events = []
    current: dict | None = None
    for line in text.splitlines():
        if line.startswith("### "):
            if current:
                events.append(current)
            raw = line[4:].strip()
            source = source_name
            match = re.search(r"\(source:\s*([^)]*)\)", raw, flags=re.I)
            if match:
                source = match.group(1).strip() or source_name
                raw = re.sub(r"\s*\(source:\s*[^)]*\)", "", raw, flags=re.I).strip()
            current = {"title": raw, "source": source, "source_type": source_name, "published_at": utc_now(), "summary": "", "url": ""}
        elif current and line.startswith("Link:"):
            current["url"] = line.split(":", 1)[1].strip()
        elif current and line.strip() and not line.startswith("##"):
            current["summary"] = (current.get("summary", "") + " " + line.strip()).strip()[:800]
        if len(events) >= limit:
            break
    if current and len(events) < limit:
        events.append(current)
    return events[:limit]

def fetch_yfinance_global(symbols: list[str], limit: int) -> tuple[list[dict], dict]:
    ensure_tradingagents_path()
    try:
        from tradingagents.dataflows.yfinance_news import get_global_news_yfinance
    except Exception as exc:
        return [], source_status("yfinance", "error", 0, f"import_failed:{exc}")
    try:
        text = get_global_news_yfinance(datetime.now(timezone.utc).strftime("%Y-%m-%d"), look_back_days=2, limit=min(limit, 20))
        events = parse_markdown_news(text, "yfinance", limit)
        status = "ok" if events else "empty"
        return events, source_status("yfinance", status, len(events))
    except Exception as exc:
        return [], source_status("yfinance", "error", 0, str(exc))

def fetch_reddit(symbols: list[str], limit: int) -> tuple[list[dict], dict]:
    ensure_tradingagents_path()
    try:
        from tradingagents.dataflows.reddit import fetch_reddit_posts
    except Exception as exc:
        return [], source_status("reddit", "error", 0, f"import_failed:{exc}")
    events = []
    reddit_logger = logging.getLogger("tradingagents.dataflows.reddit")
    old_level = reddit_logger.level
    old_propagate = reddit_logger.propagate
    try:
        reddit_logger.setLevel(logging.CRITICAL + 1)
        reddit_logger.propagate = False
        for symbol in symbols[: min(4, len(symbols))]:
            text = fetch_reddit_posts(symbol, subreddits=("CryptoCurrency", "CryptoMarkets"), limit_per_sub=2, inter_request_delay=0.2)
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("[") or "]" not in stripped:
                    continue
                title = stripped.split("]", 1)[1].strip()
                if title:
                    events.append({"title": title, "source": "reddit", "source_type": "social", "published_at": utc_now(), "symbols": [symbol], "summary": ""})
                if len(events) >= limit:
                    break
            if len(events) >= limit:
                break
        return events, source_status("reddit", "ok" if events else "empty", len(events))
    except Exception as exc:
        return events, source_status("reddit", "error", len(events), str(exc))
    finally:
        reddit_logger.setLevel(old_level)
        reddit_logger.propagate = old_propagate

def fetch_rss(symbols: list[str], limit: int) -> tuple[list[dict], dict]:
    events = []
    for feed_url in RSS_FEEDS:
        if len(events) >= limit:
            break
        try:
            req = Request(feed_url, headers={"User-Agent": "trading-agent-news-observer/1.0", "Accept": "application/rss+xml, application/xml, text/xml"})
            with urlopen(req, timeout=8) as resp:
                root = ElementTree.fromstring(resp.read())
        except (HTTPError, URLError, TimeoutError, ElementTree.ParseError, OSError):
            continue
        channel_items = root.findall(".//item")
        for item in channel_items:
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            events.append(
                {
                    "title": title,
                    "summary": (item.findtext("description") or "")[:800],
                    "url": item.findtext("link") or "",
                    "source": feed_url.split("/")[2],
                    "source_type": "rss",
                    "published_at": item.findtext("pubDate") or utc_now(),
                }
            )
            if len(events) >= limit:
                break
    return events, source_status("rss", "ok" if events else "empty", len(events))

FETCHERS: tuple[Callable[[list[str], int], tuple[list[dict], dict]], ...] = (
    fetch_cryptopanic,
    fetch_alpha_vantage,
    fetch_yfinance_global,
    fetch_reddit,
    fetch_rss,
)

def fetch_all(symbols: list[str], max_items_per_source: int) -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    health: list[dict] = []
    for fetcher in FETCHERS:
        rows, status = fetcher(symbols, max_items_per_source)
        health.append(status)
        events.extend(rows)
    return events, health

def write_events(events: list[dict], events_path: Path | None = None) -> list[dict]:
    events_path = events_path or EVENTS_JSONL
    existing = read_existing_ids(events_path)
    written = []
    now = utc_now()
    for raw in events:
        event = normalize_event(raw, now=now)
        if not event.get("event_id"):
            event["event_id"] = stable_event_id(event.get("title", ""), event.get("source", ""), event.get("url", ""), event.get("published_at", ""))
        if event["event_id"] in existing:
            continue
        existing.add(event["event_id"])
        append_jsonl(events_path, event)
        safe_append_event("news_observer", "news_event", event, ts=event.get("ts_seen"))
        written.append(event)
    return written

def recent_events(path: Path | None = None, max_lines: int = 500) -> list[dict]:
    path = path or EVENTS_JSONL
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]:
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
        except Exception:
            continue
    return rows

def write_heartbeat(status: str, payload: dict | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    HEARTBEAT_PATH.write_text(json.dumps(row, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    safe_upsert_heartbeat("news_observer", status, row, ts=row["ts"])

def heartbeat_age_seconds() -> float | None:
    row = read_json(HEARTBEAT_PATH)
    try:
        ts = datetime.fromisoformat(str(row.get("ts")).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    except Exception:
        return None

def build_snapshot(symbols: list[str], max_items_per_source: int) -> dict:
    fetched, health = fetch_all(symbols, max_items_per_source)
    written = write_events(fetched)
    history = recent_events()
    scored = score_events(history, source_health=health)
    scored["symbols"] = symbols
    scored["new_event_count"] = len(written)
    scored["fetched_event_count"] = len(fetched)
    return scored

def run_once(symbols: list[str], max_items_per_source: int) -> dict:
    snapshot = build_snapshot(symbols, max_items_per_source)
    save_latest(snapshot, LATEST_JSON, LATEST_MD)
    safe_append_snapshot("news_observer", "news_state", snapshot, ts=snapshot.get("ts"))
    write_heartbeat("ok", {"event_count": snapshot.get("event_count"), "new_event_count": snapshot.get("new_event_count"), "macro_risk_score": snapshot.get("macro_risk_score")})
    return snapshot

def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None

def is_pid_running(pid: int | None, expected_script: str | None = None) -> bool:
    if not pid:
        return False
    if os.name != "nt":
        proc = Path(f"/proc/{pid}")
        if not proc.exists():
            return False
        return True
    try:
        import subprocess

        script_check = ""
        if expected_script:
            escaped = expected_script.replace("'", "''")
            script_check = f"; if ($p.CommandLine -notlike '*{escaped}*') {{ exit 2 }}"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction Stop; if (-not $p) {{ exit 1 }}{script_check}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False

def interruptible_sleep(seconds: float, stop_file: Path) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not stop_file.exists():
        time.sleep(min(1.0, max(0.0, deadline - time.time())))

def status() -> int:
    pid = read_pid(PID_FILE)
    print(f"news_observer_pid={pid} running={is_pid_running(pid, 'news_observer.py')}")
    print(f"latest_json={LATEST_JSON}")
    print(f"latest_md={LATEST_MD}")
    print(f"events_jsonl={EVENTS_JSONL}")
    print(f"heartbeat={HEARTBEAT_PATH} age_seconds={heartbeat_age_seconds()}")
    print(f"stop_file={STOP_FILE}")
    return 0

def run_loop(args: argparse.Namespace) -> int:
    load_env()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(PID_FILE)
    if not args.once and existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid, "news_observer.py"):
        print(f"news observer already running pid={existing_pid}", flush=True)
        return 0
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    symbols = parse_symbols(args.symbols)
    while not STOP_FILE.exists():
        try:
            snapshot = run_once(symbols, args.max_items_per_source)
            print(f"news_update ts={snapshot['ts']} events={snapshot.get('event_count')} macro_risk={snapshot.get('macro_risk_score')}", flush=True)
        except Exception as exc:
            safe_append_event("news_observer", "observer_error", {"error": str(exc)[:300]}, ts=utc_now())
            write_heartbeat("error", {"error": str(exc)[:300]})
            print(f"news_observer_error {str(exc)[:160]}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds, STOP_FILE)
    return 0

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write continuous crypto news/macro updates")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=600.0)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--max-items-per-source", type=int, default=20)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.max_items_per_source < 1:
        parser.error("--max-items-per-source must be >= 1")
    return args

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        return status()
    return run_loop(args)

if __name__ == "__main__":
    raise SystemExit(main())
