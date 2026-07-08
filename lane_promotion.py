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

MIN_N = int(os.environ.get("PROMO_MIN_N", "50"))   # thin for 6:1 payoff variance -> 50 (Codex/owner #3)
MAX_PROMOTE = int(os.environ.get("PROMO_MAX", "8"))
ALPHA = 0.05
MIN_EXPECT_R = float(os.environ.get("PROMO_MIN_R", "0.05"))   # absolute expectancy floor (R)
PERSIST = int(os.environ.get("PROMO_PERSIST", "2"))          # consecutive passes to promote (Codex #2)
MIN_STEP = int(os.environ.get("PROMO_MIN_STEP", "15"))       # bughunt: a counted pass needs >=15 NEW
# closed trades since the last counted pass, else a static closed.jsonl re-scored every tick reaches
# PERSIST on ONE window and the winner's-curse protection is nullified.
DEMOTE_FAILS = int(os.environ.get("PROMO_DEMOTE_FAILS", "2"))  # consecutive fails to demote (Codex #6)
PERM_B = 5000                                                # sign-flip permutations (bughunt 2026-07-08:
# 2000 gave a p-floor 1/2001=0.0005 that the Šidák bar could dip below at ~100 tested lanes -> gate
# unsatisfiable; 5000 -> floor 0.0002 keeps the bar resolvable up to ~250 tested lanes)
STATE = ROOT / "state" / "lane_promo_state.json"
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


def _perm_p(xs: list[float]) -> float:
    """Distribution-free one-sided p for H1: mean(xs) > 0 via a SIGN-FLIP permutation
    test (Codex #1: normal/erfc is anti-conservative for n=30 heavy-tailed 6:1 R). Under
    the symmetric null each R keeps or flips its sign with p=0.5; p = fraction of
    permuted means >= observed. Deterministic seed -> reproducible. 1.0 for tiny/neg."""
    n = len(xs)
    if n < MIN_N:
        return 1.0
    obs = sum(xs) / n
    if obs <= 0:
        return 1.0
    import random as _r
    rng = _r.Random(1234567 + n)
    ge = 0
    for _ in range(PERM_B):
        s = 0.0
        for x in xs:
            s += x if rng.random() < 0.5 else -x
        if s / n >= obs:
            ge += 1
    return (ge + 1) / (PERM_B + 1)


def lane_stats() -> tuple[list[dict], float, int]:
    """Per-lane live stats from closed.jsonl. Returns (rows, random_mean_r, random_n)."""
    try:
        summary = json.loads((LANES / "summary.json").read_text(encoding="utf-8")).get("lanes", {})
    except Exception:
        return [], 0.0, 0
    rows, rand_mr, rand_n = [], 0.0, 0
    for k, v in summary.items():
        closed = _load_jsonl(LANES / k / "closed.jsonl")
        rs = [float(c.get("r") or 0) for c in closed]
        nets = [float(c.get("pnl") or 0) for c in closed]
        n = len(rs)
        mean_r = sum(rs) / n if n else 0.0
        se = (math.sqrt(sum((x - mean_r) ** 2 for x in rs) / (n - 1) / n) if n > 1 else 0.0)
        row = {"k": k, "mid": v.get("mid", k), "desc": v.get("desc", ""),
               "n": n, "mean_r": round(mean_r, 4), "lcb": round(mean_r - se, 4),
               "net": round(sum(nets), 3), "equity": v.get("equity"),
               "win": round(sum(1 for x in nets if x > 0) / n * 100, 1) if n else None,
               "p": round(_perm_p(rs), 5), "is_random": k == "L00_random"}
        if row["is_random"]:
            rand_mr, rand_n = mean_r, n
        rows.append(row)
    return rows, rand_mr, rand_n


def evaluate(rows: list[dict], rand_mr: float, rand_n: int) -> list[dict]:
    """Flag each lane pass/fail against the corrected bar. Persistence + winner's-curse
    (LCB ranking) are handled in run_once via the promo-state; this only sets r['pass']."""
    # bughunt 2026-07-08 (SHOWSTOPPER): the funnel promoted NOTHING because sidak(168)=0.000305 was
    # FINER than the sign-flip floor 1/(PERM_B+1)=0.0005 -> `p < sidak` structurally impossible. The
    # fix is the PERM_B bump ALONE (floor now 0.0002 < 0.000305, satisfiable at the FULL family).
    # Re-audit caveat: do NOT shrink the family to "currently-tested" lanes — lanes cross MIN_N one at
    # a time, so a tested-count of 1 gives sidak=ALPHA=0.05 = an ~uncorrected bar for the first lucky
    # lane (a 168-method dredge). Keep the FIXED pre-registered family (all non-random lanes launched).
    n_lanes = max(1, len([r for r in rows if not r["is_random"]]))
    sidak = 1.0 - (1.0 - ALPHA) ** (1.0 / n_lanes)     # family-wise corrected bar (Codex #1)
    if sidak <= 1.0 / (PERM_B + 1):                     # surface the silent-death mode instead of
        print(json.dumps({"promo_warn": "sidak_below_perm_floor", "n_lanes": n_lanes,   # promoting nothing
                          "sidak": round(sidak, 6), "perm_floor": round(1.0 / (PERM_B + 1), 6),
                          "hint": "raise PERM_B or cap lanes"}))
    # Codex #5: only trust the random baseline once it has a real sample; else a fixed floor.
    floor = max(rand_mr if rand_n >= MIN_N else 0.0, MIN_EXPECT_R)
    for r in rows:
        if r["is_random"] or r["mid"] in HAND_ARMED:
            r["pass"] = False
            continue
        r["sidak_bar"] = round(sidak, 6)
        r["floor"] = round(floor, 4)
        r["pass"] = bool(r["n"] >= MIN_N and r["mean_r"] > 0 and (r["net"] or 0) > 0
                         and r["mean_r"] > floor and r["p"] < sidak)
    return rows


def _method_defs() -> dict:
    import lane_farm
    return lane_farm._all_method_defs()


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s), encoding="utf-8")
    os.replace(tmp, STATE)


def apply_promotions(winners: list[dict], stat_by_mid: dict, state: dict, dry: bool = False) -> dict:
    """Merge winners into armed_methods.json. Hand-armed ALWAYS kept; a lane-promoted
    method is demoted only on a HARD fail (net<=0 or mean_r<=0 on fresh data) or after
    DEMOTE_FAILS consecutive failed windows (Codex #6 hysteresis, not one-miss churn).
    De-dupes by id so a winner that shares a hand-armed id is never duplicated (Codex #3)."""
    try:
        cur = json.loads(ARMED.read_text(encoding="utf-8"))
        cur = cur if isinstance(cur, list) else cur.get("methods", [])
    except Exception:
        cur = []
    defs = _method_defs()
    win_ids = {w["mid"] for w in winners}
    kept, demoted = [], []
    for m in cur:
        if m.get("source") == "lane_promoted":
            st = stat_by_mid.get(m["id"]) or {}
            ps = state.get(m["id"], {})
            has_data = (st.get("n", 0) or 0) > 0
            hard_fail = has_data and ((st.get("net", 0) or 0) <= 0 or (st.get("mean_r", 0) or 0) <= 0)
            persist_fail = ps.get("fails", 0) >= DEMOTE_FAILS
            if m["id"] in win_ids or not (hard_fail or persist_fail):
                kept.append(m)                          # still winning OR not failing enough -> keep
            else:
                demoted.append(m["id"])
        else:
            kept.append(m)                              # hand-armed / other -> always keep
    kept_ids = {m["id"] for m in kept}
    promoted = []
    for w in winners:
        if w["mid"] in kept_ids:                        # de-dupe by id (Codex #3)
            continue
        d = defs.get(w["mid"]) or {}
        if not (d.get("when") or d.get("conds")):
            continue
        kept.append({"id": w["mid"], "side": d.get("side", "LONG"),
                     "sl_pct": float(d.get("sl_pct") or 1.5), "tp_pct": float(d.get("tp_pct") or 3.0),
                     "timeout": int(d.get("timeout") or 16), "when": d.get("when") or d.get("conds"),
                     "family": d.get("family"), "desc": (d.get("desc") or "")[:80],
                     "source": "lane_promoted", "lane_n": w["n"], "lane_mean_r": w["mean_r"],
                     "lane_lcb": w.get("lcb"), "lane_p": w["p"]})
        kept_ids.add(w["mid"])
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
    rows, rand_mr, rand_n = lane_stats()
    rows = evaluate(rows, rand_mr, rand_n)
    state = _load_state()
    stat_by_mid = {}
    for r in rows:
        if r["is_random"] or r["mid"] in HAND_ARMED:
            continue
        stat_by_mid[r["mid"]] = r
        ps = state.setdefault(r["mid"], {"passes": 0, "fails": 0, "last_n": 0, "last_pass_n": 0})
        if "last_pass_n" not in ps:                 # migration (re-audit #4): pre-upgrade entries lack
            ps["last_pass_n"] = ps.get("last_n", 0)  # this key; seed to last_n so the first post-upgrade
            # tick can't hand a free +1 pass (n-0>=MIN_STEP always true) to a lane with existing passes>=1
        if r["n"] < ps.get("last_n", 0):            # closed count went backwards = lanes were
            ps["passes"] = 0; ps["fails"] = 0; ps["last_pass_n"] = 0   # reset stale persistence (Codex #6)
        ps["last_n"] = r["n"]
        if r["pass"]:
            # bughunt: only count a pass on a GENUINELY FRESH window (>=MIN_STEP new closes since the
            # last counted pass) — re-scoring a static closed.jsonl every tick must not advance PERSIST.
            if r["n"] - ps.get("last_pass_n", 0) >= MIN_STEP:
                ps["passes"] = ps.get("passes", 0) + 1; ps["last_pass_n"] = r["n"]; ps["fails"] = 0
            # else: passing but no fresh data -> hold the counter (neither promote-progress nor fail)
        else:
            ps["fails"] = ps.get("fails", 0) + 1; ps["passes"] = 0
    # eligible = PERSIST consecutive passes (Codex #2 winner's-curse), ranked by LCB.
    eligible = [r for r in rows if state.get(r["mid"], {}).get("passes", 0) >= PERSIST]
    eligible.sort(key=lambda r: -(r.get("lcb") if r.get("lcb") is not None else -9))
    winners = eligible[:MAX_PROMOTE]
    res = apply_promotions(winners, stat_by_mid, state, dry=dry)
    if not dry:
        _save_state(state)
    res.update({"random_mean_r": round(rand_mr, 4), "random_n": rand_n,
                "evaluated": len(rows), "eligible": len(eligible)})
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
