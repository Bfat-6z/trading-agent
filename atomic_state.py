"""Atomic local state helpers for trading-agent.

These helpers keep Phase A modules from hand-writing JSON/JSONL in slightly
different ways. They intentionally do not touch exchange APIs or secrets.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {} if default is None else default


def read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if limit is not None:
        lines = lines[-max(0, int(limit)) :]
    rows: list[dict] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(canonical_json(row) + "\n")


def append_jsonl_once(path: Path, row: dict, id_field: str) -> bool:
    value = row.get(id_field)
    if not value:
        append_jsonl(path, row)
        return True
    existing = {str(item.get(id_field)) for item in read_jsonl(path) if item.get(id_field)}
    if str(value) in existing:
        return False
    append_jsonl(path, row)
    return True
