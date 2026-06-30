"""Fail-closed runtime blocker for legacy live/private scripts."""
from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

from agent_data_contracts import SCHEMA_VERSION
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
DENIAL_HISTORY = MEMORY_DIR / "legacy_script_denials.jsonl"
DENIAL_LATEST = MEMORY_DIR / "legacy_script_denial_latest.json"

ALLOWED_EXCEPTIONS = {"live_permission_firewall.py", "legacy_live_blocker.py", "sitecustomize.py", "test_harness.py"}
BLOCKED_FILENAME_PATTERNS = (
    re.compile(r"^execute_.*\.py$", re.IGNORECASE),
    re.compile(r".*live.*\.py$", re.IGNORECASE),
)
PRIVATE_FILENAME_PATTERNS = (
    re.compile(r"^check_.*live.*\.py$", re.IGNORECASE),
    re.compile(r".*(account|balance|position).*\.py$", re.IGNORECASE),
)
FORBIDDEN_SOURCE_PATTERNS = {
    "futures_create_order": re.compile(r"\bfutures_create_order\s*\("),
    "futures_change_leverage": re.compile(r"\bfutures_change_leverage\s*\("),
    "_request_futures_api_signed": re.compile(r"_request_futures_api\s*\([^)]*(True|signed\s*=\s*True)", re.IGNORECASE | re.DOTALL),
    "raw_order_http": re.compile(r"requests\.(post|put|delete)\s*\([^)]*(order|leverage|withdraw|transfer)", re.IGNORECASE | re.DOTALL),
    "subprocess_live_script": re.compile(r"subprocess\.(run|Popen|call)\s*\([^)]*(execute_|live)", re.IGNORECASE | re.DOTALL),
}
PRIVATE_SOURCE_PATTERNS = {
    "private_account_read": re.compile(r"\b(account|balance|position|futures_account|futures_position_information)\s*\("),
    "signed_request": re.compile(r"\bSIGNED\b|signed\s*=\s*True", re.IGNORECASE),
}


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="ignore")).hexdigest()[:16]


def _pattern_hits(source: str, patterns: dict[str, re.Pattern[str]]) -> list[str]:
    return sorted(name for name, pattern in patterns.items() if pattern.search(source))


def _inside_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
        return True
    except Exception:
        return False


def classify_legacy_script(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    name = resolved.name
    if name in ALLOWED_EXCEPTIONS:
        return {"classification": "paper_safe", "reason": "allowed_exception", "path": str(resolved)}
    if resolved.suffix != ".py" or not resolved.exists():
        return {"classification": "unknown", "reason": "missing_or_not_python", "path": str(resolved)}
    source = resolved.read_text(encoding="utf-8", errors="ignore")
    forbidden_calls = _pattern_hits(source, FORBIDDEN_SOURCE_PATTERNS)
    private_hits = _pattern_hits(source, PRIVATE_SOURCE_PATTERNS)
    blocked_name = any(pattern.match(name) for pattern in BLOCKED_FILENAME_PATTERNS)
    private_name = any(pattern.match(name) for pattern in PRIVATE_FILENAME_PATTERNS)
    if forbidden_calls or blocked_name:
        classification = "blocked_legacy_live"
    elif private_hits or private_name:
        classification = "readonly_private"
    else:
        classification = "paper_safe"
    return {
        "classification": classification,
        "path": str(resolved),
        "forbidden_calls": forbidden_calls,
        "private_account_hits": private_hits,
        "blocked_filename": blocked_name,
        "private_filename": private_name,
        "quarantine_enforced": _inside_root(resolved) and (ROOT / "sitecustomize.py").exists(),
        "can_place_live_orders": False,
        "live_permission": False,
    }


def denial_event(path: Path, classification: dict[str, Any], operation: str) -> dict[str, Any]:
    base = {
        "schema_version": SCHEMA_VERSION,
        "ts": utc_now(),
        "event": "legacy_script_blocked",
        "operation": operation,
        "path": str(path.resolve()),
        "classification": classification.get("classification"),
        "reason": "blocked_by_phase_00_firewall",
        "can_place_live_orders": False,
        "live_permission": False,
        "paper_action_allowed": False,
    }
    base["denial_id"] = "legacy_denial_" + _fingerprint(base)
    base["signature"] = _fingerprint({"denial_id": base["denial_id"], "path": base["path"], "ts": base["ts"]})
    return base


def record_denial(event: dict[str, Any]) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    with DENIAL_HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    DENIAL_LATEST.write_text(json.dumps(event, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def should_block(path: Path) -> tuple[bool, dict[str, Any]]:
    classification = classify_legacy_script(path)
    return classification.get("classification") in {"blocked_legacy_live", "readonly_private", "unknown"}, classification


def block_file_if_legacy(path: str | Path, operation: str = "module_load", exit_fn: Callable[[int], Any] | None = None) -> dict[str, Any] | None:
    script = Path(path)
    blocked, classification = should_block(script)
    if not blocked:
        return None
    event = denial_event(script, classification, operation)
    record_denial(event)
    print("legacy_script_blocked: Phase 00 paper-only firewall denied this script.", file=sys.stderr)
    if exit_fn is not None:
        exit_fn(78)
        return event
    raise SystemExit(78)


def block_if_legacy_entrypoint(argv: list[str] | None = None, exit_fn: Callable[[int], Any] | None = None) -> dict[str, Any] | None:
    argv = argv if argv is not None else sys.argv
    if not argv:
        return None
    script = Path(argv[0])
    if not script.exists():
        return None
    return block_file_if_legacy(script, "direct_exec", exit_fn=exit_fn)


class LegacyImportBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname in {"legacy_live_blocker", "sitecustomize", "security_import_guard"}:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        origin = getattr(spec, "origin", None)
        if not spec or not origin or not str(origin).endswith(".py"):
            return spec
        module_path = Path(origin)
        if not _inside_root(module_path):
            return spec
        blocked, classification = should_block(module_path)
        if not blocked:
            return spec
        event = denial_event(module_path, classification, "import")
        record_denial(event)
        raise ImportError(f"legacy_script_blocked:{module_path.name}")


def install_import_guard() -> None:
    if os.environ.get("TRADING_AGENT_DISABLE_LEGACY_BLOCKER") == "1":
        return
    if not any(isinstance(finder, LegacyImportBlocker) for finder in sys.meta_path):
        sys.meta_path.insert(0, LegacyImportBlocker())


def install() -> None:
    install_import_guard()
    block_if_legacy_entrypoint()
