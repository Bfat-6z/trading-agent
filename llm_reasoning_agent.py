"""LLM reasoning loop for the trading agent.

This agent connects the configured large model (9router/OpenRouter/OpenAI/etc.)
to the learning loop. It is read-only: it critiques memory, proposes
hypotheses/curriculum/risk tightening, and never places orders.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from event_store import safe_append_event, safe_append_snapshot, safe_upsert_heartbeat
from data_trust import prepare_llm_egress
from llm_output_quality_gate import sanitize_output
from model_usage_ledger import record_model_usage
from model_router import route_model
from llm_council import model_budget_allowed
from trace_eval import build_prompt_trace, save_prompt_trace

ROOT = Path(__file__).resolve().parent
CRYPTO_SRC = ROOT / "tradingagents_crypto_src"
if str(CRYPTO_SRC) not in sys.path:
    sys.path.insert(0, str(CRYPTO_SRC))

STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"

MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
SCALP_LOG = STATE_DIR / "scalp_autotrader.jsonl"
BIAS_PATH = MEMORY_DIR / "execution_bias.json"
NEWS_LATEST = MEMORY_DIR / "news_latest.json"
SHADOW_PERFORMANCE = MEMORY_DIR / "shadow_performance_latest.json"
SELF_IMPROVEMENT = MEMORY_DIR / "self_improvement_latest.json"
DAILY_EXAM = MEMORY_DIR / "daily_exam_latest.json"
COGNITIVE_LATEST = MEMORY_DIR / "cognitive_state_latest.json"
REASONING_TRACE = MEMORY_DIR / "reasoning_trace_latest.json"
SETUP_SKILLS = MEMORY_DIR / "setup_skills.json"
BELIEF_LEDGER = MEMORY_DIR / "belief_ledger.json"
SEMANTIC_MEMORY = MEMORY_DIR / "semantic_memory.json"

LATEST_JSON = MEMORY_DIR / "llm_reasoning_latest.json"
HISTORY_JSONL = MEMORY_DIR / "llm_reasoning_history.jsonl"
REPORT_MD = MEMORY_DIR / "llm_reasoning_latest.md"
HEARTBEAT_PATH = STATE_DIR / "llm_reasoning_agent_heartbeat.json"
PID_FILE = STATE_DIR / "llm_reasoning_agent.pid"
STOP_FILE = STATE_DIR / "STOP_LLM_REASONING_AGENT"

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")

def read_jsonl_tail(path: Path, max_lines: int = 80) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]:
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
        except Exception:
            continue
    return rows

def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value

def compact(value: object, max_chars: int = 22000) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...TRUNCATED"

def collect_context(max_log_lines: int = 80) -> dict:
    setup = read_json(SETUP_SKILLS)
    skills = setup.get("skills") if isinstance(setup.get("skills"), dict) else {}
    setup_rows = []
    for setup_id, row in sorted(skills.items()):
        stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
        setup_rows.append({
            "setup_id": setup_id,
            "enabled": bool(row.get("enabled", True)),
            "trades": int(stats.get("trades", 0) or 0),
            "win_rate": stats.get("win_rate", 0),
            "expectancy": stats.get("expectancy", 0),
            "net": stats.get("net", 0),
        })
    return {
        "ts": utc_now(),
        "execution_bias": read_json(BIAS_PATH),
        "market_latest": read_json(MARKET_LATEST),
        "news_latest": read_json(NEWS_LATEST),
        "shadow_performance": read_json(SHADOW_PERFORMANCE),
        "self_improvement": read_json(SELF_IMPROVEMENT),
        "daily_exam": read_json(DAILY_EXAM),
        "cognitive_state": read_json(COGNITIVE_LATEST),
        "reasoning_trace": read_json(REASONING_TRACE),
        "setup_skills": setup_rows,
        "belief_ledger": read_json(BELIEF_LEDGER),
        "semantic_memory": read_json(SEMANTIC_MEMORY),
        "recent_trade_events": read_jsonl_tail(SCALP_LOG, max_log_lines),
    }

def provider_snapshot() -> dict:
    from tradingagents.crypto import agents
    agents._client = None
    agents._provider = None
    provider = agents._detect_provider()
    return {
        "provider": provider,
        "deep_model": agents._resolve_model(provider, "deep"),
        "quick_model": agents._resolve_model(provider, "quick"),
        "judge_model": agents._resolve_model(provider, "judge"),
        "base_url": os.environ.get("NINEROUTER_BASE_URL") or os.environ.get("OPENAI_COMPAT_BASE_URL") if provider == "9router" else None,
    }

def build_messages(context: dict) -> tuple[str, str]:
    system = (
        "You are the large-model reasoning layer for a crypto futures trading learning agent. "
        "You are read-only. Never place live orders, never recommend all-in, and never loosen risk automatically. "
        "Your job is to critique the agent, find blindspots, propose paper/shadow experiments, and produce a curriculum. "
        "Return one valid JSON object only."
    )
    user = f"""
Current trading-agent memory follows as compact JSON.

Safety contract:
- can_place_live_orders must be false.
- can_loosen_risk must be false.
- Recommendations may tighten risk, propose paper/shadow tests, or request more data.
- If edge quality is weak or sample size is low, block promotion.

Required JSON schema:
{{
  "summary": "short Vietnamese summary",
  "market_read": "short Vietnamese market/context read",
  "critical_blindspots": ["..."],
  "hypotheses": [{{"id":"...","setup_id":"...","statement":"...","test":"...","success_metric":"..."}}],
  "paper_shadow_experiments": [{{"name":"...","setup_id":"...","symbols":["..."],"rules":"...","sample_target":20}}],
  "risk_proposal": {{"mode":"tighten_only","can_place_live_orders":false,"can_loosen_risk":false,"min_signal_score":8,"blocked_sides":[],"blocked_symbols":[],"reason":"..."}},
  "curriculum": [{{"priority":1,"task":"...","acceptance_test":"..."}}],
  "confidence": 0.0
}}

Context JSON:
{compact(context)}
""".strip()
    return system, user

def call_large_model(system: str, user: str, model: str | None = None, max_tokens: int = 1600) -> str:
    from tradingagents.crypto.agents import _call_llm
    return _call_llm(system, user, model=model, max_tokens=max_tokens, temperature=0.15)

def parse_model_json(text: str) -> dict:
    from tradingagents.crypto.agents import _extract_json
    payload = _extract_json(text)
    return payload if isinstance(payload, dict) else {}

def sanitize_reasoning(payload: dict, provider: dict, raw_text: str) -> dict:
    risk = payload.get("risk_proposal") if isinstance(payload.get("risk_proposal"), dict) else {}
    violations: list[str] = []
    if risk.get("can_place_live_orders") is True or payload.get("can_place_live_orders") is True:
        violations.append("model_attempted_live_order_permission")
    if risk.get("can_loosen_risk") is True or payload.get("can_loosen_risk") is True:
        violations.append("model_attempted_risk_loosening")
    risk["mode"] = "tighten_only"
    risk["can_place_live_orders"] = False
    risk["can_loosen_risk"] = False
    payload["risk_proposal"] = risk
    payload["can_place_live_orders"] = False
    payload["can_loosen_risk"] = False
    payload["live_permission"] = False
    payload["contract"] = {"read_only": True, "paper_shadow_only": True, "can_place_live_orders": False, "can_loosen_risk": False}
    payload["provider"] = provider
    payload["raw_text_preview"] = raw_text[:1200]
    payload["safety_violations_corrected"] = violations
    return payload

def fallback_payload(error: str, provider: dict | None = None) -> dict:
    return {
        "summary": "LLM reasoning chưa chạy được, giữ pipeline deterministic và không nới risk.",
        "market_read": "Không có phản hồi model lớn trong chu kỳ này.",
        "critical_blindspots": ["llm_reasoning_unavailable"],
        "hypotheses": [],
        "paper_shadow_experiments": [],
        "risk_proposal": {"mode": "tighten_only", "can_place_live_orders": False, "can_loosen_risk": False, "reason": "LLM call failed; keep conservative mode."},
        "curriculum": [{"priority": 1, "task": "Fix 9router/model connectivity", "acceptance_test": "llm_reasoning_agent produces status=ok with provider/model metadata."}],
        "confidence": 0.0,
        "contract": {"read_only": True, "paper_shadow_only": True, "can_place_live_orders": False, "can_loosen_risk": False},
        "provider": provider or {},
        "error": error[:500],
    }

def render_report(result: dict) -> str:
    reasoning = result.get("reasoning") or {}
    provider = result.get("provider") or reasoning.get("provider") or {}
    lines = [
        "# LLM Reasoning Report",
        "",
        f"Generated: {result.get('ts')}",
        f"Status: `{result.get('status')}`",
        f"Provider: `{provider.get('provider')}` deep=`{provider.get('deep_model')}` quick=`{provider.get('quick_model')}`",
        "",
        "## Summary",
        str(reasoning.get("summary") or "none"),
        "",
        "## Market Read",
        str(reasoning.get("market_read") or "none"),
        "",
        "## Critical Blindspots",
    ]
    lines.extend(f"- {item}" for item in reasoning.get("critical_blindspots") or ["none"])
    lines.extend(["", "## Curriculum"])
    for item in reasoning.get("curriculum") or []:
        lines.append(f"- P{item.get('priority')} {item.get('task')} | test: {item.get('acceptance_test')}")
    lines.extend(["", "## Risk Proposal", "```json", json.dumps(reasoning.get("risk_proposal") or {}, ensure_ascii=True, indent=2, sort_keys=True), "```", ""])
    return "\n".join(lines)

def run_once(max_log_lines: int = 80, model: str | None = None, max_tokens: int = 1600) -> dict:
    load_dotenv()
    model_route = route_model("blindspot")
    provider = {
        "provider": model_route.get("provider_redacted"),
        "deep_model": model_route.get("deep_model") or model_route.get("model"),
        "quick_model": model_route.get("quick_model"),
        "judge_model": model_route.get("quick_model"),
        "base_url": None,
    }
    raw_context = collect_context(max_log_lines)
    egress = prepare_llm_egress(raw_context, "llm_reasoning")
    context = egress["payload"]
    ts = utc_now()
    system = ""
    user = ""
    text = ""
    if not model_route.get("allowed", True):
        blocked_reason = str(model_route.get("degraded_reason") or "route_blocked")
        payload = fallback_payload(f"model_route_blocked: {blocked_reason}", provider)
        payload["egress_proof"] = egress["proof"]
        quality = sanitize_output(payload, kind="llm_reasoning")
        usage = record_model_usage(
            "llm_reasoning",
            str(model or model_route.get("model") or provider.get("deep_model")),
            str(provider.get("provider") or model_route.get("provider_redacted")),
            prompt="",
            response=payload.get("error"),
            status="degraded",
            route_reason=str(model_route.get("route_reason")),
            fallback_reason=blocked_reason,
            quality_gate_ok=False,
        )
        status = "degraded"
        error = payload["error"]
    else:
        system, user = build_messages(context)
        budget_guard = model_budget_allowed("llm_reasoning", "blindspot", system + user, max_response_tokens=max_tokens)
        if not budget_guard.get("allowed"):
            blocked_reason = str(budget_guard.get("reason") or "token_budget_exhausted")
            model_route = {
                **model_route,
                "allowed": False,
                "degraded_reason": blocked_reason,
                "degraded_action": "fail_closed",
                "budget_guard": budget_guard,
            }
            payload = fallback_payload(f"model_budget_blocked: {blocked_reason}", provider)
            payload["egress_proof"] = egress["proof"]
            quality = sanitize_output(payload, kind="llm_reasoning")
            usage = record_model_usage(
                "llm_reasoning",
                str(model or model_route.get("model") or provider.get("deep_model")),
                str(provider.get("provider") or model_route.get("provider_redacted")),
                prompt=system + user,
                response=payload.get("error"),
                status="degraded",
                route_reason=str(model_route.get("route_reason")),
                fallback_reason=blocked_reason,
                quality_gate_ok=False,
            )
            status = "degraded"
            error = payload["error"]
        try:
            if budget_guard.get("allowed"):
                provider = provider_snapshot()
                routed_model = model or model_route.get("model") or provider.get("deep_model")
                text = call_large_model(system, user, model=routed_model, max_tokens=max_tokens)
                payload = sanitize_reasoning(parse_model_json(text), provider, text)
                payload["egress_proof"] = egress["proof"]
                quality_input = dict(payload)
                quality_input.pop("raw_text_preview", None)
                quality = sanitize_output(quality_input, kind="llm_reasoning")
                payload = quality.get("sanitized") if isinstance(quality.get("sanitized"), dict) else payload
                payload["raw_text_preview"] = text[:1200]
                status = "ok" if quality.get("ok") else "degraded"
                usage = record_model_usage(
                    "llm_reasoning",
                    str(routed_model),
                    str(provider.get("provider") or model_route.get("provider_redacted")),
                    prompt=system + user,
                    response=text,
                    status=status,
                    route_reason=str(model_route.get("route_reason")),
                    quality_gate_ok=bool(quality.get("ok")),
                )
                error = None
        except Exception as exc:
            payload = fallback_payload(f"{type(exc).__name__}: {exc}", provider)
            payload["egress_proof"] = egress["proof"]
            quality = sanitize_output(payload, kind="llm_reasoning")
            usage = record_model_usage("llm_reasoning", str(model or model_route.get("model") or provider.get("deep_model")), str(provider.get("provider") or model_route.get("provider_redacted")), prompt="", response=payload.get("error"), status="degraded", route_reason=str(model_route.get("route_reason")), fallback_reason=type(exc).__name__, quality_gate_ok=False)
            status = "degraded"
            error = payload["error"]
    risk = payload.get("risk_proposal") if isinstance(payload.get("risk_proposal"), dict) else {}
    result = {
        "ts": ts,
        "pid": os.getpid(),
        "status": status,
        "provider": provider,
        "model_route": model_route,
        "quality_gate": quality,
        "model_usage": usage,
        "egress_proof": egress["proof"],
        "reasoning": payload,
        "can_place_live_orders": bool(risk.get("can_place_live_orders")),
        "can_loosen_risk": bool(risk.get("can_loosen_risk")),
        "error": error,
    }
    try:
        prompt_trace = build_prompt_trace(
            run_id=f"llm_reasoning:{ts}",
            event_id=result.get("model_usage", {}).get("request_id"),
            source_ids=[str(result.get("egress_proof", {}).get("egress_id") or "")],
            provenance_ids=[str(result.get("egress_proof", {}).get("egress_id") or "")],
            model=str(model or model_route.get("model") or provider.get("deep_model") or ""),
            prompt_version="llm_reasoning.v1",
            prompt=system + user,
            completion=text or payload,
            model_route=model_route,
            gate_result=quality,
            outcome=status,
            egress_proof=egress["proof"],
            model_usage=usage,
            labels=["llm_reasoning", str(status)],
            evidence_refs=[str(result.get("egress_proof", {}).get("egress_id") or "")],
            payload=payload,
        )
        save_prompt_trace(prompt_trace, MEMORY_DIR / "prompt_trace_latest.json", MEMORY_DIR / "prompt_trace_history.jsonl")
        result["prompt_trace"] = prompt_trace
    except Exception as exc:
        result["status"] = "degraded"
        trace_error = f"{type(exc).__name__}: {exc}"
        result["error"] = trace_error if not result.get("error") else f"{result.get('error')} | prompt_trace: {trace_error}"
        result["prompt_trace_error"] = trace_error
    try:
        safe_append_snapshot("llm_reasoning_agent", "llm_reasoning", result, ts=ts)
        safe_append_event("llm_reasoning_agent", "llm_reasoning_update", {"status": result.get("status"), "provider": provider.get("provider"), "model": provider.get("deep_model"), "error": result.get("error")}, ts=ts)
    except Exception as exc:
        event_error = f"{type(exc).__name__}: {exc}"
        result["status"] = "degraded"
        result["error"] = event_error if not result.get("error") else f"{result.get('error')} | event_store: {event_error}"
        result["event_store_error"] = event_error
    write_json(LATEST_JSON, result)
    append_jsonl(HISTORY_JSONL, result)
    REPORT_MD.write_text(render_report(result), encoding="utf-8")
    write_heartbeat(str(result.get("status") or status), {"provider": provider.get("provider"), "model": provider.get("deep_model"), "summary": payload.get("summary"), "error": result.get("error")})
    return result

def write_heartbeat(status: str, payload: dict | None = None) -> None:
    row = {"ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json(HEARTBEAT_PATH, row)
    try:
        safe_upsert_heartbeat("llm_reasoning_agent", status, row, ts=row["ts"])
    except Exception as exc:
        event_error = f"{type(exc).__name__}: {exc}"
        row["event_store_error"] = event_error
        row["error"] = event_error if not row.get("error") else f"{row.get('error')} | heartbeat: {event_error}"
        write_json(HEARTBEAT_PATH, row)

def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None

def is_pid_running(pid: int | None, expected_script: str | None = None) -> bool:
    if not pid:
        return False
    if os.name != "nt":
        proc = Path(f"/proc/{pid}")
        if not proc.exists():
            return False
        if expected_script:
            try:
                return expected_script in (proc / "cmdline").read_text(errors="ignore")
            except Exception:
                return True
        return True
    try:
        import subprocess
        script_check = ""
        if expected_script:
            escaped = expected_script.replace("'", "''")
            script_check = f"; if ($p.CommandLine -notlike '*{escaped}*') {{ exit 2 }}"
        result = subprocess.run(["powershell", "-NoProfile", "-Command", f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction Stop; if (-not $p) {{ exit 1 }}{script_check}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return result.returncode == 0
    except Exception:
        return False

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(60.0, max(0.0, deadline - time.time())))

def status() -> int:
    pid = read_pid(PID_FILE)
    print(f"llm_reasoning_agent_pid={pid} running={is_pid_running(pid, 'llm_reasoning_agent.py')}")
    print(f"latest={LATEST_JSON}")
    print(f"report={REPORT_MD}")
    print(f"heartbeat={HEARTBEAT_PATH}")
    print(f"stop_file={STOP_FILE}")
    return 0

def run_loop(args: argparse.Namespace) -> int:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(PID_FILE)
    if not args.once and existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid, "llm_reasoning_agent.py"):
        print(f"llm reasoning agent already running pid={existing_pid}", flush=True)
        return 0
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        result = run_once(args.max_log_lines, args.model, args.max_tokens)
        provider = result.get("provider") or {}
        print(f"llm_reasoning status={result.get('status')} provider={provider.get('provider')} model={provider.get('deep_model')}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_minutes * 60)
    return 0

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run large-model reasoning loop for the trading agent")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=60.0)
    parser.add_argument("--max-log-lines", type=int, default=80)
    parser.add_argument("--max-tokens", type=int, default=1600)
    parser.add_argument("--model")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be positive")
    if args.max_log_lines < 10:
        parser.error("--max-log-lines must be >= 10")
    return args

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        return status()
    return run_loop(args)

if __name__ == "__main__":
    raise SystemExit(main())
