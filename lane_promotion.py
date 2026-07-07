"""LANE PROMOTION — funnel the genuinely-edged lane methods into the mission armed set.

Owner: 'dồn các pp winrate cao nhất về line main để hoàn thành mission 1k'. Picking the
top of 100 lanes by RAW winrate is the multiple-comparisons trap that has repeatedly
burned this project (falling-knife −$69, S_QUIET_BEAR_COIL overfit, "no edge yet"), and
winrate itself misleads (a 6:1 method wins 30% and is +EV; a tight-SL method wins 70%
and is −EV). Lanes trade FRESH live bars = out-of-sample, so a lane that is significantly
+EV over a real sample AFTER correcting for the ~100 methods raced is a legitimate signal.

Promotion bar (ALL must hold):
  - n >= MIN_N live-closed trades (real sample, not 3-trade luck)
  - net-positive equity AND positive mean-R expectancy
  - mean-R beats the RANDOM control lane's mean-R (must clear the alpha floor)
  - Šidák-corrected one-sided t-test that mean-R > 0: p < 1-(1-0.05)^(1/n_lanes)
Top MAX_PROMOTE survivors are merged into armed_methods.json tagged source=lane_promoted
(the hand-validated armed methods are ALWAYS kept). A promoted method that later turns
net-negative is demoted. The mission's mech_sizing + gap-gate still govern execution.
PAPER-ONLY; live stays LOCKED — this only edits which PROVEN methods the paper bot fires.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LANES = ROOT / "state" / "lanes"
ARMED = ROOT / "state" / "method_lab" / "armed_methods.json"
PROMO_LOG = ROOT / "state" / "lane_promotions.jsonl"
HB = ROOT / "state" / "lane_promotion_heartbeat.json"

MIN_N = int(os.environ.get("PROMO_MIN_N", "30"))
MAX_PROMOTE = int(os.environ.get("PROMO_MAX", "8"))
ALPHA = 0.05
# hand-validated methods that must ALWAYS stay armed regardless of the lane funnel.
HAND_ARMED = {"wr_flush_notknife", "capitulation_long"}


def _load_jsonl(p: Path) -> list[dict]:
    out = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _t_p_one_sided(xs: list[float]) -> float:
    """One-sided p-value for H1: mean(xs) > 0 via a t-approximation (normal tail).
    Returns 1.0 for degenerate samples so they never pass the bar."""
    n = len(xs)
    if n < 3:
        return 1.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return 0.0 if m > 0 else 1.0
    t = m / math.sqrt(var / n)
    # normal-tail approx of the t survival function (fine for n>=30; conservative below)
    z = t
    p = 0.5 * math.erfc(z / math.sqrt(2))     # P(Z > z)
    return min(1.0, max(0.0, p))


def lane_stats() -> tuple[list[dict], float]:
    """Per-lane live stats from closed.jsonl. Returns (rows, random_mean_r)."""
    try:
        summary = json.loads((LANES / "summary.json").read_text(encoding="utf-8")).get("lanes", {})
    except Exception:
        return [], 0.0
    rows, rand_mr = [], 0.0
    for k, v in summary.items():
        closed = _load_jsonl(LANES / k / "closed.jsonl")
        rs = [float(c.get("r") or 0) for c in closed]
        nets = [float(c.get("pnl") or 0) for c in closed]
        n = len(rs)
        mean_r = sum(rs) / n if n else 0.0
        row = {"k": k, "mid": v.get("mid", k), "desc": v.get("desc", ""),
               "n": n, "mean_r": round(mean_r, 4),
               "net": round(sum(nets), 3), "equity": v.get("equity"),
               "win": round(sum(1 for x in nets if x > 0) / n * 100, 1) if n else None,
               "p": round(_t_p_one_sided(rs), 5), "is_random": k == "L00_random"}
        if row["is_random"]:
            rand_mr = mean_r
        rows.append(row)
    return rows, rand_mr


def evaluate(rows: list[dict], rand_mr: float) -> list[dict]:
    n_lanes = max(1, len([r for r in rows if not r["is_random"]]))
    sidak = 1.0 - (1.0 - ALPHA) ** (1.0 / n_lanes)     # multiple-comparisons corrected bar
    winners = []
    for r in rows:
        if r["is_random"] or r["mid"] in HAND_ARMED:
            continue
        r["sidak_bar"] = round(sidak, 6)
        r["pass"] = bool(r["n"] >= MIN_N and r["mean_r"] > 0 and (r["net"] or 0) > 0
                         and r["mean_r"] > rand_mr and r["p"] < sidak)
        if r["pass"]:
            winners.append(r)
    winners.sort(key=lambda r: -r["mean_r"])
    return winners[:MAX_PROMOTE]


def _method_defs() -> dict:
    import lane_farm
    return lane_farm._all_method_defs()


def apply_promotions(winners: list[dict], dry: bool = False) -> dict:
    """Merge winners into armed_methods.json (keep hand-armed + prior valid promotions;
    demote promoted methods that no longer pass)."""
    try:
        cur = json.loads(ARMED.read_text(encoding="utf-8"))
        cur = cur if isinstance(cur, list) else cur.get("methods", [])
    except Exception:
        cur = []
    defs = _method_defs()
    win_ids = {w["mid"] for w in winners}
    # keep: hand-armed always; existing lane_promoted only if still winning
    kept = []
    demoted = []
    for m in cur:
        src = m.get("source")
        if src == "lane_promoted":
            if m["id"] in win_ids:
                kept.append(m)                          # still winning -> keep
            else:
                demoted.append(m["id"])                 # dropped from winners -> demote
        else:
            kept.append(m)                              # hand-armed / other -> always keep
    kept_ids = {m["id"] for m in kept}
    promoted = []
    for w in winners:
        if w["mid"] in kept_ids:
            continue
        d = defs.get(w["mid"]) or {}
        if not (d.get("when") or d.get("conds")):
            continue
        kept.append({"id": w["mid"], "side": d.get("side", "LONG"),
                     "sl_pct": float(d.get("sl_pct") or 1.5), "tp_pct": float(d.get("tp_pct") or 3.0),
                     "timeout": int(d.get("timeout") or 16), "when": d.get("when") or d.get("conds"),
                     "family": d.get("family"), "desc": d.get("desc", "")[:80],
                     "source": "lane_promoted", "lane_n": w["n"], "lane_mean_r": w["mean_r"],
                     "lane_p": w["p"]})
        promoted.append(w["mid"])
    out = {"promoted": promoted, "demoted": demoted, "armed_total": len(kept),
           "winners": [w["mid"] for w in winners]}
    if not dry:
        ARMED.parent.mkdir(parents=True, exist_ok=True)
        tmp = ARMED.with_suffix(".tmp")
        tmp.write_text(json.dumps(kept, indent=1), encoding="utf-8")
        os.replace(tmp, ARMED)
        with PROMO_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**out, "ts": int(time.time() * 1000)}) + "\n")
    return out


def run_once(dry: bool = False) -> dict:
    rows, rand_mr = lane_stats()
    winners = evaluate(rows, rand_mr)
    res = apply_promotions(winners, dry=dry)
    res["random_mean_r"] = round(rand_mr, 4)
    res["evaluated"] = len(rows)
    HB.write_text(json.dumps({"agent": "lane_promotion", "pid": os.getpid(),
                              "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "status": "running", **res}), encoding="utf-8")
    return res


def main():
    ap = argparse.ArgumentParser(description="lane -> mission promotion (paper-only)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry", action="store_true", help="evaluate + print, do not edit armed set")
    ap.add_argument("--interval", type=float, default=1800.0)
    args = ap.parse_args()
    pid_f = ROOT / "state" / "lane_promotion.pid"
    try:
        from forward_test import _pid_alive
        old = int(pid_f.read_text(encoding="utf-8").strip())
        if old and old != os.getpid() and _pid_alive(old):
            print(json.dumps({"exit": "another lane_promotion alive"})); return
    except Exception:
        pass
    pid_f.write_text(str(os.getpid()), encoding="utf-8")
    while True:
        try:
            print(json.dumps(run_once(dry=args.dry)))
        except Exception as e:
            print(json.dumps({"error": repr(e)[:160]}))
        if args.once:
            break
        time.sleep(max(300.0, args.interval))


if __name__ == "__main__":
    main()
