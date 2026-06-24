"""Static import guard for paper-learning modules."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
IMPORT_GUARD_LATEST = STATE_DIR / "agent_memory" / "security_import_guard_latest.json"

FORBIDDEN_IMPORT_PREFIXES = ("binance", "ccxt", "web3")
ALLOWED_EXCEPTIONS = {"live_permission_firewall.py", "test_harness.py"}

def imports_from_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports

def scan_import_guard(paths: list[Path], output_path: Path = IMPORT_GUARD_LATEST) -> dict[str, Any]:
    violations = []
    scanned = []
    for path in paths:
        if path.name in ALLOWED_EXCEPTIONS:
            continue
        if not path.exists() or path.suffix != ".py":
            continue
        try:
            imports = imports_from_file(path)
        except Exception as exc:
            violations.append({"path": str(path), "error": f"parse_error:{str(exc)[:120]}"})
            continue
        scanned.append(str(path))
        bad = [name for name in imports if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_IMPORT_PREFIXES)]
        if bad:
            violations.append({"path": str(path), "forbidden_imports": bad})
    payload = {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "ok": not violations, "scanned_count": len(scanned), "violations": violations, "can_place_live_orders": False}
    write_json_atomic(output_path, payload)
    return payload
