"""LANE PROMOTION — funnel the genuinely-edged lane methods into the mission armed set.

Owner: 'dồn các pp winrate cao nhất về line main để hoàn thành mission 1k'. Picking the
top of 100 lanes by RAW winrate is the multiple-comparisons trap that has repeatedly
burned this project (falling-knife −$69, S_QUIET_BEAR_COIL overfit, "no edge yet"), and
winrate itself misleads (a 6:1 method wins 30% and is +EV; a tight-SL method wins 70%
and is −EV). Lanes trade FRESH live bars = out-of-sample, so a lane that is significantly
+EV over a real sample AFTER correcting for the ~100 methods raced is a legitimate signal.

TWO-STAGE GATE (v2 2026-07-15 — owner-approved redesign after 399 cycles / 0 promotions):
the v1 bar (Šidák over the FULL ~172-lane family × sign-flip p on lifetime data, n>=50)
demanded a sustained ~+0.74R/trade — physically unreachable, so the funnel was inert and
the mission never learned anything from 220 lanes (the loop-forensic's core finding).
v2 splits selection from validation, walk-forward style (learned from the Rust bot):

  STAGE A — SCREEN (history, run ONCE per registration round):
    scan ALL lane ledgers (state/lanes/*/closed.jsonl, incl. rotated-out generations),
    keep n >= SCREEN_MIN_N, mean_r > max(random+0.10, MIN_EXPECT_R), LCB > 0;
    top SCREEN_MAX by LCB -> written to confirm_set.json with a registration timestamp,
    their method defs PINNED for lane_farm (pool rotation can't evict them), their cull
    keys removed so they trade again.
  STAGE B — CONFIRM (FRESH closes only, closed_ts_ms > registered_ms):
    fresh n >= CONFIRM_MIN_N, fresh mean_r >= CONFIRM_MIN_R, PF >= CONFIRM_MIN_PF,
    fresh mean_r >= random_fresh + CONFIRM_BEAT_RANDOM, sign-flip p_fresh < CONFIRM_ALPHA,
    over PERSIST consecutive windows each with >= MIN_STEP new closes.
    Rigor comes from PRE-REGISTRATION + fresh-data replication x persistence — not from
    an unpayable family-wise bar. The set is FROZEN between registrations (no per-cycle
    re-screening = no dredging); re-registration is an explicit --reregister run.

Top MAX_PROMOTE survivors are merged into armed_methods.json tagged source=lane_promoted
(the hand-validated armed methods are ALWAYS kept). A promoted method that later turns
net-negative is demoted (unchanged hysteresis). The mission's mech_sizing + gap-gate
still govern execution.
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

# --- two-stage gate v2 (2026-07-15) ---
CONFIRM_SET = LANES / "confirm_set.json"     # pre-registered candidates + registration ts
PINNED = LANES / "pinned_lanes.json"         # lane_farm force-include: {lane_key: method_def}
CULL_FILE = LANES / "closed_lanes.json"      # owner-cull registry (keys removed on registration)
SCREEN_MIN_N = int(os.environ.get("PROMO_SCREEN_MIN_N", "25"))
SCREEN_MAX = int(os.environ.get("PROMO_SCREEN_MAX", "10"))
SCREEN_BEAT_RANDOM = 0.10                    # screen: mean_r > random + this (history)
CONFIRM_MIN_N = int(os.environ.get("PROMO_CONFIRM_MIN_N", "25"))
CONFIRM_MIN_R = 0.10                         # fresh expectancy floor (R)
CONFIRM_MIN_PF = 1.3                         # fresh profit factor floor
CONFIRM_BEAT_RANDOM = 0.30                   # fresh mean_r must beat random fresh by this
CONFIRM_ALPHA = 0.05                         # per-window sign-flip p on FRESH trades only;
# family-wise safety = pre-registration (set frozen, K<=SCREEN_MAX) x PERSIST independent
# fresh windows x effect-size floors — NOT a Šidák/172 bar no real edge can pay.


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


def _perm_p(xs: list[float], min_n: int | None = None) -> float:
    """Distribution-free one-sided p for H1: mean(xs) > 0 via a SIGN-FLIP permutation
    test (Codex #1: normal/erfc is anti-conservative for n=30 heavy-tailed 6:1 R). Under
    the symmetric null each R keeps or flips its sign with p=0.5; p = fraction of
    permuted means >= observed. Deterministic seed -> reproducible. 1.0 for tiny/neg.
    min_n defaults to the v1 MIN_N=50 (callers on lifetime stats); the v2 confirm stage
    passes CONFIRM_MIN_N=25 — without this the guard silently returned 1.0 for every
    25<=n<50 fresh window and stage B could never pass."""
    n = len(xs)
    if n < (MIN_N if min_n is None else min_n):
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
    """LEGACY v1 (unused in the v2 promotion path — kept only for ad-hoc inspection;
    the v2 gate lives in screen_and_register + run_once).
    Per-lane live stats from closed.jsonl. Returns (rows, random_mean_r, random_n)."""
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


def _lane_rows(k: str) -> list[dict]:
    return _load_jsonl(LANES / k / "closed.jsonl")


def _f(v) -> float:
    """Malformed-row-proof float (Codex MINOR: a nonnumeric r/pnl in one legacy row must
    not abort the whole candidate evaluation)."""
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _ts_ms(row: dict) -> int:
    try:
        return int(row.get("closed_ts_ms") or 0)
    except Exception:
        return 0


def _stats_of(rows: list[dict]) -> dict:
    """n / mean_r / lcb / net / pf / p over a set of closed rows (pure)."""
    rs = [_f(c.get("r")) for c in rows]
    nets = [_f(c.get("pnl")) if c.get("pnl") is not None else _f(c.get("net")) for c in rows]
    n = len(rs)
    mean_r = sum(rs) / n if n else 0.0
    se = (math.sqrt(sum((x - mean_r) ** 2 for x in rs) / (n - 1) / n) if n > 1 else 0.0)
    gw = sum(x for x in nets if x > 0)
    gl = abs(sum(x for x in nets if x < 0))
    pf = (gw / gl) if gl > 0 else (99.0 if gw > 0 else 0.0)
    return {"n": n, "mean_r": round(mean_r, 4), "lcb": round(mean_r - se, 4),
            "net": round(sum(nets), 3), "pf": round(pf, 3),
            "p": round(_perm_p(rs, min_n=CONFIRM_MIN_N), 5)}   # v2 windows are 25+, not 50+


def _fresh_stats(k: str, since_ms: int) -> dict:
    """Stage-B input: ONLY closes after the registration timestamp (walk-forward OOS)."""
    return _stats_of([c for c in _lane_rows(k) if _ts_ms(c) > int(since_ms)])


def _def_for(mid: str) -> dict | None:
    """Method def for a candidate: live pool/armed first, then brain.db trials
    (dsl_canonical keeps the FULL def) — pool rotation can't orphan a candidate."""
    d = _method_defs().get(mid) or {}
    if d.get("when") or d.get("conds"):
        return d
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{ROOT / 'state' / 'memory' / 'brain.db'}?mode=ro",
                              uri=True, timeout=10)   # ro: a READ path must never create
        try:                                          # a stub db file (audit#3 LOW)
            row = con.execute("SELECT dsl_canonical FROM trials WHERE method_id=? AND "
                              "dsl_canonical IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                              (mid,)).fetchone()
        finally:
            con.close()
        if row and row[0]:
            d = json.loads(row[0])
            if d.get("when") or d.get("conds"):
                return d
    except Exception:
        pass
    return None


def _load_confirm_set() -> dict:
    """Missing file -> {} (first run, may register). Corrupt/unreadable file -> sentinel:
    fail-CLOSED (Opus I4 — silently re-registering on corruption would churn registered_ms
    every tick, repeatedly un-cull, and keep the fresh window forever empty)."""
    if not CONFIRM_SET.exists():
        return {}
    try:
        d = json.loads(CONFIRM_SET.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {"__corrupt__": True}
    except Exception:
        return {"__corrupt__": True}


def _safe_key(mid: str) -> str:
    """MUST mirror lane_farm._safe_key — lane dir names are this transform of the mid."""
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(mid))[:48]


def _ensure_candidates_active(cs: dict) -> None:
    """Self-repair every tick (Opus I3): registration writes three files; a crash between
    them — or a later manual cull sweep — leaves a candidate culled/unpinned and silently
    'accumulating' forever (lane_farm's cull check runs BEFORE its pin loop). Re-assert
    both invariants idempotently: every candidate with a def is pinned + not culled.
    ALSO covers armed lane_promoted methods (audit#2: they are demote-monitored via their
    lane's fresh closes — if their lane stops trading, they become undemotable zombies)."""
    cands = list(cs.get("candidates") or [])
    try:
        _a = json.loads(ARMED.read_text(encoding="utf-8"))
        _a = _a if isinstance(_a, list) else _a.get("methods", [])
        have = {c["k"] for c in cands}
        cands += [{"k": _safe_key(m["id"]), "mid": m["id"]}
                  for m in _a if m.get("source") == "lane_promoted"
                  and _safe_key(m["id"]) not in have]
    except Exception:
        pass
    if not cands:
        return
    try:
        pinned = json.loads(PINNED.read_text(encoding="utf-8")) if PINNED.exists() else {}
        pinned = pinned if isinstance(pinned, dict) else {}
    except Exception:
        pinned = {}
    changed = False
    for c in cands:
        if c["k"] not in pinned:
            d = _def_for(c["mid"])
            if d is not None:
                pinned[c["k"]] = d
                changed = True
    if changed:
        tmp = PINNED.with_suffix(".tmp")
        tmp.write_text(json.dumps(pinned, indent=1), encoding="utf-8")
        os.replace(tmp, PINNED)
    try:
        cull = json.loads(CULL_FILE.read_text(encoding="utf-8"))
        if not isinstance(cull, dict):
            return
    except Exception:
        return
    removed = [c["k"] for c in cands if cull.pop(c["k"], None) is not None]
    if removed:
        tmp = CULL_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cull, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CULL_FILE)
        with PROMO_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "candidate_uncull_repair", "keys": removed,
                                 "ts": int(time.time() * 1000)}) + "\n")


def screen_and_register(dry: bool = False) -> dict:
    """STAGE A: scan ALL lane ledgers (incl. rotated-out generations the summary no longer
    sees), pick top SCREEN_MAX by LCB, register them (frozen set + timestamp), pin their
    defs for lane_farm and un-cull them so they trade fresh data. Runs only when no
    confirm set exists (or an explicit --reregister)."""
    rand = _stats_of(_lane_rows("L00_random"))
    floor = max(rand["mean_r"] + SCREEN_BEAT_RANDOM if rand["n"] >= 15 else 0.0, MIN_EXPECT_R)
    try:                                             # true method id per lane key: dir name is
        _summ = json.loads((LANES / "summary.json").read_text(encoding="utf-8")).get("lanes", {})
    except Exception:                                # _safe_key(mid) — sanitized/truncated ids
        _summ = {}                                   # would otherwise be looked up + promoted
    cands = []                                       # under the WRONG id (Codex I-mid)
    for d in sorted(LANES.iterdir()) if LANES.exists() else []:
        if not d.is_dir() or d.name == "L00_random":
            continue
        st = _stats_of(_lane_rows(d.name))
        mid = (_summ.get(d.name) or {}).get("mid") or d.name
        if st["n"] >= SCREEN_MIN_N and st["mean_r"] > floor and st["lcb"] > 0 \
                and d.name not in HAND_ARMED and mid not in HAND_ARMED:
            cands.append({"k": d.name, "mid": mid, "screen": st})
    cands.sort(key=lambda c: -c["screen"]["lcb"])
    cands = cands[:SCREEN_MAX]
    now_ms = int(time.time() * 1000)
    pinned, no_def = {}, []
    for c in cands:
        d = _def_for(c["mid"])
        if d is not None:
            pinned[c["k"]] = d
        else:
            no_def.append(c["mid"])   # candidate still confirmable if its lane is alive in configs
    cs = {"registered_ms": now_ms, "screen_floor": round(floor, 4),
          "random_screen": rand, "candidates": cands, "no_def": no_def, "version": 2}
    if not dry:
        CONFIRM_SET.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIRM_SET.with_suffix(".tmp")
        tmp.write_text(json.dumps(cs, indent=1), encoding="utf-8")
        os.replace(tmp, CONFIRM_SET)
        tmp = PINNED.with_suffix(".tmp")                     # defs INLINE -> rotation-proof
        tmp.write_text(json.dumps(pinned, indent=1), encoding="utf-8")
        os.replace(tmp, PINNED)
        try:                                                 # un-cull candidates (reversal is owner-visible
            cull = json.loads(CULL_FILE.read_text(encoding="utf-8"))   # in the promo log below)
            removed = [c["k"] for c in cands if cull.pop(c["k"], None) is not None]
            if removed:
                tmp = CULL_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(cull, indent=1, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, CULL_FILE)
        except Exception as _ce:
            removed = []
            with PROMO_LOG.open("a", encoding="utf-8") as fh:   # Codex MINOR: a swallowed cull
                fh.write(json.dumps({"event": "cull_update_failed",  # failure must be distinguishable
                                     "error": repr(_ce)[:120],       # from "nothing to un-cull"
                                     "ts": now_ms}) + "\n")
            # (the _ensure_candidates_active repair pass retries this every tick)
        with PROMO_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "confirm_set_registered", "ts": now_ms,
                                 "candidates": [c["k"] for c in cands], "unculled": removed,
                                 "no_def": no_def, "floor": round(floor, 4)}) + "\n")
    return cs


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
    except FileNotFoundError:
        cur = []
    except Exception:
        # fail-CLOSED (Codex CRITICAL): an EXISTING-but-unreadable armed file must never be
        # replaced by a freshly-built partial set — that would silently disarm the hand-armed
        # methods the mission fires ("ALWAYS kept" guarantee). Alert + skip this tick.
        if not dry:
            with PROMO_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"event": "armed_file_corrupt_skip",
                                     "ts": int(time.time() * 1000)}) + "\n")
        return {"promoted": [], "demoted": [], "armed_total": None,
                "winners": [w["mid"] for w in winners], "error": "armed_file_corrupt"}
    defs = _method_defs()
    win_ids = {w["mid"] for w in winners}
    kept, demoted = [], []
    for m in cur:
        if m.get("source") == "lane_promoted":
            st = stat_by_mid.get(m["id"]) or {}
            ps = state.get(m["id"], {})
            has_data = (st.get("n", 0) or 0) >= 5   # audit#2: n>0 hard-demoted on a SINGLE
                                                     # losing fresh close after every
                                                     # re-registration — coin-flip demotion
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
        d = _def_for(w["mid"]) or {}    # pool first, brain.db fallback — a resurrected
                                        # candidate must not be silently unpromotable
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
    # STAGE A: register once (set is FROZEN between explicit registrations — no dredging).
    cs = _load_confirm_set()
    if cs.get("__corrupt__"):
        out = {"error": "confirm_set_corrupt", "action": "fix file or --reregister (fail-closed)"}
        if not dry:
            _write_hb({"status": "error", **out})
        return out
    if not cs.get("candidates"):
        # Opus I2: an EMPTY registered round must not re-screen every tick (54s compute +
        # registered_ms churn + pinned/cull overwrites). Retry at most daily.
        age_ms = int(time.time() * 1000) - int(cs.get("registered_ms") or 0)
        if not cs.get("registered_ms") or age_ms > 86_400_000:
            cs = screen_and_register(dry=dry)
    if not dry:
        _ensure_candidates_active(cs)              # Opus I3: crash/cull-sweep self-repair
    reg_ms = int(cs.get("registered_ms") or 0)
    # STAGE B: confirm each pre-registered candidate on FRESH closes only.
    rand_fresh = _fresh_stats("L00_random", reg_ms)
    rand_life = _stats_of(_lane_rows("L00_random"))
    # random's fresh mean once it has a real fresh sample; else its lifetime mean (never 0-default:
    # random lifetime is deeply negative, a 0 floor would be STRICTER, but honesty > accident).
    rand_base = rand_fresh["mean_r"] if rand_fresh["n"] >= 15 else rand_life["mean_r"]
    state = _load_state()
    # Opus C2 (demote coverage): armed lane_promoted methods OUTSIDE the current confirm set
    # would otherwise get stat_by_mid={} -> has_data=False -> permanently undemotable. Track
    # them as pseudo-candidates so their fresh stats + fail counters keep flowing.
    it_set = list(cs.get("candidates", []))
    cand_mids = {c["mid"] for c in it_set}
    try:
        _armed = json.loads(ARMED.read_text(encoding="utf-8"))
        _armed = _armed if isinstance(_armed, list) else _armed.get("methods", [])
    except Exception:
        _armed = []
    it_set += [{"k": _safe_key(m["id"]), "mid": m["id"], "armed_watch": True}
               for m in _armed if m.get("source") == "lane_promoted" and m["id"] not in cand_mids]
    # (audit#2: lane dirs are _safe_key(mid) — a mangled/truncated id read as a raw dir name
    #  would find no ledger -> fresh n=0 forever -> undemotable zombie)
    stat_by_mid, rows = {}, []
    for c in it_set:
        rows_f = [x for x in _lane_rows(c["k"]) if _ts_ms(x) > reg_ms]
        fs = _stats_of(rows_f)
        ps = state.setdefault(c["mid"], {"passes": 0, "fails": 0, "last_n": 0,
                                         "last_pass_n": 0, "last_eval_n": 0})
        if "last_eval_n" not in ps:                  # migration: window cursor for v2.1 scoring
            ps["last_eval_n"] = ps.get("last_pass_n", ps.get("last_n", 0))
        if fs["n"] < ps.get("last_n", 0):           # closed count went backwards = legacy lifetime
            ps["passes"] = 0; ps["fails"] = 0       # counter or re-registration:
            ps["last_pass_n"] = 0; ps["last_eval_n"] = 0   # one-time reset, then fresh-n is monotone
        ps["last_n"] = fs["n"]
        # WINDOW-SCORED persistence (audit#2 CRITICAL): score pass OR fail exactly ONCE per
        # >=MIN_STEP-new-closes window since the last SCORED evaluation. The old shape scored
        # every 30-min tick: after a counted pass the <15-row disjoint slice made dis_p=1.0 ->
        # tick counted as FAIL -> passes oscillated 0->1->0 forever (funnel inert, v1 disease)
        # and an armed method hit DEMOTE_FAILS within 2 ticks. Between windows: HOLD.
        lev_n = int(ps.get("last_eval_n", 0))
        dis = [_f(x.get("r")) for x in rows_f[lev_n:]]   # disjoint since last SCORED window
        dis_p = _perm_p(dis, min_n=MIN_STEP)
        r = {"k": c["k"], "mid": c["mid"], "is_random": False, **fs,
             "p_dis": round(dis_p, 5), "armed_watch": bool(c.get("armed_watch")),
             "window_new": fs["n"] - lev_n,
             "confirm_bar": {"min_n": CONFIRM_MIN_N, "min_r": CONFIRM_MIN_R,
                             "min_pf": CONFIRM_MIN_PF, "rand_base": round(rand_base, 4),
                             "alpha": CONFIRM_ALPHA}}
        r["pass"] = bool(fs["n"] >= CONFIRM_MIN_N and fs["mean_r"] >= CONFIRM_MIN_R
                         and fs["pf"] >= CONFIRM_MIN_PF
                         and fs["mean_r"] >= rand_base + CONFIRM_BEAT_RANDOM
                         and dis_p < CONFIRM_ALPHA)
        rows.append(r)
        stat_by_mid[r["mid"]] = r
        scoreable = fs["n"] >= CONFIRM_MIN_N and (fs["n"] - lev_n) >= MIN_STEP
        if scoreable:
            if r["pass"]:
                ps["passes"] = ps.get("passes", 0) + 1; ps["fails"] = 0
                ps["last_pass_n"] = fs["n"]
            else:
                ps["fails"] = ps.get("fails", 0) + 1; ps["passes"] = 0
            ps["last_eval_n"] = fs["n"]              # window consumed either way
        # else: accumulating (young candidate OR <MIN_STEP new closes since the last scored
        # window) -> HOLD both counters. Never score a window that doesn't exist yet.
    # eligible = PERSIST consecutive passes (Codex #2 winner's-curse), ranked by fresh LCB.
    # armed_watch rows are demote-monitoring only — already promoted, never re-winners.
    eligible = [r for r in rows
                if not r.get("armed_watch")
                and state.get(r["mid"], {}).get("passes", 0) >= PERSIST]
    eligible.sort(key=lambda r: -(r.get("lcb") if r.get("lcb") is not None else -9))
    winners = eligible[:MAX_PROMOTE]
    res = apply_promotions(winners, stat_by_mid, state, dry=dry)
    if not dry:
        _save_state(state)
    res.update({"random_fresh_mean_r": round(rand_fresh["mean_r"], 4),
                "random_fresh_n": rand_fresh["n"], "rand_base": round(rand_base, 4),
                "registered_ms": reg_ms, "candidates": len(rows),
                "evaluated": len(rows), "eligible": len(eligible),
                "fresh": {r["k"]: {"n": r["n"], "mean_r": r["mean_r"], "pf": r["pf"],
                                   "p": r["p"], "pass": r["pass"]} for r in rows}})
    if not dry:                       # audit#2: a manual --dry run must not clobber the
        _write_hb({"status": "running", **res})   # live daemon's heartbeat with its own pid
    return res


def _write_hb(payload: dict) -> None:
    """Atomic heartbeat write (the old bare write_text was the file's only non-atomic write)."""
    body = json.dumps({"agent": "lane_promotion", "pid": os.getpid(),
                       "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       **payload})
    tmp = HB.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, HB)


def main():
    ap = argparse.ArgumentParser(description="lane -> mission promotion (paper-only)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry", action="store_true", help="evaluate + print, do not edit armed set")
    ap.add_argument("--reregister", action="store_true",
                    help="explicit new screen round: wipe confirm_set.json and re-register "
                         "(the ONLY sanctioned way to change the frozen candidate family)")
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
    if args.reregister and not args.dry:
        try:                                # persistence counters belong to the OLD family's
            CONFIRM_SET.unlink()            # windows — a new registration restarts the clock
        except FileNotFoundError:
            pass                            # (Opus I4: state wipe must not be skipped just
                                            #  because the set file was already gone)
        st = _load_state()                  # Codex: but ARMED lane_promoted methods keep their
        keep = {}                           # demote hysteresis — wiping it hands every promoted
        try:                                # method a fresh grace period on each re-registration
            _a = json.loads(ARMED.read_text(encoding="utf-8"))
            _a = _a if isinstance(_a, list) else _a.get("methods", [])
            keep = {m["id"]: st[m["id"]] for m in _a
                    if m.get("source") == "lane_promoted" and m.get("id") in st}
        except Exception:
            keep = {}
        _save_state(keep)
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
