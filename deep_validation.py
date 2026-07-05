"""Deep, TWO-SIDED validation that attacks FALSE NEGATIVES (owner: 'lam ca 3 cai
sua sau o tang validation di'). The strict single-shot test rejected good methods
because their exits/timeout were fixed and the universe was small. This fixes all
three, WITHOUT inflating false positives:

  Fix 1+2  WALK-FORWARD (SL, TP, TIMEOUT) grid — for each method the exit params are
           chosen ONLY on the in-sample TRAIN slice, then the single chosen combo is
           scored on the untouched OOS slice. A lucky train fit cannot survive OOS,
           so false positives stay controlled (OOS + BH-FDR), while a good entry is
           no longer killed by a badly-guessed stop/target or a too-short timeout.
  Fix 3    Wider universe ($15M vs $50M) -> ~2x more coins -> more OOS trades ->
           real statistical power, so genuine edges can actually reach significance.

Entry fires are computed ONCE per (method, coin); only the cheap exit walk repeats
per grid combo, so the grid is affordable. Same no-lookahead engine, per-coin 70/30
temporal cut, bootstrap+permutation p, BH-FDR + strong-solo. Paper/offline only.
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
LAB = ROOT / "state" / "method_lab"
OUT = LAB / "deep_validation.json"
PROG = LAB / "deep_progress.json"
DONE = LAB / "deep.done"
DIST = LAB / "deep_distributions.json"

MIN_QVOL = float(os.environ.get("DEEP_MIN_QVOL", "15000000"))   # Fix 3: wider universe
MONTHS = 5.0
MIN_BARS = 2000
OOS_FRAC = 0.3
FEE_RT = ml.FEE_RT

# Fix 1+2: exit-parameter grid (train-selected, OOS-validated)
SL_GRID = [1.0, 1.5, 2.5]
TP_GRID = [2.5, 4.0, 6.0]
TIMEOUT_GRID = [16, 48]                 # 4h, 12h — let slow/trend methods breathe
MIN_TRAIN_TRADES = 20                   # need enough train fires to pick params honestly


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
    only = os.environ.get("DEEP_ONLY_IDS", "").strip()
    if only:                              # targeted re-check of specific methods
        want = {s.strip() for s in only.split(",") if s.strip()}
        return [m for m in by_id.values() if m["id"] in want]
    return list(by_id.values())


def _combos() -> list[tuple[float, float, int]]:
    out = []
    for sl in SL_GRID:
        for tp in TP_GRID:
            if tp < 1.3 * sl:            # keep a sane reward:risk, skip degenerate combos
                continue
            for to in TIMEOUT_GRID:
                out.append((sl, tp, to))
    return out


COMBOS = _combos()


def fire_bars(rows: list[dict], method: dict) -> list[int]:
    """Bars (>= warmup) where the method's ENTRY condition holds. Independent of
    exit params, so computed once per (method, coin)."""
    return [i for i in range(200, len(rows) - 1) if ml.method_fires(rows[i], method)]


def trades_for_combo(rows: list[dict], fires: list[int], side: str,
                     sl_pct: float, tp_pct: float, timeout: int,
                     lo: int, hi: int) -> list[dict]:
    """Replay the SAME no-lookahead fill as method_lab.backtest_method (SL before TP,
    pessimistic, timeout-at-close, no overlapping positions) for one exit combo, over
    fires whose entry bar is in [lo, hi)."""
    slf, tpf = sl_pct / 100.0, tp_pct / 100.0
    n = len(rows)
    out = []
    last_exit = -1
    for i in fires:
        if i < lo or i >= hi or i <= last_exit:
            continue
        entry = rows[i]["close"]
        if side == "LONG":
            sl, tp = entry * (1 - slf), entry * (1 + tpf)
        else:
            sl, tp = entry * (1 + slf), entry * (1 - tpf)
        exit_px = reason = None
        j = i
        for j in range(i + 1, min(i + 1 + timeout, n)):
            hib, lowb = rows[j]["high"], rows[j]["low"]
            if side == "LONG":
                if lowb <= sl:
                    exit_px, reason = sl, "sl"; break
                if hib >= tp:
                    exit_px, reason = tp, "tp"; break
            else:
                if hib >= sl:
                    exit_px, reason = sl, "sl"; break
                if lowb <= tp:
                    exit_px, reason = tp, "tp"; break
        if exit_px is None:
            j = min(i + timeout, n - 1)
            exit_px, reason = rows[j]["close"], "timeout"
        gross = (exit_px / entry - 1) if side == "LONG" else (entry - exit_px) / entry
        net = gross - FEE_RT
        out.append({"net": net, "r": net / slf, "reason": reason})
        last_exit = j
    return out


def main() -> None:
    from tradingagents.binance.client import spot_client
    import orderflow_data as of
    client = spot_client()
    t0 = time.time()

    tickers = client.futures_ticker()
    uni = sorted(t["symbol"] for t in tickers
                 if t.get("symbol", "").endswith("USDT") and "_" not in t["symbol"]
                 and float(t.get("quoteVolume", 0) or 0) >= MIN_QVOL)
    methods = methods_all()

    # per method: accumulate OOS trades under the TRAIN-selected combo, across coins
    oos_agg: dict[str, list[dict]] = {m["id"]: [] for m in methods}
    # #2 LOCKBOX (gpt-5.5 review): the most-recent slice is NEVER used to select params
    # or admit a method — a final untouched holdout that catches regime fragility
    # ("worked once, dead now") and selection/OOS contamination. per-coin, same TF.
    lockbox_agg: dict[str, list[dict]] = {m["id"]: [] for m in methods}
    # track chosen combo per method per coin (report the modal choice)
    combo_votes: dict[str, dict[tuple, int]] = {m["id"]: {} for m in methods}
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
            n = len(rows)
            train_end = int(n * 0.55)          # params chosen here
            oos_end = int(n * 0.80)            # OOS-select scored here (train_end..oos_end)
            # lockbox = [oos_end:n] — never touched by selection
            for m in methods:
                side = m.get("side", "LONG")
                fires = fire_bars(rows, m)
                if not fires:
                    continue
                # Fix 1+2: pick (sl,tp,timeout) on TRAIN only
                best, best_mr = None, None
                for (sl, tp, to) in COMBOS:
                    tr = trades_for_combo(rows, fires, side, sl, tp, to, 200, train_end)
                    if len(tr) < MIN_TRAIN_TRADES:
                        continue
                    mr = sum(t["r"] for t in tr) / len(tr)
                    if best_mr is None or mr > best_mr:
                        best_mr, best = mr, (sl, tp, to)
                if best is None:            # too few train trades to choose honestly
                    continue
                combo_votes[m["id"]][best] = combo_votes[m["id"]].get(best, 0) + 1
                # score the CHOSEN combo on the OOS-select slice AND the untouched lockbox
                oos_agg[m["id"]].extend(
                    trades_for_combo(rows, fires, side, best[0], best[1], best[2], train_end, oos_end))
                lockbox_agg[m["id"]].extend(
                    trades_for_combo(rows, fires, side, best[0], best[1], best[2], oos_end, n))
            done_coins += 1
        except Exception:
            skipped += 1
        if idx % 15 == 0 or idx == len(uni):
            PROG.write_text(json.dumps({"scanned": idx, "of": len(uni), "tested": done_coins,
                "skipped": skipped, "combos": len(COMBOS),
                "minutes": round((time.time() - t0) / 60, 1)}), encoding="utf-8")

    results = []
    for m in methods:
        oos = oos_agg[m["id"]]
        votes = combo_votes[m["id"]]
        modal = max(votes.items(), key=lambda kv: kv[1])[0] if votes else None
        row = {"id": m["id"], "side": m.get("side"), "desc": m.get("desc", "")[:80],
               "oos_n": len(oos),
               "opt_sl": modal[0] if modal else None, "opt_tp": modal[1] if modal else None,
               "opt_timeout": modal[2] if modal else None}
        if len(oos) >= 30:
            card = ls.scorecard(oos)
            met = card.get("metrics", {})
            row.update({"oos_mean_r": met.get("mean_r"), "oos_win": met.get("win_rate"),
                        "oos_net_pct": round(sum(t["net"] for t in oos) * 100, 2),
                        "pvalue": card.get("pvalue")})
        else:
            row.update({"oos_mean_r": None, "oos_win": None, "oos_net_pct": None, "pvalue": None})
        # #2 LOCKBOX: never used to select — the honest final holdout
        lb = lockbox_agg[m["id"]]
        row["lockbox_n"] = len(lb)
        if len(lb) >= 30:
            lc = ls.scorecard(lb)
            row["lockbox_mean_r"] = lc.get("metrics", {}).get("mean_r")
            row["lockbox_net_pct"] = round(sum(t["net"] for t in lb) * 100, 2)
            row["lockbox_pvalue"] = lc.get("pvalue")
        else:
            row["lockbox_mean_r"] = row["lockbox_net_pct"] = row["lockbox_pvalue"] = None
        # a method is lockbox_held only if it stays net-positive with positive mean on
        # data that never influenced its selection (regime-robustness, not luck)
        row["lockbox_held"] = bool((row["lockbox_mean_r"] or 0) > 0 and (row["lockbox_net_pct"] or 0) > 0
                                   and (row["lockbox_n"] or 0) >= 30)
        results.append(row)

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
        r["strong_solo"] = bool(r["pvalue"] is not None and r["pvalue"] < 0.005
                                and (r["oos_mean_r"] or 0) > 0 and (r["oos_net_pct"] or 0) > 0
                                and (r["oos_n"] or 0) >= 120)

    # ROBUST = LOCKBOX-PRIMARY. The untouched holdout is the trustworthy judge; the
    # 25% OOS-select window is small + noisy so its p is unreliable (good methods score
    # weak there by chance). So require: lockbox significant (p<0.05, n>=100) + positive,
    # and merely DIRECTIONALLY positive on OOS-select. This is the only armable tier —
    # S_QUIET_BEAR_COIL passed BH on OOS-select but FAILED the lockbox (overfit).
    for r in results:
        r["robust"] = bool(r.get("lockbox_held")
                           and (r.get("lockbox_pvalue") is not None and r["lockbox_pvalue"] < 0.05)
                           and (r.get("lockbox_n") or 0) >= 100
                           and (r.get("lockbox_mean_r") or 0) > 0
                           and (r["oos_mean_r"] or 0) > 0)

    dist = {}
    for r in results:
        if r["robust"] or r["survived"] or r["strong_solo"]:
            arr = [round(t["net"], 6) for t in oos_agg[r["id"]]]
            dist[r["id"]] = {"net": arr, "n": len(arr), "pvalue": r["pvalue"], "win": r["oos_win"],
                             "mean": (sum(arr) / len(arr)) if arr else 0, "lockbox_held": r.get("lockbox_held"),
                             "opt": {"sl": r["opt_sl"], "tp": r["opt_tp"], "timeout": r["opt_timeout"]}}
    DIST.write_text(json.dumps(dist), encoding="utf-8")

    results.sort(key=lambda r: (not r["robust"], not (r["survived"] or r["strong_solo"]), -(r["oos_mean_r"] or -9)))
    OUT.write_text(json.dumps({"universe_total": len(uni), "coins_tested": done_coins,
        "coins_skipped": skipped, "min_qvol_usd": MIN_QVOL, "grid_combos": len(COMBOS),
        "sl_grid": SL_GRID, "tp_grid": TP_GRID, "timeout_grid": TIMEOUT_GRID,
        "methods": len(methods), "bh_threshold": thr,
        "minutes": round((time.time() - t0) / 60, 1), "results": results}, indent=1), encoding="utf-8")
    DONE.write_text("done", encoding="utf-8")
    print(json.dumps({"coins_tested": done_coins, "grid": len(COMBOS),
        "ROBUST_lockbox": [r["id"] for r in results if r["robust"]],
        "survived_bh": [r["id"] for r in results if r["survived"]],
        "strong_solo": [r["id"] for r in results if r["strong_solo"] and not r["survived"]]}))


if __name__ == "__main__":
    main()
