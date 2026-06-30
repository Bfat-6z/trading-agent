"""Static import and script guard for paper-learning modules."""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
IMPORT_GUARD_LATEST = STATE_DIR / "agent_memory" / "security_import_guard_latest.json"

FORBIDDEN_IMPORT_PREFIXES = ("binance", "ccxt", "web3")
ALLOWED_EXCEPTIONS = {"legacy_live_blocker.py", "live_permission_firewall.py", "security_import_guard.py", "sitecustomize.py", "test_harness.py"}
BLOCKED_FILENAME_PATTERNS = (
    re.compile(r"^execute_.*\.py$", re.IGNORECASE),
    re.compile(r".*live.*\.py$", re.IGNORECASE),
)
READONLY_PRIVATE_FILENAME_PATTERNS = (
    re.compile(r"^check_.*live.*\.py$", re.IGNORECASE),
    re.compile(r".*(account|balance|position).*\.py$", re.IGNORECASE),
)
FORBIDDEN_CALL_PATTERNS = {
    "futures_create_order": re.compile(r"\bfutures_create_order\s*\("),
    "futures_change_leverage": re.compile(r"\bfutures_change_leverage\s*\("),
    "_request_futures_api_signed": re.compile(r"_request_futures_api\s*\([^)]*(True|signed\s*=\s*True)", re.IGNORECASE | re.DOTALL),
    "raw_order_http": re.compile(r"requests\.(post|put|delete)\s*\([^)]*(order|leverage|withdraw|transfer)", re.IGNORECASE | re.DOTALL),
    "subprocess_live_script": re.compile(r"subprocess\.(run|Popen|call)\s*\([^)]*(execute_|live)", re.IGNORECASE | re.DOTALL),
}
PRIVATE_ACCOUNT_PATTERNS = {
    "private_account_read": re.compile(r"\b(account|balance|position|futures_account|futures_position_information)\s*\("),
    "signed_request": re.compile(r"\bSIGNED\b|signed\s*=\s*True", re.IGNORECASE),
}


def _inside_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
        return True
    except Exception:
        return False


def imports_from_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports


def _matches_any(patterns: tuple[re.Pattern[str], ...], name: str) -> bool:
    return any(pattern.match(name) for pattern in patterns)


def _pattern_hits(source: str, patterns: dict[str, re.Pattern[str]]) -> list[str]:
    return sorted(name for name, pattern in patterns.items() if pattern.search(source))


def classify_script(path: Path) -> dict[str, Any]:
    name = path.name
    if name in ALLOWED_EXCEPTIONS:
        return {
            "path": str(path),
            "classification": "paper_safe",
            "reason": "allowed_exception",
            "can_place_live_orders": False,
            "live_permission": False,
        }
    if not path.exists() or path.suffix != ".py":
        return {"path": str(path), "classification": "unknown", "reason": "not_python_or_missing", "can_place_live_orders": False, "live_permission": False}
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
        imports = imports_from_file(path)
    except Exception as exc:
        return {
            "path": str(path),
            "classification": "unknown",
            "error": f"parse_error:{str(exc)[:120]}",
            "can_place_live_orders": False,
            "live_permission": False,
        }

    forbidden_imports = [item for item in imports if any(item == prefix or item.startswith(prefix + ".") for prefix in FORBIDDEN_IMPORT_PREFIXES)]
    forbidden_calls = _pattern_hits(source, FORBIDDEN_CALL_PATTERNS)
    private_hits = _pattern_hits(source, PRIVATE_ACCOUNT_PATTERNS)
    blocked_filename = _matches_any(BLOCKED_FILENAME_PATTERNS, name)
    readonly_private_filename = _matches_any(READONLY_PRIVATE_FILENAME_PATTERNS, name)

    if forbidden_calls or (blocked_filename and name not in ALLOWED_EXCEPTIONS):
        classification = "blocked_legacy_live"
    elif forbidden_imports and (blocked_filename or private_hits):
        classification = "blocked_legacy_live"
    elif forbidden_imports or private_hits or readonly_private_filename:
        classification = "readonly_private"
    else:
        classification = "paper_safe"

    return {
        "path": str(path),
        "classification": classification,
        "forbidden_imports": forbidden_imports,
        "forbidden_calls": forbidden_calls,
        "private_account_hits": private_hits,
        "blocked_filename": blocked_filename,
        "quarantine_enforced": _inside_root(path) and (ROOT / "sitecustomize.py").exists() and (ROOT / "legacy_live_blocker.py").exists(),
        "can_place_live_orders": False,
        "live_permission": False,
        "paper_action_allowed": classification == "paper_safe",
        "authoritative_for_paper": classification == "paper_safe",
    }


def scan_import_guard(paths: list[Path], output_path: Path = IMPORT_GUARD_LATEST) -> dict[str, Any]:
    violations = []
    scanned = []
    manifest = []
    quarantined_count = 0
    for path in paths:
        if not path.exists() or path.suffix != ".py":
            continue
        entry = classify_script(path)
        scanned.append(str(path))
        manifest.append(entry)
        quarantined = entry.get("classification") in {"blocked_legacy_live", "readonly_private"} and bool(entry.get("quarantine_enforced"))
        if quarantined:
            quarantined_count += 1
        if entry.get("error"):
            violations.append({"path": str(path), "error": entry["error"], "classification": entry["classification"]})
            continue
        if entry["classification"] == "unknown":
            violations.append({"path": str(path), "classification": entry["classification"], "reason": entry.get("reason", "unknown_script")})
        elif entry["classification"] == "blocked_legacy_live" and not quarantined:
            violations.append({k: v for k, v in entry.items() if k in {"path", "classification", "forbidden_imports", "forbidden_calls", "private_account_hits", "blocked_filename"}})
        elif entry.get("forbidden_imports") and not quarantined:
            violations.append({"path": str(path), "classification": entry["classification"], "forbidden_imports": entry["forbidden_imports"]})
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "ok": not violations,
        "scanned_count": len(scanned),
        "quarantined_count": quarantined_count,
        "violations": violations,
        "script_manifest": manifest,
        "can_place_live_orders": False,
        "live_permission": False,
    }
    write_json_atomic(output_path, payload)
    return payload


def discover_python_files(root: Path = ROOT) -> list[Path]:
    skipped = {".git", "venv", "__pycache__", ".pytest_cache", "node_modules", "tests"}
    return [path for path in root.rglob("*.py") if not any(part in skipped for part in path.parts)]


def scan_repo_import_guard(root: Path = ROOT, output_path: Path = IMPORT_GUARD_LATEST) -> dict[str, Any]:
    return scan_import_guard(discover_python_files(root), output_path=output_path)
