"""memory_lint — grep-scale contradiction + staleness detector for the two memory stores.
Owner 2026-07-08 (research agent 5): the +EV move on duplicated memory is NOT bulk rewrite,
it's catching CONTRADICTIONS (same key, different value across stores) + stale index state.
Pure stdlib, read-only. Exit 1 if any contradiction so it can bolt onto verify/heartbeat.

  python tools/memory_lint.py            # report
  python tools/memory_lint.py --strict   # exit 1 on contradictions (for CI/heartbeat)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:                                    # Windows console is cp1252 — force UTF-8 for VN text
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MEM = Path(r"C:\Users\ACER\.claude\projects\E--keo-moi-mail\memory")
VAULT = Path(r"E:\keo-moi-mail\trading-agent\vault")

# canonical numeric facts worth guarding — key -> regex capturing its value
FACTS = {
    # tightened to the ACTUAL liquidity-floor phrasing ($NN M / NNe6), not any stray number
    "MIN_QVOL / liquidity floor": r"(?:MIN_QVOL|liquidity floor|liquid)\D{0,18}?(\$?\d{2,3}\s?M|\d{2,3}e6)",
    "PER_POS_CAP": r"PER[_ ]?POS[_ ]?CAP\D{0,6}(0?\.\d{1,2})",
    "atr gate %": r"atr\D{0,10}?(3\.3|3\.33|3\.3%)",
    # NOTE: MECH_LEV deliberately NOT checked — x5 OR x10 are BOTH valid by rule, not a conflict.
}
# index lines that embed mutable STATE (a position, an 'open', a specific $ pnl) instead of
# routing — these rot (agent5). We flag digits+state words on MEMORY.md bullet lines.
# only an actual OPEN POSITION rots — require a price pattern (@ $price / SL $ / TP $),
# not the bare word "open" (which appears in many non-state routing lines -> false alarms)
STATE_WORDS = re.compile(r"(open\s+@|@ ?\$\d|SL \$?\d|TP \$?\d|LONG open|SHORT open)", re.I)


def scan_facts():
    hits: dict[str, dict[str, list[str]]] = {}
    roots = [("memory", MEM.glob("*.md")), ("vault", (p for p in VAULT.rglob("*.md") if "auto" not in p.parts))]
    for store, it in roots:
        for p in it:
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for key, rx in FACTS.items():
                for m in re.finditer(rx, txt, re.I):
                    val = re.sub(r"\s", "", m.group(1)).lstrip("$").upper()
                    val = {"50M": "50", "30M": "30", "50E6": "50", "0.10": ".10", "0.25": ".25"}.get(val, val)
                    hits.setdefault(key, {}).setdefault(val, []).append(f"{store}/{p.name}")
    return hits


def scan_stale_index():
    idx = MEM / "MEMORY.md"
    out = []
    if idx.exists():
        for i, ln in enumerate(idx.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if ln.strip().startswith("- ") and STATE_WORDS.search(ln):
                out.append((i, ln.strip()[:100]))
    return out


def main():
    strict = "--strict" in sys.argv
    facts = scan_facts()
    stale = scan_stale_index()
    contradictions = 0
    print("=== numeric-fact consistency (same key, >1 distinct value = CONTRADICTION) ===")
    for key, vals in facts.items():
        distinct = {v for v in vals if v not in ("", "X")}
        if len(distinct) > 1:
            # PER_POS_CAP .25/.10 and floor 50/30 are legit history if one is clearly older;
            # we FLAG for a human glance, not auto-fix (agent3: prefer under-merging).
            contradictions += 1
            print(f"  [CONFLICT] {key}: {sorted(distinct)}")
            for v, files in vals.items():
                print(f"      {v}: {len(files)}x  ({', '.join(sorted(set(files))[:3])}{'…' if len(set(files)) > 3 else ''})")
        elif distinct:
            print(f"  [ok] {key} = {distinct.pop()} (consistent across {len(vals[next(iter(vals))])} mentions)")
    print("\n=== MEMORY.md index lines embedding mutable STATE (should be routing, rot risk) ===")
    if stale:
        for i, ln in stale:
            print(f"  L{i}: {ln}")
    else:
        print("  none — index is routing-only, clean")
    print(f"\nsummary: {contradictions} numeric conflict(s), {len(stale)} stale-state index line(s)")
    if strict and (contradictions or stale):
        sys.exit(1)


if __name__ == "__main__":
    main()
