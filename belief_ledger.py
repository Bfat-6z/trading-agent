"""Persistent belief ledger for the trading agent.

The ledger turns loose lessons into testable beliefs with confidence and
evidence. It is deliberately deterministic: LLMs may propose belief text, but
confidence changes only through structured evidence updates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from event_store import safe_append_event, safe_append_snapshot

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
LEDGER_PATH = MEMORY_DIR / "belief_ledger.json"
LEDGER_REPORT_PATH = MEMORY_DIR / "belief_ledger_latest.md"

VALID_STATUSES = {"candidate", "active", "weakened", "rejected"}
VALID_EVIDENCE_SIDES = {"for", "against"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def make_belief_id(statement: str, scope: str = "global", topic: str = "general") -> str:
    raw = json.dumps(
        {
            "statement": normalize_text(statement),
            "scope": normalize_text(scope),
            "topic": normalize_text(topic),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "belief_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def status_from_confidence(confidence: float, evidence_count: int = 0) -> str:
    confidence = clamp(confidence)
    if confidence <= 0.15:
        return "rejected"
    if confidence < 0.4:
        return "weakened"
    if confidence >= 0.65 and evidence_count > 0:
        return "active"
    return "candidate"


def default_ledger() -> dict:
    return {
        "created_at": utc_now(),
        "updated_at": None,
        "version": 1,
        "beliefs": {},
        "history": [],
    }


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def load_ledger(path: Path = LEDGER_PATH) -> dict:
    payload = read_json(path)
    if not payload:
        return default_ledger()
    base = default_ledger()
    base.update(payload)
    if not isinstance(base.get("beliefs"), dict):
        base["beliefs"] = {}
    if not isinstance(base.get("history"), list):
        base["history"] = []
    return base


def save_ledger(ledger: dict, path: Path = LEDGER_PATH, write_report: bool = True) -> dict:
    ledger["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if write_report:
        report_path = LEDGER_REPORT_PATH if path.resolve() == LEDGER_PATH.resolve() else path.with_suffix(".md")
        report_path.write_text(render_report(ledger), encoding="utf-8")
    if path.resolve() == LEDGER_PATH.resolve():
        safe_append_snapshot("belief_ledger", "belief_ledger", compact_ledger(ledger), ts=ledger["updated_at"])
    return ledger


def evidence_count(belief: dict) -> int:
    return len(belief.get("evidence_for") or []) + len(belief.get("evidence_against") or [])


def new_belief(
    statement: str,
    scope: str = "global",
    topic: str = "general",
    confidence: float = 0.5,
    metadata: dict | None = None,
    ts: str | None = None,
) -> dict:
    row_ts = ts or utc_now()
    row_confidence = clamp(safe_float(confidence, 0.5))
    return {
        "belief_id": make_belief_id(statement, scope, topic),
        "statement": " ".join(str(statement or "").strip().split()),
        "scope": str(scope or "global"),
        "topic": str(topic or "general"),
        "confidence": round(row_confidence, 6),
        "status": status_from_confidence(row_confidence, 0),
        "evidence_for": [],
        "evidence_against": [],
        "metadata": metadata or {},
        "created_at": row_ts,
        "updated_at": row_ts,
        "last_tested_at": None,
    }


def append_history(ledger: dict, event: str, payload: dict, ts: str | None = None) -> None:
    row = {"ts": ts or utc_now(), "event": event, **payload}
    history = list(ledger.get("history") or [])
    history.append(row)
    ledger["history"] = history[-500:]


def upsert_belief(
    ledger: dict,
    statement: str,
    scope: str = "global",
    topic: str = "general",
    confidence: float = 0.5,
    metadata: dict | None = None,
    ts: str | None = None,
) -> dict:
    if not str(statement or "").strip():
        raise ValueError("belief statement is required")
    belief_id = make_belief_id(statement, scope, topic)
    beliefs = ledger.setdefault("beliefs", {})
    if belief_id not in beliefs:
        beliefs[belief_id] = new_belief(statement, scope, topic, confidence, metadata, ts)
        append_history(ledger, "belief_created", {"belief_id": belief_id, "topic": topic}, ts)
        safe_append_event("belief_ledger", "belief_created", {"belief_id": belief_id, "statement": statement, "scope": scope, "topic": topic}, ts=ts)
        return beliefs[belief_id]

    belief = beliefs[belief_id]
    belief["statement"] = belief.get("statement") or " ".join(str(statement).strip().split())
    belief["scope"] = belief.get("scope") or str(scope or "global")
    belief["topic"] = belief.get("topic") or str(topic or "general")
    if metadata:
        merged = dict(belief.get("metadata") or {})
        merged.update(metadata)
        belief["metadata"] = merged
    belief["updated_at"] = ts or utc_now()
    append_history(ledger, "belief_seen", {"belief_id": belief_id, "topic": belief.get("topic")}, ts)
    return belief


def confidence_delta(side: str, weight: float) -> float:
    magnitude = clamp(abs(weight), 0.0, 5.0) * 0.04
    return magnitude if side == "for" else -magnitude


def add_evidence(
    ledger: dict,
    belief_id: str,
    side: str,
    weight: float,
    source: str,
    summary: str,
    event_id: str | None = None,
    metadata: dict | None = None,
    ts: str | None = None,
) -> dict:
    side = str(side).lower().strip()
    if side not in VALID_EVIDENCE_SIDES:
        raise ValueError("evidence side must be 'for' or 'against'")
    beliefs = ledger.setdefault("beliefs", {})
    if belief_id not in beliefs:
        raise KeyError(f"unknown belief_id: {belief_id}")
    if not str(summary or "").strip():
        raise ValueError("evidence summary is required")

    row_ts = ts or utc_now()
    row_weight = clamp(abs(safe_float(weight, 1.0)), 0.0, 5.0)
    belief = beliefs[belief_id]
    evidence = {
        "ts": row_ts,
        "side": side,
        "weight": row_weight,
        "source": str(source or "unknown"),
        "summary": " ".join(str(summary).strip().split()),
        "event_id": event_id,
        "metadata": metadata or {},
    }
    key = "evidence_for" if side == "for" else "evidence_against"
    rows = list(belief.get(key) or [])
    rows.append(evidence)
    belief[key] = rows[-100:]
    old_confidence = safe_float(belief.get("confidence"), 0.5)
    new_confidence = clamp(old_confidence + confidence_delta(side, row_weight))
    belief["confidence"] = round(new_confidence, 6)
    belief["status"] = status_from_confidence(new_confidence, evidence_count(belief))
    belief["last_tested_at"] = row_ts
    belief["updated_at"] = row_ts
    append_history(
        ledger,
        "evidence_added",
        {"belief_id": belief_id, "side": side, "weight": row_weight, "confidence": belief["confidence"], "status": belief["status"]},
        row_ts,
    )
    safe_append_event("belief_ledger", "evidence_added", {"belief_id": belief_id, "side": side, "weight": row_weight, "confidence": belief["confidence"], "summary": summary}, ts=row_ts)
    return belief


def parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def decay_stale_beliefs(
    ledger: dict,
    max_age_hours: float = 72.0,
    decay: float = 0.03,
    now: datetime | None = None,
    ts: str | None = None,
) -> dict:
    current = now or datetime.now(timezone.utc)
    row_ts = ts or current.isoformat(timespec="seconds")
    stale_count = 0
    for belief in (ledger.get("beliefs") or {}).values():
        last = parse_ts(belief.get("last_tested_at") or belief.get("updated_at") or belief.get("created_at"))
        if not last:
            continue
        age_hours = (current - last).total_seconds() / 3600
        if age_hours < max_age_hours:
            continue
        confidence = safe_float(belief.get("confidence"), 0.5)
        if confidence <= 0.5:
            continue
        belief["confidence"] = round(clamp(confidence - abs(decay)), 6)
        belief["status"] = status_from_confidence(belief["confidence"], evidence_count(belief))
        belief["updated_at"] = row_ts
        stale_count += 1
    if stale_count:
        append_history(ledger, "stale_decay", {"count": stale_count, "decay": abs(decay)}, row_ts)
        safe_append_event("belief_ledger", "stale_decay", {"count": stale_count, "decay": abs(decay)}, ts=row_ts)
    return ledger


def compact_ledger(ledger: dict) -> dict:
    beliefs = ledger.get("beliefs") or {}
    by_status: dict[str, int] = {status: 0 for status in sorted(VALID_STATUSES)}
    for belief in beliefs.values():
        status = str(belief.get("status") or "candidate")
        by_status[status] = by_status.get(status, 0) + 1
    top = sorted(
        beliefs.values(),
        key=lambda item: (safe_float(item.get("confidence")), item.get("updated_at") or ""),
        reverse=True,
    )[:10]
    return {
        "updated_at": ledger.get("updated_at"),
        "belief_count": len(beliefs),
        "by_status": by_status,
        "top_beliefs": [
            {
                "belief_id": belief.get("belief_id"),
                "topic": belief.get("topic"),
                "scope": belief.get("scope"),
                "confidence": belief.get("confidence"),
                "status": belief.get("status"),
                "statement": belief.get("statement"),
            }
            for belief in top
        ],
    }


def render_report(ledger: dict) -> str:
    summary = compact_ledger(ledger)
    lines = [
        "# Belief Ledger",
        "",
        f"Generated: {utc_now()}",
        f"Updated: {ledger.get('updated_at')}",
        f"Beliefs: {summary['belief_count']}",
        "",
        "## Status Counts",
    ]
    for status, count in summary["by_status"].items():
        lines.append(f"- {status}: {count}")
    lines.extend(["", "## Top Beliefs"])
    if not summary["top_beliefs"]:
        lines.append("- No beliefs recorded yet.")
    for belief in summary["top_beliefs"]:
        lines.append(
            f"- `{belief['belief_id']}` {belief['confidence']:.2f} {belief['status']} "
            f"[{belief['topic']}/{belief['scope']}]: {belief['statement']}"
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage trading agent belief ledger")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--decay", action="store_true")
    parser.add_argument("--add-belief")
    parser.add_argument("--scope", default="global")
    parser.add_argument("--topic", default="general")
    parser.add_argument("--confidence", type=float, default=0.5)
    parser.add_argument("--belief-id")
    parser.add_argument("--evidence-side", choices=sorted(VALID_EVIDENCE_SIDES))
    parser.add_argument("--evidence-weight", type=float, default=1.0)
    parser.add_argument("--evidence-source", default="manual")
    parser.add_argument("--evidence-summary")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    ledger = load_ledger()
    changed = False
    if args.add_belief:
        belief = upsert_belief(ledger, args.add_belief, args.scope, args.topic, args.confidence)
        print(json.dumps(belief, ensure_ascii=True, indent=2, sort_keys=True))
        changed = True
    if args.evidence_side or args.evidence_summary:
        if not args.belief_id:
            raise SystemExit("--belief-id is required when adding evidence")
        if not args.evidence_side or not args.evidence_summary:
            raise SystemExit("--evidence-side and --evidence-summary are required")
        belief = add_evidence(
            ledger,
            args.belief_id,
            args.evidence_side,
            args.evidence_weight,
            args.evidence_source,
            args.evidence_summary,
        )
        print(json.dumps(belief, ensure_ascii=True, indent=2, sort_keys=True))
        changed = True
    if args.decay:
        decay_stale_beliefs(ledger)
        changed = True
    if changed:
        save_ledger(ledger)
    if args.status or not changed:
        print(json.dumps(compact_ledger(ledger), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
