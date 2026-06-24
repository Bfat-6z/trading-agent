"""24/7 market observer for Binance USDT-M futures.

The observer does not trade. It writes a rolling market brief to markdown plus
JSONL so the trading agent has continuous situational awareness while the
executor remains separately guarded by scalp_watchdog.py.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

from event_store import safe_append_event, safe_append_snapshot, safe_upsert_heartbeat

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
STOP_FILE = STATE_DIR / "STOP_MARKET_OBSERVER"
PID_FILE = STATE_DIR / "market_observer.pid"
HEARTBEAT_PATH = STATE_DIR / "market_observer_heartbeat.json"
JSONL_PATH = STATE_DIR / "market_updates.jsonl"
LATEST_JSON = STATE_DIR / "market_updates_latest.json"
LATEST_MD = STATE_DIR / "market_updates_latest.md"
SCALP_LOG = STATE_DIR / "scalp_autotrader.jsonl"

EXCLUDE_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD", "PYUSD", "EUR", "USD1",
    "XPLUS", "LINK",  # stale reduce-only algo history in this workspace
}


@dataclass(frozen=True)
class FuturesTicker:
    symbol: str
    price: float
    change_pct: float
    quote_volume: float
    trade_count: int
    high: float
    low: float

    @property
    def base(self) -> str:
        return self.symbol[:-4] if self.symbol.endswith("USDT") else self.symbol

    @property
    def range_pos(self) -> float:
        if self.high <= self.low:
            return 0.5
        return max(0.0, min(1.0, (self.price - self.low) / (self.high - self.low)))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request_json(url: str, params: dict | None = None, timeout: float = 10.0) -> object:
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def parse_ticker(row: dict) -> FuturesTicker | None:
    symbol = str(row.get("symbol", "")).upper()
    if not symbol.endswith("USDT"):
        return None
    base = symbol[:-4]
    if base in EXCLUDE_BASES or any(token in base for token in ("UP", "DOWN", "BULL", "BEAR")):
        return None
    try:
        return FuturesTicker(
            symbol=symbol,
            price=float(row["lastPrice"]),
            change_pct=float(row["priceChangePercent"]),
            quote_volume=float(row["quoteVolume"]),
            trade_count=int(row.get("count", 0)),
            high=float(row["highPrice"]),
            low=float(row["lowPrice"]),
        )
    except Exception:
        return None


def fetch_tickers() -> list[FuturesTicker]:
    rows = request_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not isinstance(rows, list):
        raise RuntimeError("unexpected ticker response")
    tickers = [parsed for row in rows if (parsed := parse_ticker(row))]
    return [ticker for ticker in tickers if ticker.quote_volume > 0]


def fetch_funding() -> dict[str, float]:
    rows = request_json("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10)
    if isinstance(rows, dict):
        rows = [rows]
    funding: dict[str, float] = {}
    if not isinstance(rows, list):
        return funding
    for row in rows:
        try:
            symbol = str(row.get("symbol", "")).upper()
            if symbol.endswith("USDT"):
                funding[symbol] = float(row.get("lastFundingRate", 0.0)) * 100
        except Exception:
            continue
    return funding


def hot_score(ticker: FuturesTicker) -> float:
    volume_score = math.log10(max(ticker.quote_volume, 1))
    activity_score = math.log10(max(ticker.trade_count, 1)) / 2
    move_score = abs(ticker.change_pct) / 4
    edge_zone = 0.25 if 0.2 <= ticker.range_pos <= 0.8 else -0.25
    return volume_score + activity_score + move_score + edge_zone


def top_n(items: Iterable[FuturesTicker], key, n: int) -> list[FuturesTicker]:
    return sorted(items, key=key, reverse=True)[:n]


def latest_executor_state(path: Path = SCALP_LOG, tail_lines: int = 80) -> dict:
    if not path.exists():
        return {"status": "no_log"}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-tail_lines:]
    except Exception as exc:
        return {"status": "read_error", "error": str(exc)[:120]}
    last_events: dict[str, dict] = {}
    recent_risk_block = None
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        event = row.get("event")
        if event:
            last_events[event] = row
        if event == "risk_block":
            recent_risk_block = row
    return {
        "status": "ok",
        "last_start": last_events.get("start"),
        "last_signal": last_events.get("signal"),
        "last_paper_open": last_events.get("paper_open"),
        "last_paper_close": last_events.get("paper_close"),
        "recent_risk_block": recent_risk_block,
    }


def ticker_payload(ticker: FuturesTicker, funding: dict[str, float]) -> dict:
    return {
        **asdict(ticker),
        "range_pos": round(ticker.range_pos, 4),
        "funding_pct": funding.get(ticker.symbol),
        "hot_score": round(hot_score(ticker), 4),
    }


def build_snapshot(limit: int = 10) -> dict:
    tickers = fetch_tickers()
    funding = fetch_funding()
    by_symbol = {ticker.symbol: ticker for ticker in tickers}
    top_volume = top_n(tickers, lambda item: item.quote_volume, limit)
    top_gainers = top_n(tickers, lambda item: item.change_pct, limit)
    top_losers = sorted(tickers, key=lambda item: item.change_pct)[:limit]
    hot = top_n(tickers, hot_score, limit)
    funding_extremes = sorted(
        [ticker for ticker in tickers if ticker.symbol in funding],
        key=lambda item: abs(funding[item.symbol]),
        reverse=True,
    )[:limit]
    majors = [by_symbol[symbol] for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT") if symbol in by_symbol]
    return {
        "ts": utc_now(),
        "universe_count": len(tickers),
        "majors": [ticker_payload(ticker, funding) for ticker in majors],
        "top_volume": [ticker_payload(ticker, funding) for ticker in top_volume],
        "top_gainers": [ticker_payload(ticker, funding) for ticker in top_gainers],
        "top_losers": [ticker_payload(ticker, funding) for ticker in top_losers],
        "hot": [ticker_payload(ticker, funding) for ticker in hot],
        "funding_extremes": [ticker_payload(ticker, funding) for ticker in funding_extremes],
        "executor": latest_executor_state(),
    }


def format_money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    return f"${value:,.0f}"


def table(title: str, rows: list[dict], include_funding: bool = False) -> str:
    headers = "| Symbol | Price | 24h% | Quote vol | Range | Score |"
    sep = "|---|---:|---:|---:|---:|---:|"
    if include_funding:
        headers = "| Symbol | Price | 24h% | Funding% | Quote vol | Range |"
        sep = "|---|---:|---:|---:|---:|---:|"
    lines = [f"## {title}", headers, sep]
    for row in rows:
        if include_funding:
            funding = row.get("funding_pct")
            funding_text = "n/a" if funding is None else f"{funding:+.4f}"
            lines.append(
                f"| {row['symbol']} | {row['price']:.6g} | {row['change_pct']:+.2f} | {funding_text} | {format_money(row['quote_volume'])} | {row['range_pos']:.2f} |"
            )
        else:
            lines.append(
                f"| {row['symbol']} | {row['price']:.6g} | {row['change_pct']:+.2f} | {format_money(row['quote_volume'])} | {row['range_pos']:.2f} | {row['hot_score']:.2f} |"
            )
    return "\n".join(lines)


def executor_summary(executor: dict) -> str:
    risk = executor.get("recent_risk_block")
    signal = executor.get("last_signal") or {}
    paper_open = executor.get("last_paper_open") or {}
    lines = ["## Executor State"]
    if risk:
        lines.append(f"- Risk block: `{risk.get('reason')}` count={risk.get('count')} at {risk.get('ts')}")
    if signal.get("signal"):
        sig = signal["signal"]
        lines.append(f"- Last signal: {sig.get('symbol')} {sig.get('side')} score={sig.get('score')} at {signal.get('ts')}")
    if paper_open.get("position"):
        pos = paper_open["position"]
        lines.append(f"- Last paper open: {pos.get('symbol')} {pos.get('side')} entry={pos.get('entry')} SL={pos.get('stop')} TP={pos.get('take_profit')}")
    if len(lines) == 1:
        lines.append("- No executor events yet.")
    return "\n".join(lines)


def render_markdown(snapshot: dict) -> str:
    parts = [
        "# Market Update",
        f"Generated: {snapshot['ts']}",
        f"Universe: {snapshot['universe_count']} USDT-M symbols",
        table("Majors", snapshot["majors"]),
        table("Hot Symbols", snapshot["hot"]),
        table("Top Volume", snapshot["top_volume"]),
        table("Top Gainers", snapshot["top_gainers"]),
        table("Top Losers", snapshot["top_losers"]),
        table("Funding Extremes", snapshot["funding_extremes"], include_funding=True),
        executor_summary(snapshot.get("executor", {})),
    ]
    return "\n\n".join(parts) + "\n"


def write_snapshot(snapshot: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
    LATEST_JSON.write_text(json.dumps(snapshot, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    LATEST_MD.write_text(render_markdown(snapshot), encoding="utf-8")
    safe_append_snapshot("market_observer", "market_update", snapshot, ts=snapshot.get("ts"))


def write_heartbeat(status: str, payload: dict | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    HEARTBEAT_PATH.write_text(json.dumps(row, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    safe_upsert_heartbeat("market_observer", status, row, ts=row["ts"])


def heartbeat_age_seconds() -> float | None:
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        row = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(str(row.get("ts")).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def interruptible_sleep(seconds: float, stop_file: Path) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not stop_file.exists():
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None


def is_pid_running(pid: int | None, expected_script: str | None = None) -> bool:
    if not pid:
        return False
    try:
        import subprocess

        script_check = ""
        if expected_script:
            escaped = expected_script.replace("'", "''")
            script_check = f"; if ($p.CommandLine -notlike '*{escaped}*') {{ exit 2 }}"
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction Stop; if (-not $p) {{ exit 1 }}{script_check}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False


def status() -> int:
    pid = read_pid(PID_FILE)
    print(f"market_observer_pid={pid} running={is_pid_running(pid, 'market_observer.py')}")
    print(f"latest_md={LATEST_MD}")
    print(f"latest_json={LATEST_JSON}")
    print(f"jsonl={JSONL_PATH}")
    print(f"heartbeat={HEARTBEAT_PATH} age_seconds={heartbeat_age_seconds()}")
    print(f"stop_file={STOP_FILE}")
    return 0


def run_once(limit: int) -> dict:
    snapshot = build_snapshot(limit=limit)
    write_snapshot(snapshot)
    return snapshot


def run_loop(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(PID_FILE)
    if not args.once and existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid, "market_observer.py"):
        print(f"market observer already running pid={existing_pid}", flush=True)
        return 0
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        try:
            snapshot = run_once(args.limit)
            write_heartbeat("ok", {"latest_ts": snapshot["ts"], "hot": snapshot["hot"][0]["symbol"] if snapshot["hot"] else None})
            print(f"market_update ts={snapshot['ts']} hot={snapshot['hot'][0]['symbol'] if snapshot['hot'] else 'n/a'}", flush=True)
        except Exception as exc:
            row = {"ts": utc_now(), "event": "observer_error", "error": str(exc)[:300]}
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with JSONL_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
            safe_append_event("market_observer", "observer_error", {"error": str(exc)[:300]}, ts=row["ts"])
            write_heartbeat("error", {"error": str(exc)[:300]})
            print(f"observer_error {str(exc)[:160]}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds, STOP_FILE)
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write continuous Binance futures market updates")
    parser.add_argument("--status", action="store_true", help="print observer status and exit")
    parser.add_argument("--once", action="store_true", help="write one update then exit")
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.limit < 3:
        parser.error("--limit must be >= 3")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        return status()
    return run_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
