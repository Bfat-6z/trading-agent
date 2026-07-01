"""Edge-research harness — experience ledger (owner directive).

Append-only record of every tested setup + a human-readable ranked view. This is
the primary review artifact: what was tried, on what timeframe/universe, how many
trades, in-sample vs holdout expectancy, profit factor, DSR, verdict, and why.

- research_ledger.jsonl : append-only, one row per tested setup (machine).
- experience_ranked.md  : regenerated after each sweep, sorted by holdout
                          expectancy then DSR (human).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LEDGER_PATH = ROOT / "state" / "agent_memory" / "research_ledger.jsonl"
RANKED_PATH = ROOT / "plans" / "260701-0200-claude-takeover-edge-first" / "reports" / "experience_ranked.md"


def append_row(row: dict[str, Any], *, ledger_path: Path = LEDGER_PATH) -> None:
    """Append one tested-setup row. Caller supplies a stamped_at timestamp (the
    harness has no wall-clock); we do not invent one here."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def load_rows(ledger_path: Path = LEDGER_PATH) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        return []
    rows = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _num(row: dict[str, Any], *path, default=None):
    cur: Any = row
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def regenerate_ranked(ledger_path: Path = LEDGER_PATH, ranked_path: Path = RANKED_PATH) -> str:
    """Rebuild experience_ranked.md from the ledger, sorted by holdout expectancy
    (desc) then DSR (desc). Rows without holdout sort last."""
    rows = load_rows(ledger_path)

    def sort_key(r):
        ho = _num(r, "holdout", "expectancy_r", default=None)
        dsr = _num(r, "dsr", "dsr", default=0.0) or 0.0
        # None holdout sorts last
        return (ho is not None, ho if ho is not None else -9.99, dsr)

    rows_sorted = sorted(rows, key=sort_key, reverse=True)

    lines = ["# Experience — Ranked Research Ledger", "",
             "Every setup the harness has tested, best holdout first. KILL is the",
             "normal result. A row reaches the holdout column only if it passed all",
             "in-sample overfit gates first.", "",
             f"Total setups tested: **{len(rows)}**", "",
             "| # | Family | TF | Dir | N (is/ho) | Exp is | Exp ho | PF is | PF ho | DSR | Verdict | Reason |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows_sorted, 1):
        fam = r.get("family", "?")
        tf = r.get("timeframe", "?")
        d = r.get("direction", "?")
        n_is = _num(r, "in_sample", "trades", default="-")
        n_ho = _num(r, "holdout", "trades", default="-")
        exp_is = _fmt(_num(r, "in_sample", "expectancy_r"))
        exp_ho = _fmt(_num(r, "holdout", "expectancy_r"))
        pf_is = _fmt(_num(r, "in_sample", "profit_factor"))
        pf_ho = _fmt(_num(r, "holdout", "profit_factor"))
        dsr = _fmt(_num(r, "dsr", "dsr"))
        verdict = r.get("verdict", "?")
        reason = (r.get("reason") or "").replace("|", "/")[:40]
        lines.append(f"| {i} | {fam} | {tf} | {d} | {n_is}/{n_ho} | {exp_is} | {exp_ho} | "
                     f"{pf_is} | {pf_ho} | {dsr} | {verdict} | {reason} |")
    content = "\n".join(lines) + "\n"
    ranked_path.parent.mkdir(parents=True, exist_ok=True)
    ranked_path.write_text(content, encoding="utf-8")
    return content


def _fmt(v) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.3f}"
    except Exception:
        return str(v)
