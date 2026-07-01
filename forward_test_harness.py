"""Edge-research harness — FORWARD-TEST channel (HARNESS-B, forward-only).

The order-book / liquidation / whale signals have NO usable history (per the
feasibility audit), so they CANNOT be backtested. The only honest way to measure
their edge is forward-paper: record real signal snapshots at decision time now,
then tag the realized forward return once the horizon elapses in WALL-CLOCK time,
and accumulate expectancy per signal bucket over weeks.

This is strictly no-lookahead by construction: a snapshot records features known
at record time (decision_cutoff = now); a label is only written once a horizon
has fully elapsed, using price data that came strictly AFTER the snapshot.

IMPORTANT: forward-only is NOT an excuse to skip validation — it means HIGHER
risk and a longer wait. Never jump to live from lack of history. Paper-only;
live_guard untouched; ALLOW_LIVE_ORDERS never set.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
FT_DIR = ROOT / "state" / "forward_test"
SNAPSHOTS = FT_DIR / "snapshots.jsonl"
LABELED = FT_DIR / "labeled.jsonl"
HORIZONS_MIN = [15, 60, 240]     # +15m / +1h / +4h forward returns
MIN_SAMPLE = 200                 # need this many labeled samples before any read-out


def fetch_orderflow_snapshot(client: Any, symbol: str, now_iso: str, now_ms: int) -> dict[str, Any]:
    """Real order-flow features at decision time: order-book imbalance (top depth),
    funding rate, mark price. All are 'known now' — no future data."""
    ob = client.futures_order_book(symbol=symbol, limit=20)
    bids = [(float(p), float(q)) for p, q in ob.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in ob.get("asks", [])]
    bid_depth = sum(q for _, q in bids)
    ask_depth = sum(q for _, q in asks)
    total = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total if total else 0.0
    mark = client.futures_mark_price(symbol=symbol)
    mark_price = float(mark.get("markPrice", 0))
    funding = float(mark.get("lastFundingRate", 0))
    return {
        "symbol": symbol, "decision_cutoff": now_iso, "ts_ms": now_ms,
        "mark_price": mark_price, "ob_imbalance": round(imbalance, 6),
        "funding_rate": funding, "bid_depth": round(bid_depth, 4), "ask_depth": round(ask_depth, 4),
        "can_place_live_orders": False,
    }


def record_snapshots(client: Any, symbols: list[str], now_iso: str, now_ms: int,
                     snapshots_path: Path = SNAPSHOTS) -> int:
    """Record one snapshot per symbol. Starts / advances the forward-test clock."""
    snapshots_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(snapshots_path, "a", encoding="utf-8") as fh:
        for sym in symbols:
            try:
                snap = fetch_orderflow_snapshot(client, sym, now_iso, now_ms)
                fh.write(json.dumps(snap, default=str) + "\n")
                n += 1
            except Exception as exc:
                fh.write(json.dumps({"symbol": sym, "error": str(exc)[:120], "ts_ms": now_ms}) + "\n")
    return n


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def tag_matured_returns(client: Any, now_ms: int, *, snapshots_path: Path = SNAPSHOTS,
                        labeled_path: Path = LABELED) -> int:
    """For each snapshot whose horizon has fully elapsed and isn't labeled yet,
    fetch the price at snapshot_ts + horizon and record the realized return. Only
    matured horizons are labeled (strictly forward data)."""
    snaps = _load_jsonl(snapshots_path)
    already = {(r["ts_ms"], r["symbol"], r["horizon_min"]) for r in _load_jsonl(labeled_path)}
    labeled_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(labeled_path, "a", encoding="utf-8") as fh:
        for s in snaps:
            if s.get("error") or "mark_price" not in s:
                continue
            for h in HORIZONS_MIN:
                horizon_ms = h * 60_000
                mature_ts = int(s["ts_ms"]) + horizon_ms
                if now_ms < mature_ts:
                    continue  # not matured yet
                key = (s["ts_ms"], s["symbol"], h)
                if key in already:
                    continue
                fwd = _price_at(client, s["symbol"], mature_ts)
                if fwd is None:
                    continue
                entry = float(s["mark_price"])
                ret = (fwd - entry) / entry if entry else 0.0
                fh.write(json.dumps({
                    "ts_ms": s["ts_ms"], "symbol": s["symbol"], "horizon_min": h,
                    "ob_imbalance": s.get("ob_imbalance"), "funding_rate": s.get("funding_rate"),
                    "entry": entry, "forward": fwd, "return": round(ret, 8),
                }, default=str) + "\n")
                n += 1
    return n


def _price_at(client: Any, symbol: str, ts_ms: int) -> float | None:
    """Close price of the 1m kline covering ts_ms (strictly forward data)."""
    try:
        rows = client.futures_klines(symbol=symbol, interval="1m", startTime=ts_ms, limit=1)
        if rows:
            return float(rows[0][4])
    except Exception:
        return None
    return None


def summarize(labeled_path: Path = LABELED) -> dict[str, Any]:
    """Expectancy per (horizon, imbalance-sign) bucket. Reports 'insufficient
    sample' honestly until MIN_SAMPLE labels exist — forward edge takes weeks."""
    rows = _load_jsonl(labeled_path)
    buckets: dict[str, list[float]] = {}
    for r in rows:
        imb = r.get("ob_imbalance") or 0.0
        sign = "bid_heavy" if imb > 0.05 else ("ask_heavy" if imb < -0.05 else "balanced")
        key = f"h{r['horizon_min']}_{sign}"
        buckets.setdefault(key, []).append(float(r.get("return", 0)))
    out = {"total_labeled": len(rows), "min_sample": MIN_SAMPLE, "buckets": {}}
    for key, rets in sorted(buckets.items()):
        n = len(rets)
        mean = sum(rets) / n if n else 0.0
        out["buckets"][key] = {
            "n": n, "mean_return": round(mean, 8),
            "status": "ok" if n >= MIN_SAMPLE else f"insufficient_sample_{n}/{MIN_SAMPLE}",
        }
    out["verdict"] = ("insufficient_data_forward_test_still_accruing"
                      if len(rows) < MIN_SAMPLE else "readable")
    return out


DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]


def run_once(symbols: list[str] | None = None) -> dict[str, Any]:
    """One record + tag pass. Idempotent enough for a cron/supervisor to call on a
    fixed cadence to keep the forward-test clock advancing. Paper-only."""
    import time as _time
    from tradingagents.binance.client import spot_client
    from timebase import utc_now
    client = spot_client()
    now_ms = int(_time.time() * 1000)
    recorded = record_snapshots(client, symbols or DEFAULT_SYMBOLS, utc_now(), now_ms)
    labeled = tag_matured_returns(client, now_ms)
    summary = summarize()
    return {"recorded": recorded, "labeled": labeled, "verdict": summary["verdict"],
            "total_labeled": summary["total_labeled"]}


if __name__ == "__main__":
    result = run_once()
    print(json.dumps(result, default=str))
