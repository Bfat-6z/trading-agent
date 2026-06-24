"""Dependency visibility for reproducible local runs."""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
DEPENDENCY_LATEST = ROOT / "state" / "dependency_audit_latest.json"

REQUIRED_PACKAGES = ["pytest", "binance", "websockets"]


def audit_dependencies(required: list[str] | None = None, output_path: Path = DEPENDENCY_LATEST) -> dict[str, Any]:
    required = required or REQUIRED_PACKAGES
    rows = []
    missing = []
    for name in required:
        try:
            version = metadata.version(name)
            rows.append({"package": name, "installed": True, "version": version})
        except metadata.PackageNotFoundError:
            rows.append({"package": name, "installed": False, "version": None})
            missing.append(name)
    payload = {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "ok": not missing, "missing": missing, "packages": rows}
    write_json_atomic(output_path, payload)
    return payload
