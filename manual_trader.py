"""Manual PAPER-trade tester — place limit/market orders by hand with SL,
optional TP, and a scale-out-at-profit rule ("take half at +X%, ride the rest").
A resolve loop fills pending limits, cancels on pump/expiry, and manages open
positions (liquidation -> SL -> TP -> scale-out, pessimistic ordering).

SEPARATE account from llm_trader so manual trades never pollute the LLM's edge
scorecard. Reuses the verified llm_trader_risk math. PAPER-ONLY: its own account,
NEVER calls futures_create_order / any live path; live_guard untouched.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import llm_trader_charts as ltc
import llm_trader_risk as lr
import orderflow_data as of
import paper_cost_model as pcm

ROOT = Path(__file__).resolve().parent
MT = ROOT / "state" / "manual_trader"
ACCOUNT = MT / "account.json"
POSITIONS = MT / "positions.jsonl"
CLOSED = MT / "closed.jsonl"
PENDING = MT / "pending.jsonl"
HEARTBEAT = MT / "manual_trader_heartbeat.json"
STOP = MT / "manual_trader.stop"
PID = MT / "manual_trader.pid"
CHARTS_DIR = ROOT.parent / "horizon-ui" / "charts"
START_EQUITY = 100.0
TF = "15m"


# --------------------------------------------------------------------------- io
def _load(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try: out.append(json.loads(line))
            except Exception: pass
    return out


def _rewrite(p: Path, rows: list[dict[str, Any]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")


def _append(p: Path, row: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def load_account() -> dict[str, Any]:
    if ACCOUNT.exists():
        try: return json.loads(ACCOUNT.read_text())
        except Exception: pass
    return {"equity": START_EQUITY, "realized": 0.0, "trades": 0, "wins": 0}


def save_account(a: dict[str, Any]) -> None:
    ACCOUNT.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNT.write_text(json.dumps(a, indent=1, default=str), encoding="utf-8")


def _save_chart(client, symbol, entry, sl, tp, now_ms) -> str | None:
    try:
        bars = of.fetch_klines_with_flow(symbol, TF, months=0.12, end_ms=now_ms, client=client, sleep_between=0.02)
        hl = [(entry, "ENTRY", "#f0b90b")]
        if sl: hl.append((sl, "SL", "#ef5350"))
        if tp: hl.append((tp, "TP", "#26a69a"))
        b64 = ltc.render_chart(symbol, bars, tf=TF, hlines=hl, title_suffix=" · MANUAL ENTRY")
        if b64:
            CHARTS_DIR.mkdir(parents=True, exist_ok=True)
            fn = f"manual_{symbol}_{int(now_ms)}.png"
            (CHARTS_DIR / fn).write_bytes(base64.b64decode(b64))
            return "charts/" + fn
    except Exception:
        pass
    return None


def _scale_price(side: str, entry: float, lev: int, trigger_pct: float) -> float:
    """Price at which uPnL == trigger_pct% of margin (isolated, x`lev`)."""
    move = (trigger_pct / 100.0) / lev
    return entry * (1 + move) if side == "LONG" else entry * (1 - move)


# --------------------------------------------------------------------- place
def _next_id() -> str:
    return f"m{len(_load(PENDING)) + len(_load(POSITIONS)) + len(_load(CLOSED)) + 1}"


def place_market(client, symbol: str, side: str, margin: float, lev: int, *,
                 sl: float | None = None, tp: float | None = None,
                 scale_out: dict | None = None, note: str = "", now_ms: int | None = None) -> dict:
    import time as _t
    now_ms = int(now_ms if now_ms is not None else _t.time() * 1000)
    tk = [x for x in client.futures_ticker() if x["symbol"] == symbol][0]
    raw = float(tk["lastPrice"])
    tier = pcm.liquidity_tier(3e9)
    slip = float(pcm.fill_bps(tier)) / 10000.0
    entry = raw * (1 + slip) if side == "LONG" else raw * (1 - slip)
    return _open(client, symbol, side, entry, margin, lev, sl, tp, scale_out, note, now_ms, kind="market")


def place_limit(symbol: str, side: str, limit_price: float, margin: float, lev: int, *,
                sl: float | None = None, tp: float | None = None,
                cancel_above: float | None = None, cancel_below: float | None = None,
                expires_hours: float = 48.0, scale_out: dict | None = None,
                note: str = "", now_ms: int | None = None) -> dict:
    import time as _t
    now_ms = int(now_ms if now_ms is not None else _t.time() * 1000)
    row = {"id": _next_id(), "symbol": symbol, "side": side, "kind": "limit",
           "limit_price": float(limit_price), "margin": float(margin), "leverage": int(lev),
           "sl": sl, "tp": tp, "cancel_above": cancel_above, "cancel_below": cancel_below,
           "expires_ts": now_ms + int(expires_hours * 3600 * 1000),
           "scale_out": scale_out, "note": note, "source": "manual",
           "status": "pending", "created_ts": now_ms}
    _append(PENDING, row)
    return row


def _open(client, symbol, side, entry, margin, lev, sl, tp, scale_out, note, now_ms, kind,
          entry_ts=None) -> dict:
    # entry_ts = the bar the position actually started on. For a LIMIT fill this
    # MUST be the fill bar's ts_ms, not now_ms — otherwise the resolver's
    # `ts_ms > entry_ts` filter finds ZERO closed bars (every closed bar has
    # ts_ms < now_ms) and never evaluates an SL/TP/liq that hit on/after the fill.
    if entry_ts is None:
        entry_ts = now_ms
    mmr = lr.mmr_for(symbol)
    liq = lr.liquidation_price(entry, lev, side, mmr)
    qty = (margin * lev) / entry if entry > 0 else 0.0
    so = None
    if scale_out:
        so = {"trigger_pct": float(scale_out.get("trigger_pct", 100)),
              "frac": float(scale_out.get("frac", 0.5)), "done": False}
        so["price"] = round(_scale_price(side, entry, lev, so["trigger_pct"]), 6)
    chart = _save_chart(client, symbol, entry, sl, tp, now_ms)
    pos = {"id": _next_id(), "symbol": symbol, "side": side, "entry": round(entry, 6),
           "qty": qty, "qty0": qty, "margin": round(margin, 4), "margin0": round(margin, 4),
           "leverage": int(lev), "sl": sl, "tp": tp, "liq_px": liq, "mmr": mmr,
           "scale_out": so, "no_timeout": True, "source": "manual", "kind": kind,
           "entry_ts": int(entry_ts), "opened_at_ms": now_ms, "note": note, "chart": chart}
    rows = _load(POSITIONS); rows.append(pos); _rewrite(POSITIONS, rows)
    return pos


# ------------------------------------------------------------------- resolve
def _fut_bars(client, symbol, since_ts, now_ms):
    fb = of.fetch_klines_with_flow(symbol, TF, months=0.06, end_ms=now_ms, client=client, sleep_between=0.02)
    bar_ms = of._TF_MS[TF]
    return [b for b in fb if int(b["ts_ms"]) > int(since_ts) and int(b["ts_ms"]) + bar_ms <= now_ms]


def _resolve_pending(client, now_ms) -> int:
    pend = _load(PENDING)
    if not pend:
        return 0
    still, changed = [], 0
    for od in pend:
        if od.get("status") != "pending":
            continue
        sym, side, lim = od["symbol"], od["side"], float(od["limit_price"])
        try:
            fut = _fut_bars(client, sym, od["created_ts"], now_ms)
        except Exception:
            still.append(od); continue
        filled = cancelled = None
        fill_ts = None
        for b in fut:
            hi, lo = float(b["high"]), float(b["low"])
            # fill: buy-limit fills when price trades DOWN to it; sell-limit when UP to it
            if (side == "LONG" and lo <= lim) or (side == "SHORT" and hi >= lim):
                filled = lim; fill_ts = int(b["ts_ms"]); break
            if od.get("cancel_above") and hi >= float(od["cancel_above"]):
                cancelled = "pumped_above"; break
            if od.get("cancel_below") and lo <= float(od["cancel_below"]):
                cancelled = "dropped_below"; break
        if filled is None and now_ms >= int(od.get("expires_ts", 0)):
            cancelled = "expired"
        if filled is not None:
            # entry_ts = fill bar MINUS 1 so the resolver evaluates the fill bar
            # itself too (adverse-only there via fill_bar_ts: SL/liq can fire on
            # it, TP never — the intrabar order vs our fill is unknown).
            newp = _open(client, sym, side, filled, float(od["margin"]), int(od["leverage"]),
                         od.get("sl"), od.get("tp"), od.get("scale_out"), od.get("note", "") + " [limit filled]",
                         now_ms, kind="limit", entry_ts=fill_ts - 1)
            try:
                rows_ = _load(POSITIONS)
                for rp in rows_:
                    if rp.get("id") == newp.get("id"):
                        rp["fill_bar_ts"] = fill_ts
                _rewrite(POSITIONS, rows_)
            except Exception:
                pass
            _append(CLOSED, {"event": "limit_filled", "id": od["id"], "symbol": sym, "side": side,
                             "fill": filled, "ts": now_ms})
            changed += 1
        elif cancelled:
            _append(CLOSED, {"event": "cancelled", "id": od["id"], "symbol": sym, "side": side,
                             "reason": cancelled, "ts": now_ms})
            changed += 1
        else:
            still.append(od)
    _rewrite(PENDING, still)
    return changed


def _book(acct, rec):
    net = float(rec["net"])
    acct["equity"] = round(float(acct["equity"]) + net, 4)
    acct["realized"] = round(float(acct["realized"]) + net, 4)
    acct["trades"] = int(acct["trades"]) + 1
    acct["wins"] = int(acct["wins"]) + (1 if net > 0 else 0)
    _append(CLOSED, rec)


def _resolve_positions(client, now_ms) -> int:
    pos = _load(POSITIONS)
    if not pos:
        return 0
    acct = load_account()
    still, n = [], 0
    for p in pos:
        sym, side = p["symbol"], p["side"]
        entry, qty, lev, margin = float(p["entry"]), float(p["qty"]), int(p["leverage"]), float(p["margin"])
        sl = float(p["sl"]) if p.get("sl") else None
        tp = float(p["tp"]) if p.get("tp") else None
        liq = float(p.get("liq_px") or lr.liquidation_price(entry, lev, side, lr.mmr_for(sym)))
        so = p.get("scale_out")
        try:
            fut = _fut_bars(client, sym, p["entry_ts"], now_ms)
        except Exception:
            still.append(p); continue
        exit_px = reason = None
        fb_ts = int(p.get("fill_bar_ts") or -1)
        for b in fut:
            hi, lo = float(b["high"]), float(b["low"])
            # pessimistic: liquidation -> SL -> TP
            if (side == "LONG" and lo <= liq) or (side == "SHORT" and hi >= liq):
                exit_px, reason = liq, "liquidation"; break
            if sl and ((side == "LONG" and lo <= sl) or (side == "SHORT" and hi >= sl)):
                exit_px, reason = sl, "sl"; break
            if int(b["ts_ms"]) == fb_ts:
                continue   # limit-fill bar: adverse-only (a TP touch may predate the fill)
            if tp and ((side == "LONG" and hi >= tp) or (side == "SHORT" and lo <= tp)):
                exit_px, reason = tp, "tp"; break
            # scale-out (take partial at +trigger%), only if not stopped this bar
            if so and not so.get("done"):
                sp = float(so["price"])
                if (side == "LONG" and hi >= sp) or (side == "SHORT" and lo <= sp):
                    frac = float(so["frac"]); qc = qty * frac
                    fee = (entry + sp) * qc * float(pcm.TAKER_FEE_RATE)
                    gross = (sp - entry) * qc if side == "LONG" else (entry - sp) * qc
                    net = gross - fee
                    _book(acct, {"symbol": sym, "side": side, "entry": entry, "exit": round(sp, 6),
                                 "reason": "scale_out_50", "net": round(net, 4),
                                 "r": round(net / (margin * frac), 3) if margin > 0 else 0,
                                 "leverage": lev, "note": p.get("note"), "chart": p.get("chart"),
                                 "source": "manual", "closed_ts": now_ms})
                    qty *= (1 - frac); margin *= (1 - frac)
                    so["done"] = True   # ride the rest
        if exit_px is None:
            p["qty"], p["margin"], p["scale_out"] = qty, round(margin, 4), so
            still.append(p); continue
        # full close of remaining qty
        tier = pcm.liquidity_tier(3e9)
        if reason == "sl":
            slip = float(pcm.fill_bps(tier, is_stop=True)) / 10000.0
            exit_px = exit_px * (1 - slip) if side == "LONG" else exit_px * (1 + slip)
        fee = float(lr.trade_costs(entry, exit_px, qty, 3e9)["fee"])
        net = lr.net_pnl(side, entry, exit_px, qty, margin, fee, 0.0, liquidated=(reason == "liquidation"))
        _book(acct, {"symbol": sym, "side": side, "entry": entry, "exit": round(exit_px, 6),
                     "reason": reason, "net": round(net, 4), "r": round(net / margin, 3) if margin > 0 else 0,
                     "leverage": lev, "note": p.get("note"), "chart": p.get("chart"),
                     "source": "manual", "closed_ts": now_ms})
        n += 1
    save_account(acct)
    _rewrite(POSITIONS, still)
    return n


def run_once() -> dict[str, Any]:
    import time as _t
    from tradingagents.binance.client import spot_client
    client = spot_client()
    now_ms = int(_t.time() * 1000)
    pend = _resolve_pending(client, now_ms)
    closed = _resolve_positions(client, now_ms)
    acct = load_account()
    return {"equity": acct["equity"], "trades": acct["trades"],
            "pending": len(_load(PENDING)), "open": len(_load(POSITIONS)),
            "pending_events": pend, "closed": closed, "live": "LOCKED"}


def _hb(last, status="running"):
    from atomic_state import write_json_atomic
    from timebase import utc_now
    write_json_atomic(HEARTBEAT, {"agent": "manual_trader", "pid": os.getpid(), "ts": utc_now(),
                                  "updated_at": utc_now(), "status": status, "last_run": last})


if __name__ == "__main__":
    import argparse, time
    ap = argparse.ArgumentParser(description="Manual PAPER-trade tester (paper-only)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-seconds", type=float, default=60.0)
    a = ap.parse_args()
    MT.mkdir(parents=True, exist_ok=True)
    PID.write_text(str(os.getpid()), encoding="ascii")
    if a.once:
        print(json.dumps(run_once(), default=str))
    else:
        while not STOP.exists():
            try: res = run_once()
            except Exception as exc: res = {"error": str(exc)[:200]}
            _hb(res)
            t = time.time() + a.interval_seconds
            while time.time() < t and not STOP.exists():
                time.sleep(1)
        _hb({}, status="stopped")
