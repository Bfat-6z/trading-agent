"""Full-scale Method Lab validation (owner: 'test phai scale lon — gan nhu full
san, thoi gian dai hon').

Universe: EVERY Binance USDT-M perp with >= $5M 24h quote volume (below that,
paper fills are fiction). History: up to ~5 months of 15m bars per coin (newer
listings use whatever exists, >= 2000 bars). Every method in seeds+pool is
backtested per coin with the SAME no-lookahead engine, trades tagged in/OOS by a
per-coin temporal 70/30 cut, then scored (bootstrap CI + permutation p) and
BH-FDR corrected across all methods. Writes progress + a final report JSON; the
armed survivors live or die by this too. Paper/offline only.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

os.environ.setdefault("INGEST_DECISION_CANDLES", "0")

import method_lab as ml
import llm_trader_scorecard as ls
from method_seeds import SEED_METHODS

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "state" / "method_lab" / "full_scale_validation.json"
PROG = ROOT / "state" / "method_lab" / "full_scale_progress.json"
DONE = ROOT / "state" / "method_lab" / "full_scale.done"

MIN_QVOL = 50_000_000.0
MONTHS = 5.0
MIN_BARS = 2000
OOS_FRAC = 0.3


def methods_all() -> list[dict]:
    by_id = {m["id"]: m for m in SEED_METHODS}
    pool = ROOT / "state" / "method_lab" / "methods_pool.jsonl"
    if pool.exists():
        for line in pool.read_text(encoding="utf-8").splitlines():
            try:
                m = json.loads(line)
                by_id[m["id"]] = m
            except Exception:
                pass
    return list(by_id.values())


def main() -> None:
    from tradingagents.binance.client import spot_client
    import orderflow_data as of
    client = spot_client()
    t0 = time.time()

    tickers = client.futures_ticker()
    uni = sorted(
        (t["symbol"] for t in tickers
         if t.get("symbol", "").endswith("USDT") and "_" not in t["symbol"]
         and float(t.get("quoteVolume", 0) or 0) >= MIN_QVOL),
    )
    methods = methods_all()
    agg: dict[str, list[dict]] = {m["id"]: [] for m in methods}
    done_coins, skipped = 0, 0

    for idx, sym in enumerate(uni, 1):
        try:
            bars = of.fetch_klines_with_flow(sym, "15m", months=MONTHS,
                                             end_ms=int(time.time() * 1000),
                                             client=client, sleep_between=0.02)
            try:
                fund = of.fetch_funding_series(sym, months=MONTHS,
                                               end_ms=int(time.time() * 1000), client=client)
            except Exception:
                fund = None
            rows = ml.feature_frame(bars, funding=fund)
            if len(rows) < MIN_BARS:
                skipped += 1
                continue
            cut = int(len(rows) * (1 - OOS_FRAC))
            for m in methods:
                for t in ml.backtest_method(rows, m, sym):
                    agg[m["id"]].append({"net": t["net"], "r": t["r"],
                                         "reason": t["reason"], "oos": t["bar"] >= cut})
            done_coins += 1
        except Exception:
            skipped += 1
        if idx % 20 == 0 or idx == len(uni):
            PROG.write_text(json.dumps({
                "scanned": idx, "of": len(uni), "tested": done_coins,
                "skipped": skipped, "minutes": round((time.time() - t0) / 60, 1)}), encoding="utf-8")

    results = []
    for m in methods:
        tr = agg[m["id"]]
        oos = [t for t in tr if t["oos"]]
        row = {"id": m["id"], "side": m.get("side"), "desc": m.get("desc", "")[:90],
               "n_total": len(tr), "oos_n": len(oos)}
        if len(oos) >= 30:
            card = ls.scorecard(oos)
            met = card.get("metrics", {})
            row.update({"oos_mean_r": met.get("mean_r"), "oos_win": met.get("win_rate"),
                        "oos_net_pct": round(sum(t["net"] for t in oos) * 100, 2),
                        "pvalue": card.get("pvalue")})
        else:
            row.update({"oos_mean_r": None, "oos_win": None, "oos_net_pct": None, "pvalue": None})
        results.append(row)

    # BH-FDR q=0.05 across every method tested at scale
    cand = sorted([r for r in results if r["pvalue"] is not None
                   and (r["oos_mean_r"] or 0) > 0 and (r["oos_net_pct"] or 0) > 0],
                  key=lambda r: r["pvalue"])
    mtot = max(1, len(results))
    thr = 0.0
    for i, r in enumerate(cand, 1):
        if r["pvalue"] <= 0.05 * i / mtot:
            thr = max(thr, r["pvalue"])
    for r in results:
        r["survived_full_scale"] = bool(r["pvalue"] is not None and thr > 0
                                        and r["pvalue"] <= thr
                                        and (r["oos_mean_r"] or 0) > 0
                                        and (r["oos_net_pct"] or 0) > 0)

    # persist survivor per-trade OOS net distributions -> mech_sizing inputs
    try:
        dist = {}
        for r in results:
            if r.get("survived_full_scale"):
                arr = [round(t["net"], 6) for t in agg[r["id"]] if t["oos"]]
                dist[r["id"]] = {"net": arr, "n": len(arr), "pvalue": r["pvalue"],
                                 "win": r["oos_win"], "mean": (sum(arr) / len(arr)) if arr else 0}
        (ROOT / "state" / "method_lab" / "survivor_distributions.json").write_text(
            json.dumps(dist), encoding="utf-8")
    except Exception:
        pass

    results.sort(key=lambda r: (not r["survived_full_scale"], -(r["oos_mean_r"] or -9)))
    OUT.write_text(json.dumps({
        "universe_total": len(uni), "coins_tested": done_coins, "coins_skipped": skipped,
        "months": MONTHS, "timeframe": "15m", "min_qvol_usd": MIN_QVOL,
        "methods": len(methods), "bh_threshold": thr,
        "minutes": round((time.time() - t0) / 60, 1),
        "results": results}, indent=1), encoding="utf-8")
    DONE.write_text("done", encoding="utf-8")
    # Second brain: register this sweep's trials (an unrecorded validation silently
    # un-deflates every later Sharpe/p-value in the DSR ledger).
    try:
        import brain
        defs = {m["id"]: m for m in methods}
        n_rec = brain.record_trials(results, defs, source="full_scale",
                                    universe=f"usdtperp>={MIN_QVOL / 1e6:.0f}M",
                                    timeframe="15m", months=MONTHS)
        print(json.dumps({"brain_trials_recorded": n_rec}))
    except Exception as e:
        print(json.dumps({"brain_record_error": repr(e)[:160]}))
    print(json.dumps({"coins_tested": done_coins, "survivors_full_scale":
                      [r["id"] for r in results if r["survived_full_scale"]]}))


if __name__ == "__main__":
    main()
