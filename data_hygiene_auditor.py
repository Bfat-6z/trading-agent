"""Data hygiene audit for JSON/JSONL learning state."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
HYGIENE_LATEST = MEMORY_DIR / "data_hygiene_latest.json"


def audit_jsonl(path: Path) -> dict[str, Any]:
    total = 0
    bad = 0
    if not path.exists():
        return {"path": str(path), "exists": False, "rows": 0, "bad_rows": 0, "ok": True}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        total += 1
        try:
            json.loads(line)
        except Exception:
            bad += 1
    return {"path": str(path), "exists": True, "rows": total, "bad_rows": bad, "ok": bad == 0}


def audit_learning_state(paths: list[Path] | None = None, output_path: Path = HYGIENE_LATEST) -> dict[str, Any]:
    paths = paths or [MEMORY_DIR / "episodes.jsonl", MEMORY_DIR / "post_trade_reviews.jsonl", MEMORY_DIR / "counterfactual_replays.jsonl", MEMORY_DIR / "memory_candidates.jsonl", MEMORY_DIR / "memory_promoted.jsonl", MEMORY_DIR / "memory_rejected.jsonl"]
    rows = [audit_jsonl(path) for path in paths]
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "ok": all(row["ok"] for row in rows), "files": rows, "bad_file_count": sum(1 for row in rows if not row["ok"])}
    write_json_atomic(output_path, payload)
    return payload
