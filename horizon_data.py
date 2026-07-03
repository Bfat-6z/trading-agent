"""Generate horizon-ui/data.json from the REAL paper/research state so the
dashboard shows truthful numbers (not the mockup). Honest by construction: reads
the actual paper account, forward-paper positions, forward-test labels, and the
research ledger. Paper-only; reads state only, never trades.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UI = ROOT.parent / "horizon-ui"
OUT = UI / "data.json"
CHARTS_DIR = UI / "charts"


def _live_position_chart(cli, p, now_ms: int) -> str | None:
    """Render (throttled ~90s) a CURRENT chart for an open position with
    entry/SL/TP marked, so clicking any open position shows a live picture even
    if it predates entry-chart saving. Returns a server-relative path or None."""
    sym = p.get("symbol")
    if not sym or cli is None:
        return None
    fn = f"live_{sym}.png"
    fp = CHARTS_DIR / fn
    try:
        if fp.exists() and (now_ms / 1000 - fp.stat().st_mtime) < 90:
            return "charts/" + fn   # fresh enough — reuse
    except Exception:
        pass
    try:
        import base64
        import llm_trader_charts as ltc
        import orderflow_data as of
        bars = of.fetch_klines_with_flow(sym, "15m", months=0.12, end_ms=now_ms, client=cli, sleep_between=0.02)
        entry = float(p.get("entry", 0) or 0); sl = float(p.get("sl", 0) or 0); tp = float(p.get("tp", 0) or 0)
        hlines = [(entry, "ENTRY", "#c99a00")]
        if sl: hlines.append((sl, "SL", "#d43a4b"))
        if tp: hlines.append((tp, "TP", "#0a9d66"))
        b64 = ltc.render_chart(sym, bars, tf="15m", hlines=hlines, title_suffix=" · LIVE POSITION")
        if b64:
            CHARTS_DIR.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(base64.b64decode(b64))
            return "charts/" + fn
    except Exception:
        pass
    return None


def _load_jsonl(p: Path):
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def build() -> dict:
    import time as _t
    from timebase import utc_now
    import research_governance as rg

    now_ms = int(_t.time() * 1000)
    st = ROOT / "state"
    acct = {}
    try:
        acct = json.loads((st / "paper_account.json").read_text())
    except Exception:
        pass
    equity = round(float(acct.get("equity", 0) or 0), 2)
    trades = int(acct.get("trades", 0) or 0)
    realized = round(float(acct.get("realized_pnl", 0) or 0), 2)

    fp_open = _load_jsonl(st / "forward_strategy" / "positions.jsonl")
    fp_closed = _load_jsonl(st / "forward_strategy" / "closed.jsonl")

    # REAL live price series — ALWAYS fetched so the chart moves in real time even
    # with no open position (then it's a market-watch of the primary symbol).
    # Best-effort; if an open position exists we chart its symbol + overlay SL/TP.
    price_series, live_price = [], None
    live_sym = (fp_open[0].get("symbol") if fp_open else "BTCUSDT")
    has_pos = bool(fp_open)
    quotes = []
    price_map = {}   # symbol -> last price, for marking open positions to market
    # last-good price cache: a transient Binance hiccup must NOT blank the ticker
    # tape or zero-out open-position MTM (that would read as a wrong number).
    cache_path = st / "llm_trader" / "price_cache.json"
    cli = None
    try:
        from tradingagents.binance.client import spot_client
        cli = spot_client()
    except Exception:
        cli = None
    # (legacy) 5m series for the live tip fallback — isolated so its failure
    # can't take down the ticker/MTM fetch below.
    if cli is not None:
        try:
            kl = cli.futures_klines(symbol=live_sym, interval="5m", limit=96)
            price_series = [round(float(r[4]), 2) for r in kl]
            live_price = price_series[-1] if price_series else None
        except Exception:
            pass
        # REAL bulk ticker: full price map for MTM + header tape — own try block.
        try:
            all_t = cli.futures_ticker()
            for t in all_t:
                sym = t.get("symbol")
                if sym:
                    price_map[sym] = float(t.get("lastPrice", 0) or 0)
            want = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                    "DOGEUSDT", "SUIUSDT", "AVAXUSDT", "ADAUSDT", "LINKUSDT"]
            stats = {t["symbol"]: t for t in all_t if t.get("symbol") in want}
            for s in want:
                t = stats.get(s)
                if t:
                    quotes.append({"s": s.replace("USDT", ""),
                                   "px": float(t.get("lastPrice", 0) or 0),
                                   "chg": round(float(t.get("priceChangePercent", 0) or 0), 2)})
        except Exception:
            pass
    # Fallback to last-good cache when the bulk ticker missed this cycle.
    if not quotes or not price_map:
        try:
            cached = json.loads(cache_path.read_text())
            if not quotes:
                quotes = cached.get("quotes", [])
            if not price_map:
                price_map = {k: float(v) for k, v in (cached.get("price_map") or {}).items()}
        except Exception:
            pass
    elif quotes and price_map:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"quotes": quotes, "price_map": price_map,
                                              "cached_at": utc_now()}), encoding="utf-8")
        except Exception:
            pass
    fp_rs = [float(c.get("r_multiple", 0)) for c in fp_closed]
    fp_mean = round(sum(fp_rs) / len(fp_rs), 4) if fp_rs else 0.0

    ft_labels = 0
    try:
        ft = json.loads((st / "forward_test" / "forward_test_harness_heartbeat.json").read_text())
        ft_labels = int(ft.get("last_run", {}).get("total_labeled", 0) or 0)
    except Exception:
        pass

    ledger = _load_jsonl(st / "agent_memory" / "research_ledger.jsonl")
    # family matrix: family x timeframe -> best in-sample expectancy + verdict
    fam = []
    for r in ledger:
        insample = r.get("in_sample") or {}
        exp = insample.get("expectancy_r")
        if exp is None:
            continue
        fam.append({
            "family": str(r.get("family", "?")),
            "tf": str(r.get("timeframe", "?")),
            "exp": round(float(exp), 3),
            "trades": int(insample.get("trades", 0) or 0),
            "verdict": str(r.get("verdict", "?")),
        })
    fam = fam[-24:]  # most recent

    try:
        global_trials = rg.global_trial_count()
    except Exception:
        global_trials = 0

    killed_cells = sum(1 for r in ledger if str(r.get("verdict", "")).startswith("KILL"))
    # distinct research families (exclude the LOCKED_CONCLUSION note row)
    fam_labels = {str(r.get("family", "?")) for r in ledger if str(r.get("family", "")) != "LOCKED_CONCLUSION"}
    families_distinct = len(fam_labels)

    # best component (the forward-paper lead). insample_exp is a FIXED historical
    # backtest figure (round B, +0.092R / 233 trades) — labelled as backtest in the
    # UI; the FORWARD result below is the real out-of-sample truth.
    lead = {
        "name": "donchian-committed + kaufman-eff + volume",
        "insample_exp": 0.092, "insample_trades": 233, "insample_note": "backtest · not DSR-significant",
        "forward_open": len(fp_open),
        "forward_closed": len(fp_closed),
        "forward_mean_r": fp_mean,
        "status": ("FORWARD-PAPER · unconfirmed" if len(fp_closed) < 200 else "readable"),
    }

    # LLM discretionary trader (llm_trader.py) — separate paper account, its
    # measured scorecard is the only edge claim allowed on the dashboard.
    lt = {"equity": None, "trades": 0, "win_rate": None, "verdict": "—",
          "pvalue": None, "open": [], "closed_recent": [], "liq_count": 0}
    try:
        lt_dir = st / "llm_trader"
        la = json.loads((lt_dir / "account.json").read_text())
        lt["equity"] = round(float(la.get("equity", 0) or 0), 2)
        lt["trades"] = int(la.get("trades", 0) or 0)
        lt["win_rate"] = (round(int(la.get("wins", 0)) / lt["trades"], 3) if lt["trades"] else None)
        card = json.loads((lt_dir / "scorecard.json").read_text())
        lt["verdict"] = str(card.get("verdict", {}).get("code", "—"))
        lt["pvalue"] = card.get("pvalue")
        lt["liq_count"] = int(card.get("metrics", {}).get("liq_count", 0) or 0)
        lt["mean_r"] = card.get("metrics", {}).get("mean_r")
        open_rows = _load_jsonl(lt_dir / "positions.jsonl")
        # dedupe double-booked closes (concurrent-loop overlap) by trade identity
        _cr = _load_jsonl(lt_dir / "closed.jsonl"); _seen = set(); closed_rows = []
        for _c in _cr:
            if _c.get("event") or _c.get("net") is None:
                continue                            # event rows (limit_cancelled) are NOT trades — skip
            _k = (_c.get("symbol"), _c.get("side"), round(float(_c.get("entry", 0) or 0), 4),
                  round(float(_c.get("exit", 0) or 0), 4), round(float(_c.get("net", 0) or 0), 4), _c.get("reason"))
            if _k in _seen:
                continue
            _seen.add(_k); closed_rows.append(_c)
        # positions enriched with live mark price + per-position unrealized PnL
        # (Binance-style positions table).
        pos_out = []
        for p in open_rows:
            entry = float(p.get("entry", 0) or 0); qty = float(p.get("qty", 0) or 0)
            side = p.get("side"); mark = price_map.get(p.get("symbol"))
            up = None
            if mark:
                up = round(((mark - entry) if side == "LONG" else (entry - mark)) * qty, 3)
            live_chart = _live_position_chart(cli, p, now_ms)
            pos_out.append({"sym": p.get("symbol"), "side": side, "lev": p.get("leverage"),
                            "margin": round(float(p.get("margin", 0) or 0), 2),
                            "entry": round(entry, 4), "mark": round(float(mark), 4) if mark else None,
                            "liq": round(float(p.get("liq_px", 0) or 0), 4),
                            "margin": round(float(p.get("margin", 0) or 0), 3),
                            "upnl": up, "opened_at": p.get("opened_at"),
                            "chart": live_chart or p.get("chart"),
                            "chart_kind": ("current" if live_chart else ("entry" if p.get("chart") else None)),
                            "rationale": (p.get("rationale") or "")[:180]})
        lt["open"] = pos_out
        # LIVE TRADE FEED — last 40 closed trades, full detail, newest first.
        lt["feed"] = [{"sym": c.get("symbol"), "side": c.get("side"), "lev": c.get("leverage"),
                       "vol": c.get("vol"),                       # volume ratio at entry (owner watches this)
                       # margin $ per trade; old rows lack it -> derive from r = net/margin
                       "margin": (round(float(c["margin"]), 2) if c.get("margin")
                                  else (round(abs(float(c.get("net", 0) or 0) / float(c["r"])), 2)
                                        if c.get("r") not in (None, 0) else None)),
                       "entry": round(float(c.get("entry", 0) or 0), 4),
                       "exit": round(float(c.get("exit", 0) or 0), 4),
                       "net": round(float(c.get("net", 0) or 0), 3), "r": c.get("r"),
                       "reason": c.get("reason"), "ts": int(c.get("closed_ts") or 0),
                       "chart": c.get("chart")}
                      for c in sorted(closed_rows, key=lambda x: int(x.get("closed_ts") or 0), reverse=True)[:400]]
        lt["closed_recent"] = [{"sym": c["sym"], "side": c["side"], "r": c["r"], "reason": c["reason"]}
                               for c in lt["feed"][:5]]

        # REAL money chart: cumulative equity over closed trades (seeded at the
        # starting capital), plus a live tip marked-to-market from open positions.
        START = 100.0
        closed_sorted = sorted(closed_rows, key=lambda c: int(c.get("closed_ts") or 0))
        eq = START
        first_ts = int(closed_sorted[0].get("closed_ts") or 0) if closed_sorted else 0
        curve = [{"ts": (first_ts - 60000) if first_ts else 0, "equity": round(START, 4)}]
        for c in closed_sorted:
            eq += float(c.get("net") or 0)
            curve.append({"ts": int(c.get("closed_ts") or 0), "equity": round(eq, 4)})
        realized_eq = round(eq, 4)   # == account equity (both = START + sum(net))
        # Unrealized MTM of open positions at current prices (gross; before exit
        # costs — labelled "unrealized" so it isn't mistaken for booked P&L).
        unreal = 0.0
        for p in open_rows:
            cur = price_map.get(p.get("symbol"))
            if not cur:
                continue
            entry, qty = float(p.get("entry", 0) or 0), float(p.get("qty", 0) or 0)
            g = (cur - entry) * qty if p.get("side") == "LONG" else (entry - cur) * qty
            unreal += g
        lt["realized"] = realized_eq
        lt["unrealized"] = round(unreal, 4)
        lt["live_equity"] = round(realized_eq + unreal, 4)
        lt["equity_curve"] = curve
        lt["start_equity"] = START
    except Exception:
        pass

    # MANUAL test trades (manual_trader.py) — separate account, shown distinctly.
    manual = {"equity": None, "realized": 0.0, "pending": [], "open": [], "closed_recent": []}
    try:
        md = st / "manual_trader"
        ma = json.loads((md / "account.json").read_text())
        manual["equity"] = round(float(ma.get("equity", 100) or 100), 2)
        manual["realized"] = round(float(ma.get("realized", 0) or 0), 2)
        manual["trades"] = int(ma.get("trades", 0) or 0)
    except Exception:
        pass
    try:
        md = st / "manual_trader"
        manual["pending"] = [{"sym": o.get("symbol"), "side": o.get("side"), "lev": o.get("leverage"),
                              "limit": o.get("limit_price"), "sl": o.get("sl"), "tp": o.get("tp"),
                              "cancel_above": o.get("cancel_above"), "note": (o.get("note") or "")[:120]}
                             for o in _load_jsonl(md / "pending.jsonl") if o.get("status") == "pending"]
        m_open = _load_jsonl(md / "positions.jsonl")
        mo = []
        for p in m_open:
            entry = float(p.get("entry", 0) or 0); qty = float(p.get("qty", 0) or 0)
            side = p.get("side"); mark = price_map.get(p.get("symbol"))
            up = round(((mark - entry) if side == "LONG" else (entry - mark)) * qty, 3) if mark else None
            so = p.get("scale_out") or {}
            mo.append({"sym": p.get("symbol"), "side": side, "lev": p.get("leverage"),
                       "entry": round(entry, 4), "mark": round(float(mark), 4) if mark else None,
                       "sl": p.get("sl"), "tp": p.get("tp"), "liq": round(float(p.get("liq_px", 0) or 0), 4),
                       "upnl": up, "chart": p.get("chart"), "chart_kind": "entry",
                       "scale_out": ({"price": so.get("price"), "done": so.get("done")} if so else None),
                       "note": (p.get("note") or "")[:140]})
        manual["open"] = mo
        cl = [c for c in _load_jsonl(md / "closed.jsonl") if c.get("net") is not None]
        manual["closed_recent"] = [{"sym": c.get("symbol"), "side": c.get("side"), "net": c.get("net"),
                                    "r": c.get("r"), "reason": c.get("reason"), "chart": c.get("chart"),
                                    "ts": int(c.get("closed_ts") or 0)}
                                   for c in sorted(cl, key=lambda x: int(x.get("closed_ts") or 0), reverse=True)[:10]]
    except Exception:
        pass

    # Whale flow (public Telegram t.me scraping via whale_flow_observer).
    whale = {"updated_at": None, "status": None, "events": 0, "channels": [], "top": []}
    try:
        wf = json.loads((st / "agent_memory" / "whale_flow_latest.json").read_text())
        whale["updated_at"] = wf.get("updated_at"); whale["status"] = wf.get("status")
        whale["events"] = wf.get("event_count", 0)
        whale["channels"] = wf.get("channels", [])
        top = wf.get("top_symbols") or list((wf.get("by_symbol") or {}).values())
        whale["top"] = [{"sym": r.get("symbol"), "side": r.get("pressure_side"),
                         "score": round(float(r.get("pressure_score", 0) or 0), 3),
                         "events": r.get("event_count"),
                         "long_liq": round(float(r.get("long_liquidation_notional", 0) or 0), 0),
                         "short_liq": round(float(r.get("short_liquidation_notional", 0) or 0), 0)}
                        for r in top if r.get("pressure_side") in ("LONG", "SHORT")][:12]
    except Exception:
        pass

    # Method Lab (autonomous research -> backtest -> curate).
    lab = {"tested": 0, "survived": 0, "killed": 0, "survivors": [], "killed_top": []}
    try:
        mldir = st / "method_lab"
        led = json.loads((mldir / "ledger.json").read_text())
        lab.update({k: led.get(k, 0) for k in ("tested", "survived", "killed", "coins")})
        surv = json.loads((mldir / "survivors.json").read_text())
        lab["survivors"] = [{"id": s.get("id"), "side": s.get("side"), "desc": s.get("desc"),
                             "mean_r": s.get("oos_mean_r"), "win": s.get("oos_win_rate"),
                             "p": s.get("pvalue"), "n": s.get("oos_n"),
                             "net": s.get("oos_total_net_pct")} for s in surv][:8]
        killed = [json.loads(l) for l in (mldir / "killed.jsonl").read_text().splitlines() if l.strip()]
        killed.sort(key=lambda r: (r.get("oos_mean_r") or -9), reverse=True)
        lab["killed_top"] = [{"id": r.get("id"), "mean_r": r.get("oos_mean_r"),
                              "reason": r.get("reason"), "p": r.get("pvalue")} for r in killed][:8]
    except Exception:
        pass

    # Mind: the bot's step-by-step thinking + self-reflection (meta-cognition).
    mind = {"thinking": None, "thinking_ts": None, "reflection": None, "directives": []}
    try:
        th = json.loads((st / "llm_trader" / "thinking_latest.json").read_text(encoding="utf-8"))
        mind["thinking"] = th.get("thinking"); mind["thinking_ts"] = th.get("ts")
    except Exception:
        pass
    try:
        rf = json.loads((st / "llm_trader" / "self_reflection.json").read_text(encoding="utf-8"))
        mind["reflection"] = rf.get("reflection"); mind["directives"] = (rf.get("directives") or [])[:6]
    except Exception:
        pass

    # Signal Follower (paper-trade Telegram alerts, measure per-channel win rate).
    sigf = {"open": 0, "total": 0, "by_kind": [], "by_channel": []}
    try:
        sfdir = st / "signal_follower"
        hb = json.loads((st / "signal_follower_heartbeat.json").read_text())
        sigf["open"] = hb.get("open", 0)
        bd = json.loads((sfdir / "scoreboard.json").read_text())
        sigf["total"] = bd.get("total", 0)
        sigf["by_kind"] = sorted([{"k": k, **v} for k, v in (bd.get("by_kind") or {}).items()],
                                 key=lambda x: (x.get("win_rate") or 0), reverse=True)
        sigf["by_channel"] = sorted([{"k": k, **v} for k, v in (bd.get("by_channel") or {}).items()],
                                    key=lambda x: (x.get("win_rate") or 0), reverse=True)
    except Exception:
        pass

    return {
        "stamped": utc_now(),
        "mode": "PAPER-ONLY · LIVE LOCKED",
        "llm_trader": lt,
        "manual": manual,
        "whale": whale,
        "method_lab": lab,
        "mind": mind,
        "signal_follower": sigf,
        "account": {"equity": equity, "trades": trades, "open": len(fp_open), "realized": realized},
        "forward_paper": {
            "open": [{"sym": p.get("symbol"), "side": p.get("direction"),
                      "entry": round(float(p.get("entry", 0) or 0), 2),
                      "sl": round(float(p.get("sl", 0) or 0), 2),
                      "tp": round(float(p.get("tp", 0) or 0), 2)} for p in fp_open],
            "closed": len(fp_closed), "mean_r": fp_mean, "target": 200,
        },
        "live": {"sym": live_sym, "price": live_price, "series": price_series, "has_pos": has_pos},
        "quotes": quotes,
        "forward_test": {"labels": ft_labels, "target_per_bucket": 200},
        "research": {
            "global_trials": global_trials, "cells_killed": killed_cells,
            "families_distinct": families_distinct, "ledger_rows": len(ledger),
            "holdout": "SEALED · never peeked", "dsr_bar": "saturated",
            "verdict": "NO EDGE in public TA + order-flow — proven",
        },
        "lead": lead,
        "families": fam,
        "guard": {"live": "LOCKED", "lookahead": "enforced", "diagnostics": "isolated"},
    }


def run_once():
    UI.mkdir(parents=True, exist_ok=True)
    data = build()
    OUT.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    return {"equity": data["account"]["equity"], "fwd_open": len(data["forward_paper"]["open"]),
            "ft_labels": data["forward_test"]["labels"], "trials": data["research"]["global_trials"]}


if __name__ == "__main__":
    import argparse, time
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval-seconds", type=float, default=20.0)
    a = ap.parse_args()
    if a.loop:
        stop = UI / "horizon_data.stop"
        while not stop.exists():
            try:
                run_once()
            except Exception:
                pass
            t = time.time() + a.interval_seconds
            while time.time() < t and not stop.exists():
                time.sleep(1)
    else:
        print(json.dumps(run_once(), default=str))
