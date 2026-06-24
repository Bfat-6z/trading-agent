"""Semantic memory compaction for trading-agent events.

Raw JSONL and SQLite remain the source of truth. This module writes a compact,
queryable memory file for long-term learning and promotes repeated lessons into
belief candidates without duplicating existing beliefs.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from belief_ledger import load_ledger, save_ledger, upsert_belief
from event_store import query_recent_events, safe_append_event, safe_append_snapshot
from market_learner import safe_float, valid_paper_close, valid_paper_open

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
SEMANTIC_MEMORY_PATH = MEMORY_DIR / "semantic_memory.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def symbol_from(row: dict) -> str | None:
    for key in ("symbol", "token_symbol"):
        if row.get(key):
            return str(row[key]).upper()
    for nested_key in ("signal", "position"):
        nested = row.get(nested_key)
        if isinstance(nested, dict) and nested.get("symbol"):
            return str(nested["symbol"]).upper()
    return None


def side_from(row: dict) -> str | None:
    if row.get("side"):
        return str(row["side"]).upper()
    for nested_key in ("signal", "position"):
        nested = row.get(nested_key)
        if isinstance(nested, dict) and nested.get("side"):
            return str(nested["side"]).upper()
    return None


def summarize_events(events: list[dict], ts: str | None = None, window_hours: float = 24.0) -> dict:
    row_ts = ts or utc_now()
    counts_by_event: Counter[str] = Counter()
    counts_by_source: Counter[str] = Counter()
    symbols: Counter[str] = Counter()
    sides: Counter[str] = Counter()
    risk_blocks: Counter[str] = Counter()
    lessons: Counter[str] = Counter()
    paper = {"opens": 0, "closes": 0, "wins": 0, "losses": 0, "net": 0.0}

    for row in events:
        event = str(row.get("event") or "unknown")
        source = str(row.get("source") or "unknown")
        counts_by_event[event] += 1
        counts_by_source[source] += 1
        symbol = symbol_from(row)
        side = side_from(row)
        if symbol:
            symbols[symbol] += 1
        if side:
            sides[side] += 1
        if event == "paper_open" and valid_paper_open(row):
            paper["opens"] += 1
        elif event == "paper_close" and valid_paper_close(row):
            paper["closes"] += 1
            net = safe_float(row.get("net"))
            paper["net"] += net
            if net > 0:
                paper["wins"] += 1
            elif net < 0:
                paper["losses"] += 1
        elif event == "risk_block":
            risk_blocks[str(row.get("reason") or "unknown")] += 1
        elif event == "lesson" and row.get("lesson"):
            lessons[" ".join(str(row["lesson"]).split())] += 1

    closes = max(1, int(paper["closes"]))
    paper_summary = {
        **paper,
        "net": round(float(paper["net"]), 8),
        "win_rate": round(int(paper["wins"]) / closes, 4) if paper["closes"] else 0.0,
    }
    repeated_lessons = [
        {"lesson": lesson, "count": count}
        for lesson, count in sorted(lessons.items(), key=lambda item: (-item[1], item[0]))
        if count >= 2
    ]
    return {
        "ts": row_ts,
        "window_hours": window_hours,
        "event_count": len(events),
        "counts_by_event": dict(sorted(counts_by_event.items())),
        "counts_by_source": dict(sorted(counts_by_source.items())),
        "top_symbols": [{"symbol": symbol, "count": count} for symbol, count in symbols.most_common(12)],
        "side_counts": dict(sorted(sides.items())),
        "paper": paper_summary,
        "risk_blocks": dict(sorted(risk_blocks.items())),
        "repeated_lessons": repeated_lessons[:12],
    }


def promote_repeated_lessons(summary: dict, ledger_path: Path | None = None) -> list[dict]:
    repeated = summary.get("repeated_lessons") if isinstance(summary.get("repeated_lessons"), list) else []
    if not repeated:
        return []
    path = ledger_path or MEMORY_DIR / "belief_ledger.json"
    ledger = load_ledger(path)
    promoted: list[dict] = []
    for item in repeated:
        lesson = " ".join(str(item.get("lesson") or "").split())
        if not lesson:
            continue
        belief = upsert_belief(
            ledger,
            lesson,
            scope="trading_agent",
            topic="lesson",
            confidence=0.55,
            metadata={"source": "memory_compactor", "repeat_count": item.get("count")},
            ts=summary.get("ts"),
        )
        promoted.append({"belief_id": belief.get("belief_id"), "statement": belief.get("statement"), "repeat_count": item.get("count")})
    save_ledger(ledger, path, write_report=path.resolve() == (MEMORY_DIR / "belief_ledger.json").resolve())
    seen = set()
    unique: list[dict] = []
    for item in promoted:
        belief_id = item.get("belief_id")
        if belief_id and belief_id not in seen:
            seen.add(belief_id)
            unique.append(item)
    return unique


def compact_memory(
    events: list[dict] | None = None,
    semantic_path: Path = SEMANTIC_MEMORY_PATH,
    ledger_path: Path | None = None,
    lookback_hours: float = 24.0,
    limit: int = 800,
    max_entries: int = 60,
    promote: bool = True,
) -> dict:
    ts = utc_now()
    rows = events if events is not None else query_recent_events(lookback_hours=lookback_hours, limit=limit)
    summary = summarize_events(rows, ts=ts, window_hours=lookback_hours)
    promoted = promote_repeated_lessons(summary, ledger_path) if promote else []
    summary["promoted_beliefs"] = promoted

    memory = read_json(semantic_path)
    if not memory:
        memory = {"created_at": ts, "updated_at": None, "entries": []}
    entries = list(memory.get("entries") or [])
    entries.append(summary)
    memory["entries"] = entries[-max_entries:]
    memory["latest"] = summary
    memory["updated_at"] = ts
    write_json(semantic_path, memory)
    if semantic_path.resolve() == SEMANTIC_MEMORY_PATH.resolve():
        safe_append_snapshot("memory_compactor", "semantic_memory", summary, ts=ts)
        safe_append_event("memory_compactor", "memory_compacted", {"event_count": summary.get("event_count"), "promoted_beliefs": promoted}, ts=ts)
    return memory


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compact recent trading-agent events into semantic memory")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument("--limit", type=int, default=800)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    memory = read_json(SEMANTIC_MEMORY_PATH) if args.status else compact_memory(lookback_hours=args.lookback_hours, limit=args.limit)
    print(json.dumps((memory or {}).get("latest") or {"status": "no_memory"}, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
