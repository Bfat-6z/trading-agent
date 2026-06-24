"""Reproducibility report for the local trading-agent runtime."""
from __future__ import annotations

import argparse
import locale
import os
import platform
import sys
from importlib import metadata
from pathlib import Path
from typing import Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from live_permission_firewall import redact_secrets
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
ENVIRONMENT_LATEST = STATE_DIR / "environment_latest.json"

IMPORTANT_PACKAGES = [
    "pytest",
    "requests",
    "numpy",
    "pandas",
    "flask",
    "fastapi",
    "python-binance",
    "ccxt",
]


def package_versions(names: list[str] = IMPORTANT_PACKAGES) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def file_hash_status() -> dict[str, dict[str, str | bool]]:
    paths = [ROOT / "requirements.txt", ROOT / "pyproject.toml", ROOT / "tradingagents_crypto_src" / "uv.lock"]
    return {str(path.relative_to(ROOT)): {"exists": path.exists()} for path in paths}


def collect_environment() -> dict:
    env_keys = ["TRADING_AGENT_MODE", "TRADING_AGENT_LIVE_ORDERS", "OPENAI_BASE_URL", "OPENAI_MODEL", "NINE_ROUTER_MODEL"]
    safe_env = {key: redact_secrets(os.environ.get(key, "")) for key in env_keys if key in os.environ}
    writable = {}
    for path in (STATE_DIR, STATE_DIR / "agent_memory"):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="ascii")
            probe.unlink(missing_ok=True)
            writable[str(path.relative_to(ROOT))] = True
        except Exception:
            writable[str(path.relative_to(ROOT))] = False
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "timezone": os.environ.get("TZ") or "system-local",
        "locale": locale.getlocale(),
        "cwd": str(Path.cwd()),
        "root": str(ROOT),
        "packages": package_versions(),
        "dependency_files": file_hash_status(),
        "env_redacted": safe_env,
        "writable_paths": writable,
    }


def write_environment_report(path: Path = ENVIRONMENT_LATEST) -> dict:
    report = collect_environment()
    write_json_atomic(path, report)
    return report


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write local environment reproducibility report")
    parser.add_argument("--output", default=str(ENVIRONMENT_LATEST))
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    report = write_environment_report(Path(args.output))
    print({"updated_at": report["updated_at"], "python_executable": report["python_executable"], "platform": report["platform"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
