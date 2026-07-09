"""R2 threshold-tuning report over trigger_log.jsonl (read-only, run ad-hoc).

Answers, from the R1 dark-measurement window: how often does each path fire? How many unique
candidates/day would the R2 gate produce? Which paths co-fire? Plus per-path value distributions
(funding rate, whale score, flush depth) so thresholds are tuned ONCE on this data — never on the
closes that later judge the paths (Šidák lesson).

Usage: python trigger_stats.py [path-to-trigger_log.jsonl]
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

LOG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("state/llm_trader/trigger_log.jsonl")


def main() -> None:
    if not LOG.exists():
        print(f"no log at {LOG}")
        return
    cycles = []
    for ln in LOG.read_text(encoding="utf-8").splitlines():
        try:
            cycles.append(json.loads(ln))
        except Exception:
            continue
    if not cycles:
        print("log empty")
        return
    span_h = (cycles[-1]["ts_ms"] - cycles[0]["ts_ms"]) / 3_600_000 if len(cycles) > 1 else 0.0
    print(f"cycles={len(cycles)}  span={span_h:.1f}h  thresholds(last)={cycles[-1].get('thresholds')}")

    fire = Counter()                       # path -> total fires (sym-cycles)
    syms: dict[str, set] = defaultdict(set)  # path -> unique symbols
    vals: dict[str, list] = defaultdict(list)
    combo = Counter()
    per_cycle_hits = []
    for r in cycles:
        hits = r.get("hits") or {}
        per_cycle_hits.append(len(hits))
        for s, h in hits.items():
            ps = h.get("paths") or []
            combo["+".join(sorted(ps))] += 1
            for p in ps:
                fire[p] += 1
                syms[p].add(s)
                v = (h.get("vals") or {}).get(p) or {}
                if p == "funding_extreme":
                    vals[p].append(abs(v.get("rate") or 0))
                elif p == "whale":
                    vals[p].append(v.get("events") if v.get("events") is not None else (v.get("score") or 0))
                elif p in ("flush_no_oi", "flush_oi_dn"):
                    vals[p].append(v.get("ret5_pct") or 0)
                elif p == "chart_align":
                    for dk in ("adx", "eff", "px_e20"):
                        dv = v.get(dk)
                        if dv is not None:
                            vals[f"chart_align.{dk}"].append(dv)

    n = len(cycles)
    print(f"hits/cycle: mean={statistics.mean(per_cycle_hits):.1f} "
          f"median={statistics.median(per_cycle_hits):.0f} max={max(per_cycle_hits)}")
    print("\n-- per path (fires = sym-cycles; a coin firing 10 cycles in a row counts 10) --")
    for p, cnt in fire.most_common():
        vv = vals.get(p) or []
        extra = (f"  vals: min={min(vv):.4g} med={statistics.median(vv):.4g} max={max(vv):.4g}"
                 if vv else "")
        print(f"  {p:16} fires={cnt:5d} ({cnt / n:.1f}/cycle)  unique_syms={len(syms[p]):3d}{extra}")
    disc = {k: v for k, v in vals.items() if k.startswith("chart_align.") and v}
    if disc:
        print("\n-- chart_align tune discriminators (where would a stricter cut land?) --")
        for k, vv in disc.items():
            vv = sorted(vv)
            p25, p75 = vv[len(vv) // 4], vv[3 * len(vv) // 4]
            print(f"  {k:22} min={vv[0]:.2f} p25={p25:.2f} med={statistics.median(vv):.2f} "
                  f"p75={p75:.2f} max={vv[-1]:.2f}  (n={len(vv)})")
    print("\n-- path combos per sym-cycle --")
    for c, cnt in combo.most_common(12):
        print(f"  {c:40} {cnt}")
    if span_h > 0 and syms:
        # ascii-only output: the Windows console is cp1252 and chokes on unicode approx signs
        print(f"\n-- R2 gate preview: unique candidate syms/day ~= "
              f"{len(set().union(*syms.values())) / (span_h / 24):.0f} "
              f"(all paths pooled, {span_h:.1f}h window)")


if __name__ == "__main__":
    main()
