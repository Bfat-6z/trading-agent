"""Local quality-gate harness for Phase A deterministic modules."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from environment_report import write_environment_report
from live_permission_firewall import evaluate_live_permission
from runtime_config import evaluate_mode, load_runtime_config, write_effective_config
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
QUALITY_PATH = STATE_DIR / "quality_gate_latest.json"


def safety_smoke() -> dict:
    config = evaluate_mode(load_runtime_config())
    firewall = evaluate_live_permission({"action": "create_order", "mode": "live", "symbol": "BTCUSDT"}, config)
    errors: list[str] = []
    if firewall.get("allowed"):
        errors.append("firewall_allowed_live_order")
    if config.get("feature_flags", {}).get("live_orders"):
        errors.append("live_orders_flag_enabled")
    return {"ok": not errors, "errors": errors, "config_mode": config.get("mode"), "firewall_reason": firewall.get("reason")}


def artifact_smoke() -> dict:
    checks = {
        "runtime_config_effective": STATE_DIR / "runtime_config_effective.json",
        "environment_latest": STATE_DIR / "environment_latest.json",
    }
    rows = {}
    for name, path in checks.items():
        rows[name] = {"exists": path.exists(), "valid_json": bool(read_json(path, default={})) if path.exists() else False}
    return {"ok": all(item["exists"] and item["valid_json"] for item in rows.values()), "artifacts": rows}


def run_pytest(args: list[str] | None = None) -> dict:
    cmd = [sys.executable, "-m", "pytest"] + (args or ["tests", "-q"])
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=300)
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "cmd": cmd, "stdout_tail": proc.stdout[-2000:], "stderr_tail": proc.stderr[-2000:]}


def run_quality_gate(run_tests: bool = False, pytest_args: list[str] | None = None, output_path: Path = QUALITY_PATH) -> dict:
    env = write_environment_report()
    write_effective_config()
    safety = safety_smoke()
    artifacts = artifact_smoke()
    tests = run_pytest(pytest_args) if run_tests else {"ok": None, "skipped": True}
    errors: list[str] = []
    if not safety["ok"]:
        errors.extend(safety["errors"])
    if run_tests and not tests["ok"]:
        errors.append("pytest_failed")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "ok": not errors,
        "errors": errors,
        "environment_updated_at": env.get("updated_at"),
        "safety": safety,
        "artifacts": artifacts,
        "tests": tests,
    }
    write_json_atomic(output_path, payload)
    return payload


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local Phase A quality gate")
    parser.add_argument("--run-tests", action="store_true")
    args, pytest_args = parser.parse_known_args(list(argv) if argv is not None else None)
    args.pytest_args = [item for item in pytest_args if item != "--"]
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_quality_gate(run_tests=args.run_tests, pytest_args=args.pytest_args or None)
    print(result)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
