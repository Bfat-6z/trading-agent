"""Ingest workflow-generated up-market method candidates into the method pool.
Un-escapes HTML-entity ops, DSL-validates, dedupes, appends. Paper/offline only."""
import html
import json
import sys
from pathlib import Path

import method_lab_runner as mlr

OUT = Path(sys.argv[1])
POOL = Path("state/method_lab/methods_pool.jsonl")

raw = json.loads(OUT.read_text(encoding="utf-8"))
# methods live under result.methods (or top-level methods)
methods = None
for holder in (raw.get("result"), raw):
    if isinstance(holder, dict) and isinstance(holder.get("methods"), list):
        methods = holder["methods"]
        break
if methods is None:
    print("NO methods found in output"); sys.exit(1)


def unescape(m):
    for c in m.get("when", []):
        if isinstance(c.get("op"), str):
            c["op"] = html.unescape(c["op"]).strip()
        if isinstance(c.get("feat"), str):
            c["feat"] = html.unescape(c["feat"]).strip()
    for k in ("name", "desc", "id"):
        if isinstance(m.get(k), str):
            m[k] = html.unescape(m[k])
    return m


# existing pool: ids + condition-signatures for dedupe
existing = []
if POOL.exists():
    for line in POOL.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                existing.append(json.loads(line))
            except Exception:
                pass
seen_ids = {m.get("id") for m in existing}


def sig(m):
    return (m.get("side"), tuple(sorted((c["feat"], c["op"], round(float(c["val"]), 4))
                                        for c in m.get("when", []))))


seen_sigs = {sig(m) for m in existing if m.get("when")}

added, rejected, dup = [], 0, 0
for m in methods:
    m = unescape(dict(m))
    v = mlr.validate_method(m)
    if not v:
        rejected += 1
        continue
    if v["id"] in seen_ids or sig(v) in seen_sigs:
        dup += 1
        continue
    seen_ids.add(v["id"]); seen_sigs.add(sig(v))
    added.append(v)

with POOL.open("a", encoding="utf-8") as fh:
    for m in added:
        fh.write(json.dumps(m) + "\n")

print(json.dumps({"input": len(methods), "added": len(added),
                  "rejected_dsl": rejected, "duplicate": dup,
                  "pool_total": len(existing) + len(added),
                  "sample_ids": [m["id"] for m in added[:12]]}))
