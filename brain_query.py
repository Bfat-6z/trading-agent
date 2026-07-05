"""Read-only CLI over the second brain (P4).

Why a CLI and not (yet) an MCP server: both consumers (Claude Code, Codex) have
shell access here, the `mcp` package is not installed/vetted, and Codex's
sandboxed shell can't keep an MCP subprocess alive anyway (CreateProcessAsUserW
1312). This covers 100% of the read surface at zero dependency risk; an MCP
wrapper can be added later by exposing exactly these subcommands as tools.

STRICTLY READ-ONLY: opens brain.db in ro mode; never writes, never calls an LLM.

Usage:
  python brain_query.py counts                      # DSR trial totals
  python brain_query.py check-novelty '<method json>'
  python brain_query.py graveyard [--limit 20] [--family txt]
  python brain_query.py trials <method_id>
  python brain_query.py lessons
  python brain_query.py autopsy [--src shadow|mission] [--limit 20]
  python brain_query.py armed
  python brain_query.py render                      # regenerate BRAIN_SUMMARY.md
"""
from __future__ import annotations

import argparse
import json
import sys

import brain


def _j(x) -> None:
    print(json.dumps(x, ensure_ascii=False, indent=1, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(description="Second-brain read-only queries")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("counts")
    p = sub.add_parser("check-novelty"); p.add_argument("method_json")
    p = sub.add_parser("graveyard"); p.add_argument("--limit", type=int, default=20); p.add_argument("--contains", default="")
    p = sub.add_parser("trials"); p.add_argument("method_id")
    sub.add_parser("lessons")
    p = sub.add_parser("autopsy"); p.add_argument("--src", default=""); p.add_argument("--limit", type=int, default=20)
    sub.add_parser("armed")
    sub.add_parser("render")
    a = ap.parse_args()

    if a.cmd == "counts":
        _j(brain.trial_counts())
    elif a.cmd == "check-novelty":
        m = json.loads(a.method_json)
        gate, rows = brain.novelty_gate(m)
        _j({"gate": gate, "evidence": rows})
    elif a.cmd == "graveyard":
        con = brain.connect(readonly=True)
        try:
            q = ("SELECT method_id, failure_mode, side, oos_n, oos_mean_r, oos_net_pct, pvalue, "
                 "lockbox_pvalue, source, created_at FROM trials WHERE verdict='DEAD' ")
            args: list = []
            if a.contains:
                q += "AND method_id LIKE ? "
                args.append(f"%{a.contains}%")
            q += "ORDER BY created_at DESC LIMIT ?"
            args.append(a.limit)
            _j([dict(r) for r in con.execute(q, args)])
        finally:
            con.close()
    elif a.cmd == "trials":
        _j([dict(r) for r in brain.connect(readonly=True).execute(
            "SELECT * FROM trials WHERE method_id=? ORDER BY created_at DESC", (a.method_id,))])
    elif a.cmd == "lessons":
        con = brain.connect(readonly=True)
        try:
            _j([dict(r) for r in con.execute("SELECT * FROM lessons ORDER BY status, lesson_id")])
        finally:
            con.close()
    elif a.cmd == "autopsy":
        con = brain.connect(readonly=True)
        try:
            q = "SELECT * FROM trade_autopsy "
            args = []
            if a.src:
                q += "WHERE src=? "
                args.append(a.src)
            q += "ORDER BY created_at DESC LIMIT ?"
            args.append(a.limit)
            _j([dict(r) for r in con.execute(q, args)])
        finally:
            con.close()
    elif a.cmd == "armed":
        con = brain.connect(readonly=True)
        try:
            _j([dict(r) for r in con.execute(
                "SELECT method_id, state, reason, valid_at FROM method_state WHERE invalid_at IS NULL")])
        finally:
            con.close()
    elif a.cmd == "render":
        brain.render_views()
        print("rendered state/memory/BRAIN_SUMMARY.md")


if __name__ == "__main__":
    sys.exit(main())
