"""Experiment registry and walk-forward-safe hypothesis tracking."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from agent_work_queue import enqueue_job
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
EXPERIMENTS_JSONL = MEMORY_DIR / "experiments.jsonl"
EXPERIMENTS_LATEST = MEMORY_DIR / "experiments_latest.json"
EXPERIMENT_SWARM_DB = ROOT / "state" / "experiment_swarm.sqlite"

EXPERIMENT_JOB_SCHEMA = "experiment_job.v1"
MAX_CHAIN_DEPTH = 3
DEFAULT_MAX_RETRIES = 2
DEFAULT_ARTIFACT_QUOTA_BYTES = 2_000_000
DEFAULT_TIMEOUT_SECONDS = 120

def canonical_hash(value: Any, prefix: str) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def experiment_id(hypothesis: str, setup_id: str) -> str:
    return "exp_" + hashlib.sha256(f"{setup_id}:{hypothesis}".encode("utf-8")).hexdigest()[:20]

def connect_swarm(db_path: Path = EXPERIMENT_SWARM_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_jobs (
            experiment_id TEXT PRIMARY KEY,
            experiment_family_id TEXT NOT NULL,
            variant_hash TEXT NOT NULL,
            data_window_hash TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            setup_id TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            locked_by TEXT,
            locked_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 2,
            error TEXT,
            UNIQUE(experiment_family_id, variant_hash, data_window_hash, config_hash)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_results (
            result_id TEXT PRIMARY KEY,
            experiment_id TEXT NOT NULL,
            experiment_family_id TEXT NOT NULL,
            variant_hash TEXT NOT NULL,
            data_window_hash TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            artifacts_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(experiment_id, variant_hash, data_window_hash, config_hash)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            experiment_family_id TEXT NOT NULL,
            setup_id TEXT NOT NULL,
            hypothesis TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_jobs_status ON experiment_jobs(status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_jobs_family ON experiment_jobs(experiment_family_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_results_family ON experiment_results(experiment_family_id, status)")
    return conn

def experiment_family_id(setup_id: str, hypothesis: str) -> str:
    return canonical_hash({"setup_id": setup_id, "hypothesis": hypothesis}, "expfam")

def build_experiment_job(
    *,
    hypothesis: str,
    setup_id: str,
    variant: dict[str, Any],
    data_window: dict[str, Any],
    config: dict[str, Any],
    setup_contract_id: str | None = None,
    setup_version: str | None = None,
    setup_contract_hash: str | None = None,
    actor_id: str = "system",
    client_id: str = "experiment_swarm",
    capability_id: str = "paper_replay_experiment",
    parent_call_id: str | None = None,
    chain_depth: int = 0,
    root_budget_id: str = "paper_experiment_root",
    budget_reservation_id: str | None = None,
    alpha_budget: float = 0.05,
    priority_budget: int = 50,
    resource_budget: dict[str, Any] | None = None,
    source_event_ids: list[str] | None = None,
    max_effect: str = "paper_experiment_only",
) -> dict[str, Any]:
    family_id = experiment_family_id(setup_id, hypothesis)
    variant_hash = canonical_hash(variant, "variant")
    data_window_hash = canonical_hash(data_window, "window")
    config_hash = canonical_hash(config, "config")
    exp_id = canonical_hash({"family": family_id, "variant": variant_hash, "window": data_window_hash, "config": config_hash}, "expjob")
    resource = {"timeout_seconds": DEFAULT_TIMEOUT_SECONDS, "artifact_quota_bytes": DEFAULT_ARTIFACT_QUOTA_BYTES, "estimated_cost_usd": 0.0, **(resource_budget or {})}
    reservation_id = budget_reservation_id or canonical_hash({"root_budget_id": root_budget_id, "experiment_id": exp_id}, "budget")
    return {
        "schema_version": EXPERIMENT_JOB_SCHEMA,
        "experiment_id": exp_id,
        "experiment_family_id": family_id,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "source_event_ids": source_event_ids or [],
        "provenance_id": canonical_hash({"source_event_ids": source_event_ids or [], "family": family_id}, "prov"),
        "variant_hash": variant_hash,
        "data_window_hash": data_window_hash,
        "config_hash": config_hash,
        "setup_id": setup_id,
        "setup_contract_id": setup_contract_id or f"{setup_id}:contract",
        "setup_version": setup_version or "v1",
        "setup_contract_hash": setup_contract_hash,
        "hypothesis": hypothesis,
        "variant": variant,
        "data_window": data_window,
        "config": config,
        "alpha_budget": alpha_budget,
        "preregistered_pass_rule": "expectancy_after_fees>0 and p_value<=alpha/variants",
        "status": "queued",
        "actor_id": actor_id,
        "client_id": client_id,
        "capability_id": capability_id,
        "parent_call_id": parent_call_id,
        "chain_depth": chain_depth,
        "max_effect": max_effect,
        "root_budget_id": root_budget_id,
        "budget_reservation_id": reservation_id,
        "resource_budget": resource,
        "priority_budget": priority_budget,
        "estimated_cost": resource.get("estimated_cost_usd", 0.0),
        "actual_cost": 0.0,
        "api_calls": 0,
        "llm_tokens": 0,
        "max_retries": DEFAULT_MAX_RETRIES,
        "can_place_live_orders": False,
    }

def validate_experiment_job(job: dict[str, Any], *, db_path: Path = EXPERIMENT_SWARM_DB, max_jobs_per_actor_family: int = 100) -> list[str]:
    errors: list[str] = []
    if job.get("schema_version") != EXPERIMENT_JOB_SCHEMA:
        errors.append("invalid_experiment_job_schema")
    for field in ("experiment_id", "experiment_family_id", "variant_hash", "data_window_hash", "config_hash", "setup_id", "hypothesis"):
        if not job.get(field):
            errors.append(f"missing_{field}")
    for field in ("setup_contract_id", "setup_version", "setup_contract_hash"):
        if not job.get(field):
            errors.append("unknown_setup_contract_hash" if field == "setup_contract_hash" else f"missing_{field}")
    if job.get("max_effect") not in {"paper_experiment_only", "analysis_only", "shadow_only"}:
        errors.append("invalid_max_effect")
    if int(job.get("chain_depth") or 0) > MAX_CHAIN_DEPTH:
        errors.append("max_chain_depth_exceeded")
    if not job.get("root_budget_id") or not job.get("budget_reservation_id"):
        errors.append("missing_cost_reservation")
    resource = job.get("resource_budget") if isinstance(job.get("resource_budget"), dict) else {}
    if int(resource.get("timeout_seconds") or 0) <= 0:
        errors.append("invalid_timeout_seconds")
    if int(resource.get("artifact_quota_bytes") or 0) <= 0:
        errors.append("invalid_artifact_quota")
    if max_jobs_per_actor_family >= 0 and job.get("actor_id") and job.get("experiment_family_id"):
        with connect_swarm(db_path) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM experiment_jobs WHERE experiment_family_id=? AND status IN ('queued','running')",
                (job["experiment_family_id"],),
            ).fetchall()
        count = 0
        for row in rows:
            try:
                payload = json.loads(row[0])
            except Exception:
                payload = {}
            if payload.get("actor_id") == job.get("actor_id"):
                count += 1
        if count >= max_jobs_per_actor_family:
            errors.append("actor_family_quota_exceeded")
    return sorted(set(errors))

def write_swarm_latest(db_path: Path = EXPERIMENT_SWARM_DB, output_path: Path = EXPERIMENTS_LATEST) -> dict[str, Any]:
    with connect_swarm(db_path) as conn:
        job_rows = conn.execute("SELECT status, COUNT(*) FROM experiment_jobs GROUP BY status").fetchall()
        result_rows = conn.execute("SELECT status, COUNT(*) FROM experiment_results GROUP BY status").fetchall()
        total_jobs = conn.execute("SELECT COUNT(*) FROM experiment_jobs").fetchone()[0]
        total_results = conn.execute("SELECT COUNT(*) FROM experiment_results").fetchone()[0]
        recent = conn.execute("SELECT payload_json FROM experiment_jobs ORDER BY created_at DESC LIMIT 30").fetchall()
    by_status = {status: count for status, count in job_rows}
    result_by_status = {status: count for status, count in result_rows}
    failed = by_status.get("failed", 0) + by_status.get("dlq", 0)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "experiment_count": total_jobs,
        "result_count": total_results,
        "by_status": by_status,
        "result_by_status": result_by_status,
        "fail_rate": round(failed / total_jobs, 6) if total_jobs else 0.0,
        "coverage": round(total_results / total_jobs, 6) if total_jobs else 0.0,
        "rows": [json.loads(row[0]) for row in reversed(recent)],
        "source": "sqlite_experiment_swarm",
        "can_place_live_orders": False,
    }
    write_json_atomic(output_path, payload)
    return payload

def enqueue_experiment_job(
    job: dict[str, Any],
    *,
    db_path: Path = EXPERIMENT_SWARM_DB,
    queue_db_path: Path | None = None,
    latest_path: Path = EXPERIMENTS_LATEST,
    max_jobs_per_actor_family: int = 100,
) -> dict[str, Any]:
    errors = validate_experiment_job(job, db_path=db_path, max_jobs_per_actor_family=max_jobs_per_actor_family)
    now = utc_now()
    payload = {**job, "updated_at": now, "status": "rejected" if errors else "queued", "errors": errors}
    with connect_swarm(db_path) as conn:
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO experiment_jobs(
                experiment_id, experiment_family_id, variant_hash, data_window_hash, config_hash,
                setup_id, status, payload_json, created_at, updated_at, max_retries, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["experiment_id"],
                payload["experiment_family_id"],
                payload["variant_hash"],
                payload["data_window_hash"],
                payload["config_hash"],
                payload["setup_id"],
                payload["status"],
                json.dumps(payload, ensure_ascii=True, sort_keys=True),
                payload.get("created_at") or now,
                now,
                int(payload.get("max_retries") or DEFAULT_MAX_RETRIES),
                ";".join(errors) if errors else None,
            ),
        )
        inserted = conn.total_changes > before
    queued = None
    if not errors and inserted:
        kwargs = {"db_path": queue_db_path} if queue_db_path is not None else {}
        queued = enqueue_job("experiment_replay", {"experiment_id": payload["experiment_id"], "experiment_job": payload}, priority=int(payload.get("priority_budget") or 50), job_id=f"job_{payload['experiment_id']}", **kwargs)
    write_swarm_latest(db_path, latest_path)
    return {"ok": not errors, "inserted": inserted, "experiment_id": payload["experiment_id"], "errors": errors, "queue": queued, "can_place_live_orders": False}

def claim_experiment_job(worker_id: str, *, db_path: Path = EXPERIMENT_SWARM_DB) -> dict[str, Any] | None:
    now = utc_now()
    with connect_swarm(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT experiment_id, payload_json FROM experiment_jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1").fetchone()
        if not row:
            conn.commit()
            return None
        exp_id, payload_json = row
        conn.execute("UPDATE experiment_jobs SET status='running', locked_by=?, locked_at=?, updated_at=? WHERE experiment_id=? AND status='queued'", (worker_id, now, now, exp_id))
        conn.commit()
    payload = json.loads(payload_json)
    return {**payload, "status": "running", "locked_by": worker_id, "locked_at": now}

def complete_experiment_job(experiment_id_value: str, *, ok: bool, error: str | None = None, db_path: Path = EXPERIMENT_SWARM_DB) -> dict[str, Any]:
    now = utc_now()
    with connect_swarm(db_path) as conn:
        row = conn.execute("SELECT attempt_count, max_retries, payload_json FROM experiment_jobs WHERE experiment_id=?", (experiment_id_value,)).fetchone()
        if not row:
            return {"ok": False, "error": "experiment_job_missing"}
        attempt_count, max_retries, payload_json = row
        payload = json.loads(payload_json)
        if ok:
            status = "done"
            next_attempt = attempt_count
        else:
            next_attempt = int(attempt_count or 0) + 1
            status = "dlq" if next_attempt > int(max_retries or DEFAULT_MAX_RETRIES) else "queued"
        payload = {**payload, "status": status, "updated_at": now, "error": error, "attempt_count": next_attempt}
        conn.execute(
            "UPDATE experiment_jobs SET status=?, attempt_count=?, locked_by=NULL, locked_at=NULL, updated_at=?, error=?, payload_json=? WHERE experiment_id=?",
            (status, next_attempt, now, error, json.dumps(payload, ensure_ascii=True, sort_keys=True), experiment_id_value),
        )
    write_swarm_latest(db_path)
    return {"ok": True, "experiment_id": experiment_id_value, "status": status, "attempt_count": next_attempt}

def family_variant_count(conn: sqlite3.Connection, family_id: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM experiment_jobs WHERE experiment_family_id=?", (family_id,)).fetchone()[0] or 1)

def record_experiment_result(job: dict[str, Any], metrics: dict[str, Any], artifacts: dict[str, Any] | None = None, *, db_path: Path = EXPERIMENT_SWARM_DB) -> dict[str, Any]:
    with connect_swarm(db_path) as conn:
        variant_count = max(1, family_variant_count(conn, str(job.get("experiment_family_id"))))
        alpha = float(job.get("alpha_budget") or 0.05)
        corrected_alpha = alpha / variant_count
        p_value = float(metrics.get("p_value") if metrics.get("p_value") is not None else 1.0)
        expectancy = float(metrics.get("expectancy_after_fees") or 0.0)
        status = "passed" if expectancy > 0 and p_value <= corrected_alpha else "failed"
        result_id = canonical_hash({"experiment_id": job.get("experiment_id"), "variant_hash": job.get("variant_hash"), "data_window_hash": job.get("data_window_hash"), "config_hash": job.get("config_hash")}, "expres")
        result = {
            "schema_version": SCHEMA_VERSION,
            "result_id": result_id,
            "experiment_id": job.get("experiment_id"),
            "experiment_family_id": job.get("experiment_family_id"),
            "variant_hash": job.get("variant_hash"),
            "data_window_hash": job.get("data_window_hash"),
            "config_hash": job.get("config_hash"),
            "status": status,
            "metrics": metrics,
            "artifacts": artifacts or {},
            "family_variant_count": variant_count,
            "corrected_alpha": corrected_alpha,
            "created_at": utc_now(),
            "can_place_live_orders": False,
        }
        conn.execute(
            """
            INSERT OR IGNORE INTO experiment_results(
                result_id, experiment_id, experiment_family_id, variant_hash, data_window_hash,
                config_hash, status, metrics_json, artifacts_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id,
                result["experiment_id"],
                result["experiment_family_id"],
                result["variant_hash"],
                result["data_window_hash"],
                result["config_hash"],
                status,
                json.dumps(metrics, ensure_ascii=True, sort_keys=True),
                json.dumps(artifacts or {}, ensure_ascii=True, sort_keys=True),
                result["created_at"],
            ),
        )
    write_swarm_latest(db_path)
    return result

def record_hypothesis(hypothesis: str, setup_id: str, status: str = "proposed", reason: str | None = None, *, db_path: Path = EXPERIMENT_SWARM_DB) -> dict[str, Any]:
    family_id = experiment_family_id(setup_id, hypothesis)
    row = {
        "schema_version": SCHEMA_VERSION,
        "hypothesis_id": canonical_hash({"setup_id": setup_id, "hypothesis": hypothesis}, "hyp"),
        "experiment_family_id": family_id,
        "setup_id": setup_id,
        "hypothesis": hypothesis,
        "status": status,
        "reason": reason,
        "created_at": utc_now(),
        "can_place_live_orders": False,
    }
    with connect_swarm(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO experiment_hypotheses(hypothesis_id, experiment_family_id, setup_id, hypothesis, status, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["hypothesis_id"], family_id, setup_id, hypothesis, status, reason, row["created_at"]),
        )
    return row


def windows_overlap(a: dict[str, str], b: dict[str, str]) -> bool:
    a0, a1 = parse_utc(a.get("start")), parse_utc(a.get("end"))
    b0, b1 = parse_utc(b.get("start")), parse_utc(b.get("end"))
    if not all([a0, a1, b0, b1]):
        return True
    return a0 <= b1 and b0 <= a1


def propose_experiment(hypothesis: str, setup_id: str, train_window: dict[str, str], test_window: dict[str, str], success_metric: str = "expectancy_after_fees", path: Path = EXPERIMENTS_JSONL) -> dict[str, Any]:
    errors = []
    if windows_overlap(train_window, test_window):
        errors.append("train_test_windows_overlap")
    row = {"schema_version": SCHEMA_VERSION, "experiment_id": experiment_id(hypothesis, setup_id), "hypothesis": hypothesis, "setup_id": setup_id, "train_window": train_window, "test_window": test_window, "success_metric": success_metric, "status": "rejected" if errors else "proposed", "errors": errors, "created_at": utc_now()}
    append_jsonl_once(path, row, "experiment_id")
    write_latest(path)
    return row


def evaluate_experiment(row: dict[str, Any], train_metrics: dict[str, Any], test_metrics: dict[str, Any], min_test_trades: int = 20) -> dict[str, Any]:
    metric = row.get("success_metric", "expectancy_after_fees")
    errors = []
    if int(test_metrics.get("trades") or 0) < min_test_trades:
        errors.append("insufficient_out_of_sample_trades")
    if float(test_metrics.get(metric) or 0.0) <= 0:
        errors.append("test_metric_not_positive")
    if float(train_metrics.get(metric) or 0.0) > 0 and float(test_metrics.get(metric) or 0.0) <= 0:
        errors.append("train_only_edge")
    status = "passed" if not errors else "failed"
    return {**row, "status": status, "evaluated_at": utc_now(), "train_metrics": train_metrics, "test_metrics": test_metrics, "errors": errors}

def latest_by_experiment(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered: list[str] = []
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("experiment_id") or "")
        if not key:
            continue
        if key not in latest:
            ordered.append(key)
        latest[key] = row
    return [latest[key] for key in ordered]


def write_latest(path: Path = EXPERIMENTS_JSONL, output_path: Path = EXPERIMENTS_LATEST) -> dict[str, Any]:
    rows = latest_by_experiment(read_jsonl(path))
    by_status: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "experiment_count": len(rows), "by_status": by_status, "rows": rows[-30:], "can_place_live_orders": False}
    write_json_atomic(output_path, payload)
    return payload
