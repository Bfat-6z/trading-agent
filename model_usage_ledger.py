"""Local model usage ledger for governance and cost visibility."""
from __future__ import annotations

from pathlib import Path
import hashlib
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_jsonl, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
USAGE_HISTORY = MEMORY_DIR / "model_usage_history.jsonl"
USAGE_LATEST = MEMORY_DIR / "model_usage_latest.json"

MODEL_COST_PER_1K = {
    "cx/gpt-5.5": {"input": 0.0, "output": 0.0},
    "gpt-5.5": {"input": 0.0, "output": 0.0},
    "gpt-5-mini": {"input": 0.0, "output": 0.0},
}

def estimate_tokens(text: Any) -> int:
    return max(1, len(str(text)) // 4) if text not in (None, "") else 0

def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = MODEL_COST_PER_1K.get(model, {"input": 0.0, "output": 0.0})
    return round((input_tokens / 1000) * rates["input"] + (output_tokens / 1000) * rates["output"], 8)

def stable_request_id(job_type: str, model: str, prompt: Any, response: Any) -> str:
    raw = f"{job_type}:{model}:{str(prompt)[:500]}:{str(response)[:500]}"
    return "model_req_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def record_model_usage(
    job_type: str,
    model: str,
    provider: str,
    prompt: Any = "",
    response: Any = "",
    status: str = "ok",
    history_path: Path = USAGE_HISTORY,
    latest_path: Path = USAGE_LATEST,
    *,
    request_id: str | None = None,
    latency_ms: int | None = None,
    actual_response_model_id: str | None = None,
    route_reason: str | None = None,
    fallback_reason: str | None = None,
    quality_gate_ok: bool | None = None,
) -> dict[str, Any]:
    input_tokens = estimate_tokens(prompt)
    output_tokens = estimate_tokens(response)
    row = {
        "schema_version": SCHEMA_VERSION,
        "ts": utc_now(),
        "job_type": job_type,
        "provider": provider,
        "model": model,
        "actual_response_model_id": actual_response_model_id or model,
        "request_id": request_id or stable_request_id(job_type, model, prompt, response),
        "latency_ms": latency_ms,
        "route_reason": route_reason,
        "fallback_reason": fallback_reason,
        "quality_gate_ok": quality_gate_ok,
        "status": status,
        "input_tokens_est": input_tokens,
        "output_tokens_est": output_tokens,
        "cost_usd_est": estimate_cost(model, input_tokens, output_tokens),
        "can_place_live_orders": False,
    }
    append_jsonl(history_path, row)
    summary = summarize_model_usage(history_path)
    write_json_atomic(latest_path, summary)
    return row

def summarize_model_usage(history_path: Path = USAGE_HISTORY) -> dict[str, Any]:
    rows = read_jsonl(history_path, limit=1000)
    total_cost = sum(float(row.get("cost_usd_est") or 0.0) for row in rows)
    total_input = sum(int(row.get("input_tokens_est") or 0) for row in rows)
    total_output = sum(int(row.get("output_tokens_est") or 0) for row in rows)
    by_job: dict[str, int] = {}
    for row in rows:
        by_job[str(row.get("job_type") or "unknown")] = by_job.get(str(row.get("job_type") or "unknown"), 0) + 1
    return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "call_count": len(rows), "input_tokens_est": total_input, "output_tokens_est": total_output, "cost_usd_est": round(total_cost, 8), "by_job": by_job, "can_place_live_orders": False}
