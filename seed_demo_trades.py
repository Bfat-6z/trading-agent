"""Seed a few REAL paper trades + charts for the demo dashboard.

The production paper loop is (correctly) conservative — its risk gates, instrument
registry, and expectancy checks refuse to open trades for setups/symbols without
proven edge, so a live demo may show no trades for a long time. This tool creates
a handful of demo paper trades on liquid majors using REAL recent candles, the
REAL Phase-2 cost model, and the REAL chart renderer, so the dashboard has
something to show. Trades are tagged demo_seed=True.

Paper/simulation only. Never places live orders (no ALLOW_LIVE_ORDERS, no client).

Usage: venv\\Scripts\\python.exe seed_demo_trades.py
"""
from __future__ import annotations

import json
from pathlib import Path

import chart_candle_ingestor as ing
import chart_candle_service as ccs
import chart_indicator_engine as ie
import chart_snapshot_renderer as csr
import paper_execution_simulator as sim
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
TRADES = STATE / "agent_memory" / "paper_trades.jsonl"

DEMO_SYMBOLS = [
    ("BTCUSDT", 3.0e10, "LONG"),
    ("ETHUSDT", 1.5e10, "SHORT"),
    ("SOLUSDT", 5.0e9, "LONG"),
]


def seed() -> None:
    made = 0
    for symbol, qv, side in DEMO_SYMBOLS:
        ing.ingest_symbol(symbol, "5m", limit=120)
        batch = ccs.load_closed_candles(symbol, "5m", utc_now(), limit=120)
        bars = batch.get("bars") or []
        if len(bars) < 30:
            print(f"[seed] {symbol}: not enough candles, skip")
            continue
        entry = float(bars[-30]["close"])   # enter 30 bars ago so the trade can resolve
        atr = abs(float(bars[-1]["high"]) - float(bars[-1]["low"])) or entry * 0.004
        if side == "LONG":
            sl, tp = entry - 1.5 * atr, entry + 3.0 * atr
        else:
            sl, tp = entry + 1.5 * atr, entry - 3.0 * atr
        # size the position for a $100 sim account risking ~2% ($2) to SL, so the
        # demo PnL is realistic for the account (not a $58k BTC notional).
        risk_usd = 2.0
        per_unit_risk = abs(entry - sl) or (entry * 0.01)
        qty = max(risk_usd / per_unit_risk, 0.0)
        # resolve the trade against the real bars that followed, real costs
        exit_candles = [{"ts": b["close_time"], "open": float(b["open"]), "high": float(b["high"]),
                         "low": float(b["low"]), "close": float(b["close"])} for b in bars[-30:]]
        result = sim.simulate_exit(side, str(entry), f"{qty:.8f}", str(sl), str(tp), exit_candles, "5", quote_volume=qv)

        # render the entry chart (real candles + EMA + volume + SL/TP)
        ind = ie.compute_indicator_bundle(batch)
        risk = {"risk_plan_id": f"demo_{symbol}", "sl": sl, "tp_ladder": [{"price": tp}]}
        snap = csr.render_snapshot(batch, indicator_bundle=ind, risk_plan=risk)

        close_event = {
            "event": "paper_close",
            "demo_seed": True,
            "symbol": symbol,
            "side": side,
            "setup_id": "demo_ema_pullback",
            "entry": f"{entry:.4f}",
            "qty": f"{qty:.8f}",
            "sl": f"{sl:.4f}",
            "tp": f"{tp:.4f}",
            "exit": result.get("exit"),
            "reason": result.get("reason"),
            "gross": result.get("gross"),
            "fee": result.get("fee"),
            "net": result.get("net"),
            "liquidity_tier": result.get("liquidity_tier"),
            "chart_snapshot_ids": {"entry": snap.get("snapshot_id")},
            "chart_snapshot_image": snap.get("image_path"),
            "ts": utc_now(),
            "close_ts": utc_now(),
            "can_place_live_orders": False,
        }
        with open(TRADES, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(close_event, default=str) + "\n")
        made += 1
        print(f"[seed] {symbol} {side}: net={result.get('net')} reason={result.get('reason')} chart={snap.get('snapshot_id')}")
    print(f"[seed] created {made} demo paper trades with real charts")


if __name__ == "__main__":
    seed()
