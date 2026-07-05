"""One-shot backfill: migrate every historical validation artifact into the
second-brain trials registry (P2 build order #3).

Every un-migrated dead method is a future laundering hole (the proposer could
re-invent it) AND an uncounted trial (which silently un-deflates every future
Sharpe/p-value). Sources, in order:

  1. deep_validation.json          — authoritative grid+lockbox runs
  2. tf_validation_{15m,1h,4h}.json — per-timeframe runs
  3. full_scale_validation.json     — the 200-coin sweep
  4. killed.jsonl                   — lab-round kills (deduped by id; label-only
                                      rows when the 150-cap pool already evicted
                                      the DSL — hash NULL but the trial COUNT is
                                      preserved for DSR)
  5. armed_methods.json             — current arming state -> method_state

Idempotency: refuses to run if trials already has rows (use --force to append
anyway — normally never needed).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import brain
from method_canonical import method_hash
from method_seeds import SEED_METHODS

ROOT = Path(__file__).resolve().parent
LAB = ROOT / "state" / "method_lab"


def _defs() -> dict[str, dict]:
    by_id = {m["id"]: m for m in SEED_METHODS}
    pool = LAB / "methods_pool.jsonl"
    if pool.exists():
        for line in pool.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    m = json.loads(line)
                    by_id[m["id"]] = m
                except Exception:
                    pass
    return by_id


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if brain.trial_counts()["total"] > 0 and not args.force:
        print(json.dumps({"skip": "trials already populated", **brain.trial_counts()}))
        return

    defs = _defs()
    total = 0

    # 1. deep_validation.json (has lockbox + opt params)
    d = _load(LAB / "deep_validation.json")
    if d.get("results"):
        uni = f"usdtperp>={(d.get('min_qvol_usd') or 0) / 1e6:.0f}M"
        total += brain.record_trials(d["results"], defs, source="deep_validation",
                                     universe=uni, timeframe="15m",
                                     months=float(d.get("months") or 5.0))

    # 2. per-TF validations
    for tf in ("15m", "1h", "4h"):
        t = _load(LAB / f"tf_validation_{tf}.json")
        if t.get("results"):
            rows = [{**r, "lockbox_held": r.get("lockbox_held")} for r in t["results"]]
            total += brain.record_trials(rows, defs, source=f"tf_validation_{tf}",
                                         universe=f"usdtperp>={(t.get('min_qvol_usd') or 50e6) / 1e6:.0f}M",
                                         timeframe=tf, months=float(t.get("months") or 5.0))

    # 3. full-scale sweep (field names differ: oos_win / oos_net_pct already match)
    f = _load(LAB / "full_scale_validation.json")
    if f.get("results"):
        total += brain.record_trials(f["results"], defs, source="full_scale",
                                     universe=f"usdtperp>={(f.get('min_qvol_usd') or 5e6) / 1e6:.0f}M",
                                     timeframe="15m", months=float(f.get("months") or 5.0))

    # 4. lab kills — dedup by id (latest row wins); label-only where DSL is gone
    killed_p = LAB / "killed.jsonl"
    if killed_p.exists():
        latest: dict[str, dict] = {}
        for line in killed_p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    latest[r.get("id")] = r
                except Exception:
                    pass
        rows = []
        for r in latest.values():
            rows.append({"id": r.get("id"), "side": r.get("side"),
                         "oos_n": r.get("oos_n"), "oos_mean_r": r.get("oos_mean_r"),
                         "oos_win": r.get("oos_win_rate"),
                         "oos_net_pct": r.get("oos_total_net_pct"),
                         "pvalue": r.get("pvalue")})
        if rows:
            total += brain.record_trials(rows, defs, source="lab_killed_backfill",
                                         universe="lab", timeframe="15m", months=2.0)

    # 5. arming state
    changes = brain.sync_armed_state(defs)

    counts = brain.trial_counts()
    with_dsl = len([1 for _ in brain.known_hashes()])
    print(json.dumps({"backfilled_rows": total, "state_changes": changes,
                      "hashes_with_dsl": with_dsl, **counts}))


if __name__ == "__main__":
    main()
