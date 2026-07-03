"""Signal Follower — paper-trade the Telegram alert channels and MEASURE whether
following them actually beats the current bot's win rate.

The listed channels don't post entry/TP/SL calls — they post ACTIVITY alerts
(WhaleSniper: 'unusual buying/selling', cointrendz: 'pump detected', liquidations).
This turns each alert into a mechanical paper trade (buying->LONG, selling->SHORT,
pump->momentum LONG) with a fixed TP/SL, resolves it on REAL price, and tracks the
hit-rate PER channel and PER signal-type via signal_source_registry. No claim is
trusted — the scoreboard shows which (if any) alert type actually wins.

PAPER/OFFLINE only: never places a live order.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import orderflow_data as of
import signal_source_registry as ssr
import whale_flow_observer as wfo

ROOT = Path(__file__).resolve().parent
SF_DIR = ROOT / "state" / "signal_follower"
OPEN = SF_DIR / "open.jsonl"
CLOSED = SF_DIR / "closed.jsonl"
SEEN = SF_DIR / "seen_ids.json"
BOARD = SF_DIR / "scoreboard.json"
HEARTBEAT = ROOT / "state" / "signal_follower_heartbeat.json"

# channel -> how to read its alerts. Each entry: (kind, parser)
SIGNAL_CHANNELS = ("WhaleSniper", "cointrendz_pumpdetector", "cointrendz_whalehunter")

# paper params (fixed rule so the CHANNEL's edge — not ours — is what's measured)
SIZE_USDT = 10.0
LEVERAGE = 5
TP_PCT = 2.5
SL_PCT = 1.5
TIMEOUT_BARS = 24          # ~6h on 15m
FEE_RT = 0.0008


def _load(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _append(p: Path, row: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def _rewrite(p: Path, rows: list[dict[str, Any]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# parse an alert message -> {symbol, side, kind} or None
# ---------------------------------------------------------------------------

_SYM = re.compile(r"#([A-Z0-9]{2,12})(?:/?USDT)?\b")
_PUMP = re.compile(r"Pump\s*[-·]?\s*([A-Z0-9]{2,12})/USDT", re.I)


def parse_signal(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.replace("\n", " ")
    low = t.lower()

    # pump detector: "Pump - PARTI/USDT ... +7.34%" -> momentum LONG
    m = _PUMP.search(t)
    if m and "pump" in low:
        return {"symbol": m.group(1).upper() + "USDT", "side": "LONG", "kind": "pump"}

    # activity alerts: "Unusual buying/selling activity #SYM"
    if "unusual buying" in low or ("buying activity" in low):
        s = _SYM.search(t)
        if s:
            return {"symbol": s.group(1).upper() + "USDT", "side": "LONG", "kind": "buy_activity"}
    if "unusual selling" in low or ("selling activity" in low):
        s = _SYM.search(t)
        if s:
            return {"symbol": s.group(1).upper() + "USDT", "side": "SHORT", "kind": "sell_activity"}
    return None


# ---------------------------------------------------------------------------
# ingest: scrape channels, open a paper trade per NEW parseable signal
# ---------------------------------------------------------------------------

def _seen() -> set[str]:
    try:
        return set(json.loads(SEEN.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _mark_seen(ids: set[str]) -> None:
    SF_DIR.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(sorted(ids)[-4000:]), encoding="utf-8")


def _mark_price(client, symbol: str) -> float | None:
    try:
        for row in client.futures_ticker():
            if row.get("symbol") == symbol:
                return float(row["lastPrice"])
    except Exception:
        pass
    return None


def ingest(client) -> int:
    seen = _seen()
    opened = 0
    for ch in SIGNAL_CHANNELS:
        try:
            html = wfo.fetch_channel(ch) if hasattr(wfo, "fetch_channel") else \
                __import__("requests").get(wfo.BASE_URL.format(channel=ch), timeout=15,
                                           headers={"User-Agent": "Mozilla/5.0"}).text
            msgs = wfo.parse_telegram_messages(ch, html, limit=25)
        except Exception:
            continue
        for msg in msgs:
            import hashlib as _h
            mid = msg.get("permalink") or _h.sha1(
                f"{ch}|{msg.get('posted_at') or msg.get('source_posted_at') or ''}|{msg.get('text','')}".encode()).hexdigest()
            mid = f"{ch}:{mid}"
            if mid in seen:
                continue
            seen.add(mid)
            sig = parse_signal(msg.get("text", ""))
            if not sig or not sig["symbol"].endswith("USDT"):
                continue
            px = _mark_price(client, sig["symbol"])
            if not px:
                continue
            ssr.get_source(ch, "telegram")            # ensure registered
            side = sig["side"]
            sl = px * (1 - SL_PCT / 100) if side == "LONG" else px * (1 + SL_PCT / 100)
            tp = px * (1 + TP_PCT / 100) if side == "LONG" else px * (1 - TP_PCT / 100)
            _append(OPEN, {"channel": ch, "kind": sig["kind"], "symbol": sig["symbol"],
                           "side": side, "entry": round(px, 6), "sl": round(sl, 6),
                           "tp": round(tp, 6), "opened_ms": _now_ms(), "msg": msg.get("text", "")[:120]})
            opened += 1
    _mark_seen(seen)
    return opened


def _now_ms() -> int:
    import time as _t
    return int(_t.time() * 1000)


# ---------------------------------------------------------------------------
# resolve: check open signal-trades against real price; update channel hit-rate
# ---------------------------------------------------------------------------

def resolve(client, now_ms: int) -> int:
    rows = _load(OPEN)
    if not rows:
        return 0
    still, closed = [], 0
    bar_ms = of._TF_MS["15m"]
    # fetch window must cover the OLDEST open position, else its early bars fall
    # outside the window (missed SL/TP; a 40h-old trade once "resolved" as tp).
    oldest = min(int(p["opened_ms"]) for p in rows)
    months = max(0.05, (now_ms - oldest) / (30 * 86400000.0) * 1.2)
    for pos in rows:
        try:
            bars = of.fetch_klines_with_flow(pos["symbol"], "15m", months=months,
                                              end_ms=now_ms, client=client, sleep_between=0.02)
            fut = [b for b in bars if int(b["ts_ms"]) > int(pos["opened_ms"]) and int(b["ts_ms"]) + bar_ms <= now_ms]
        except Exception:
            still.append(pos); continue
        if not fut:
            still.append(pos); continue
        side, entry, sl, tp = pos["side"], pos["entry"], pos["sl"], pos["tp"]
        exit_px, reason = None, None
        for i, b in enumerate(fut):
            hi, low = float(b["high"]), float(b["low"])
            if side == "LONG":
                if low <= sl: exit_px, reason = sl, "sl"; break
                if hi >= tp: exit_px, reason = tp, "tp"; break
            else:
                if hi >= sl: exit_px, reason = sl, "sl"; break
                if low <= tp: exit_px, reason = tp, "tp"; break
            if int(b["ts_ms"]) - int(pos["opened_ms"]) >= TIMEOUT_BARS * bar_ms:
                exit_px, reason = float(b["close"]), "timeout"; break   # age-based, not window-index
        if exit_px is None:
            still.append(pos); continue
        gross = (exit_px / entry - 1) if side == "LONG" else (entry - exit_px) / entry
        net = gross - FEE_RT
        hit = net > 0
        ssr.update_source_outcome(pos["channel"], hit)
        _append(CLOSED, {**pos, "exit": round(exit_px, 6), "reason": reason,
                         "net_pct": round(net * 100, 3), "hit": hit, "closed_ms": now_ms})
        closed += 1
    _rewrite(OPEN, still)
    return closed


def scoreboard() -> dict[str, Any]:
    """Per-channel and per-signal-kind hit-rate from closed signal-trades."""
    closed = _load(CLOSED)
    by_ch, by_kind = {}, {}
    for c in closed:
        for key, bucket in ((c.get("channel"), by_ch), (c.get("kind"), by_kind)):
            b = bucket.setdefault(key, {"n": 0, "wins": 0, "net_pct": 0.0})
            b["n"] += 1
            b["wins"] += 1 if c.get("hit") else 0
            b["net_pct"] += float(c.get("net_pct", 0) or 0)
    def fin(d):
        for k, v in d.items():
            v["win_rate"] = round(v["wins"] / v["n"], 3) if v["n"] else None
            v["net_pct"] = round(v["net_pct"], 2)
        return d
    board = {"by_channel": fin(by_ch), "by_kind": fin(by_kind), "total": len(closed)}
    SF_DIR.mkdir(parents=True, exist_ok=True)
    BOARD.write_text(json.dumps(board, indent=1), encoding="utf-8")
    return board


def run_once(client) -> dict[str, Any]:
    now = _now_ms()
    resolved = resolve(client, now)
    opened = ingest(client)
    board = scoreboard()
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    HEARTBEAT.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     "ts_ms": now, "opened": opened, "resolved": resolved,
                                     "open": len(_load(OPEN)), "total_closed": board["total"]}, indent=1),
                         encoding="utf-8")
    return {"opened": opened, "resolved": resolved, "open": len(_load(OPEN)),
            "closed_total": board["total"], "live": "LOCKED"}


def main() -> None:
    import argparse
    import os
    import time as _t
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=300.0)
    args = ap.parse_args()
    os.environ.setdefault("INGEST_DECISION_CANDLES", "0")
    from tradingagents.binance.client import spot_client
    client = spot_client()
    while True:
        try:
            print(json.dumps(run_once(client), default=str))
        except Exception as e:
            print(json.dumps({"error": repr(e)[:200]}))
        if args.once:
            break
        _t.sleep(max(120.0, args.interval))


if __name__ == "__main__":
    main()
