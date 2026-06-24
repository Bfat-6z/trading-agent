"""Host runtime checks for local 24/7 paper learner."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
HOST_RUNTIME_LATEST = STATE_DIR / "host_runtime_latest.json"


def check_host_runtime(min_free_gb: float = 1.0, output_path: Path = HOST_RUNTIME_LATEST) -> dict[str, Any]:
    usage = shutil.disk_usage(ROOT)
    free_gb = usage.free / (1024 ** 3)
    errors = []
    warnings = []
    if free_gb < min_free_gb:
        errors.append("low_disk_space")
    if os.name == "nt" and not os.environ.get("TRADING_AGENT_AUTOSTART_CONFIRMED"):
        warnings.append("windows_autostart_not_confirmed")
    payload = {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "status": "critical" if errors else "warn" if warnings else "ok", "free_disk_gb": round(free_gb, 3), "errors": errors, "warnings": warnings, "autostart_confirmed": bool(os.environ.get("TRADING_AGENT_AUTOSTART_CONFIRMED"))}
    write_json_atomic(output_path, payload)
    return payload
