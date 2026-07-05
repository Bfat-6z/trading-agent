"""Per-timeframe Method Lab validation (owner: 'm duoc danh 15m/1h/4h nhung phai
check ky'). Same rigorous engine as full_scale_validation but parametrized by the
TRADING timeframe, so each method is validated on the bars it would actually fire on.

Universe: liquid Binance USDT-M perps (>= $50M 24h quote vol — the anti-falling-knife
floor). History scales with TF so every TF gets a real sample. Per-coin temporal 70/30
OOS cut, bootstrap CI + permutation p, BH-FDR across all methods. Writes results +
survivor per-trade OOS distributions, both tagged by TF. Paper/offline only.

Usage: python tf_validation.py --tf 1h   (or 4h, 15m)
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault("INGEST_DECISION_CANDLES", "0")

import method_lab as ml
import llm_trader_scorecard as ls
from method_seeds import SEED_METHODS

ROOT = Path(__file__).resolve().parent
LAB = ROOT / "state" / "method_lab"

MIN_QVOL = 50_000_000.0
OOS_FRAC = 0.3
# history + min-sample per TF (higher TF -> fewer bars/day -> pull more months, lower floor)
TF_CFG = {
    "15m": {"months": 5.0, "min_bars": 2000},
    "1h":  {"months": 10.0, "min_bars": 1200},
    "4h":  {"months": 16.0, "min_bars": 400},
}


def methods_all() -> list[dict]:
    by_id = {m["id"]: m for m in SEED_METHODS}
    pool = LAB / "methods_pool.jsonl"
    if pool.exists():
        for line in pool.read_text(encoding="utf-8").splitlines():
            try:
                m = json.loads(line)
                by_id[m["id"]] = m
            except Exception:
                pass
    return list(by_id.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", required=True, choices=["15m", "1h", "4h"])
    args = ap.parse_args()
    tf = args.tf
    cfg = TF_CFG[tf]
    months, min_bars = cfg["months"], cfg["min_bars"]

    out_f = LAB / f"tf_validation_{tf}.json"
    prog_f = LAB / f"tf_progress_{tf}.json"
    done_f = LAB / f"tf_{tf}.done"
    dist_f = LAB / f"survivor_distributions_{tf}.json"

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
            bars = of.fetch_klines_with_flow(sym, tf, months=months,
                                             end_ms=int(time.time() * 1000),
                                             client=client, sleep_between=0.02)
            try:
                fund = of.fetch_funding_series(sym, months=months,
                                               end_ms=int(time.time() * 1000), client=client)
            except Exception:
                fund = None
            rows = ml.feature_frame(bars, funding=fund)
            if len(rows) < min_bars:
                skipped += 1
                continue
            cut = int(len(rows) * (1 - OOS_FRAC))
            for m in methods:
                for t in ml.backtest_method(rows, m, sym):
                    agg[m["id"]].append({"net": t["net"], "r": t["r"], "reason": t["reason"],
                                         "oos": t["bar"] >= cut})
            done_coins += 1
        except Exception:
            skipped += 1
        if idx % 15 == 0 or idx == len(uni):
            prog_f.write_text(json.dumps({"tf": tf, "scanned": idx, "of": len(uni),
                "tested": done_coins, "skipped": skipped,
                "minutes": round((time.time() - t0) / 60, 1)}), encoding="utf-8")

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

    # BH-FDR q=0.05 across every method at this TF
    cand = sorted([r for r in results if r["pvalue"] is not None
                   and (r["oos_mean_r"] or 0) > 0 and (r["oos_net_pct"] or 0) > 0],
                  key=lambda r: r["pvalue"])
    mtot = max(1, len(results))
    thr = 0.0
    for i, r in enumerate(cand, 1):
        if r["pvalue"] <= 0.05 * i / mtot:
            thr = max(thr, r["pvalue"])
    for r in results:
        r["survived"] = bool(r["pvalue"] is not None and thr > 0 and r["pvalue"] <= thr
                             and (r["oos_mean_r"] or 0) > 0 and (r["oos_net_pct"] or 0) > 0)
        # also flag individually-strong (small universe -> BH can be too strict):
        r["strong_solo"] = bool(r["pvalue"] is not None and r["pvalue"] < 0.005
                                and (r["oos_mean_r"] or 0) > 0 and (r["oos_net_pct"] or 0) > 0
                                and (r["oos_n"] or 0) >= 120)

    # per-trade OOS distributions for anything armable (survived OR strong_solo)
    dist = {}
    for r in results:
        if r["survived"] or r["strong_solo"]:
            arr = [round(t["net"], 6) for t in agg[r["id"]] if t["oos"]]
            dist[r["id"]] = {"net": arr, "n": len(arr), "pvalue": r["pvalue"],
                             "win": r["oos_win"], "mean": (sum(arr) / len(arr)) if arr else 0,
                             "tf": tf}
    dist_f.write_text(json.dumps(dist), encoding="utf-8")

    results.sort(key=lambda r: (not (r["survived"] or r["strong_solo"]), -(r["oos_mean_r"] or -9)))
    out_f.write_text(json.dumps({"tf": tf, "coins_tested": done_coins, "coins_skipped": skipped,
        "months": months, "min_bars": min_bars, "min_qvol_usd": MIN_QVOL,
        "methods": len(methods), "bh_threshold": thr,
        "minutes": round((time.time() - t0) / 60, 1), "results": results}, indent=1),
        encoding="utf-8")
    done_f.write_text("done", encoding="utf-8")
    # Second brain: every future TF run must register its trials too (an unrecorded
    # validation silently un-deflates every later Sharpe/p-value).
    try:
        import brain
        defs = {m["id"]: m for m in methods}
        n_rec = brain.record_trials(results, defs, source=f"tf_validation_{tf}",
                                    universe=f"usdtperp>={MIN_QVOL / 1e6:.0f}M",
                                    timeframe=tf, months=months)
        print(json.dumps({"brain_trials_recorded": n_rec}))
    except Exception as e:
        print(json.dumps({"brain_record_error": repr(e)[:160]}))
    print(json.dumps({"tf": tf, "coins_tested": done_coins,
        "survived_bh": [r["id"] for r in results if r["survived"]],
        "strong_solo": [r["id"] for r in results if r["strong_solo"] and not r["survived"]]}))


if __name__ == "__main__":
    main()
