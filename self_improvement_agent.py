"""Daily self-improvement loop for the trading agent.

This agent does not trade. It audits whether yesterday's learning loop actually
made the system smarter: more evidence, fewer repeated mistakes, clearer setup
boundaries, and better data quality. Output is a curriculum/proposal that other
agents or a human can inspect before changing risk.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from belief_ledger import compact_ledger, load_ledger
from event_store import safe_append_event, safe_append_snapshot, safe_upsert_heartbeat
from market_learner import safe_float, valid_paper_close
from setup_skill_library import load_library, skill_summary

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"

BIAS_PATH = MEMORY_DIR / "execution_bias.json"
MARKET_MODEL_PATH = MEMORY_DIR / "market_model.json"
COGNITIVE_LATEST = MEMORY_DIR / "cognitive_state_latest.json"
REFLECTION_PROFILE = MEMORY_DIR / "profile.json"
SHADOW_PERFORMANCE = MEMORY_DIR / "shadow_performance_latest.json"
NEWS_LATEST = MEMORY_DIR / "news_latest.json"
SCALP_LOG = STATE_DIR / "scalp_autotrader.jsonl"

LATEST_JSON = MEMORY_DIR / "self_improvement_latest.json"
HISTORY_JSONL = MEMORY_DIR / "self_improvement_history.jsonl"
REPORT_MD = MEMORY_DIR / "self_improvement_latest.md"
HEARTBEAT_PATH = STATE_DIR / "self_improvement_agent_heartbeat.json"
PID_FILE = STATE_DIR / "self_improvement_agent.pid"
STOP_FILE = STATE_DIR / "STOP_SELF_IMPROVEMENT_AGENT"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def age_hours(value: object) -> float | None:
    parsed = parse_ts(value)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 3600)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


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


def read_jsonl_tail(path: Path, max_lines: int = 1000) -> list[dict]:
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


def summarize_paper(rows: list[dict]) -> dict:
    closes = [row for row in rows if valid_paper_close(row)]
    wins = sum(1 for row in closes if safe_float(row.get("net")) > 0)
    losses = sum(1 for row in closes if safe_float(row.get("net")) < 0)
    net = sum(safe_float(row.get("net")) for row in closes)
    events = Counter(str(row.get("event") or "unknown") for row in rows)
    block_reasons = Counter(str(row.get("reason") or row.get("block_reason") or "unknown") for row in rows if row.get("event") in {"risk_block", "memory_bias_filter"})
    return {
        "closes": len(closes),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(closes), 4) if closes else 0.0,
        "net": round(net, 8),
        "events": dict(events),
        "top_block_reasons": block_reasons.most_common(6),
    }


def setup_rows(library: dict) -> list[dict]:
    return list((skill_summary(library).get("skills") or []))


def score_data_quality(shadow: dict, news: dict, cognitive: dict) -> dict:
    dq = shadow.get("data_quality") or {}
    selected_rows = safe_float(dq.get("selected_rows"))
    api_errors = safe_float(dq.get("api_error_count"))
    unresolved = safe_float(dq.get("unresolved_count"))
    confidence = str(dq.get("confidence") or "low")
    conf_score = {"high": 1.0, "medium": 0.65, "low": 0.3}.get(confidence, 0.3)
    error_penalty = min(0.35, api_errors / max(1.0, selected_rows))
    unresolved_penalty = min(0.25, unresolved / max(1.0, selected_rows))
    news_age = age_hours(news.get("ts"))
    news_score = 1.0 if news_age is not None and news_age <= 2 else 0.6 if news_age is not None and news_age <= 8 else 0.25
    cognitive_age = age_hours(cognitive.get("ts"))
    cognitive_score = 1.0 if cognitive_age is not None and cognitive_age <= 1 else 0.65 if cognitive_age is not None and cognitive_age <= 6 else 0.25
    score = clamp((conf_score * 0.55) + (news_score * 0.2) + (cognitive_score * 0.25) - error_penalty - unresolved_penalty)
    return {
        "score": round(score, 4),
        "shadow_confidence": confidence,
        "api_error_count": int(api_errors),
        "unresolved_count": int(unresolved),
        "news_age_hours": round(news_age, 2) if news_age is not None else None,
        "cognitive_age_hours": round(cognitive_age, 2) if cognitive_age is not None else None,
    }


def score_evidence_coverage(paper: dict, shadow: dict, setups: list[dict], beliefs: dict) -> dict:
    shadow_closed = safe_float((shadow.get("overall") or {}).get("closed"))
    paper_closed = safe_float(paper.get("closes"))
    setup_samples = sum(min(25, int(row.get("trades", 0) or 0)) for row in setups)
    belief_count = safe_float(beliefs.get("belief_count"))
    score = clamp((shadow_closed / 500) * 0.45 + (paper_closed / 100) * 0.2 + (setup_samples / 175) * 0.25 + (belief_count / 40) * 0.1)
    return {
        "score": round(score, 4),
        "shadow_closed": int(shadow_closed),
        "paper_closed": int(paper_closed),
        "setup_sample_units": int(setup_samples),
        "belief_count": int(belief_count),
    }


def score_edge_quality(paper: dict, shadow: dict, setups: list[dict]) -> dict:
    overall = shadow.get("overall") or {}
    shadow_wr = safe_float(overall.get("win_rate"))
    shadow_exp = safe_float(overall.get("expectancy"))
    shadow_pf = safe_float(overall.get("profit_factor"))
    paper_wr = safe_float(paper.get("win_rate"))
    positive_setups = sum(1 for row in setups if int(row.get("trades", 0) or 0) >= 5 and safe_float(row.get("expectancy")) > 0)
    sampled_setups = sum(1 for row in setups if int(row.get("trades", 0) or 0) >= 5)
    setup_score = positive_setups / sampled_setups if sampled_setups else 0.0
    wr_score = clamp(shadow_wr / 0.55)
    exp_score = 0.8 if shadow_exp > 0 else 0.35 if shadow_exp == 0 else 0.1
    pf_score = clamp(shadow_pf / 1.5) if shadow_pf < 999 else 1.0
    paper_score = clamp(paper_wr / 0.55) if paper.get("closes") else 0.0
    score = clamp(wr_score * 0.25 + exp_score * 0.25 + pf_score * 0.25 + setup_score * 0.15 + paper_score * 0.1)
    return {
        "score": round(score, 4),
        "shadow_win_rate": shadow_wr,
        "shadow_expectancy": shadow_exp,
        "shadow_profit_factor": shadow_pf,
        "paper_win_rate": paper_wr,
        "positive_sampled_setups": positive_setups,
        "sampled_setups": sampled_setups,
    }


def score_reasoning_quality(cognitive: dict, ledger_summary: dict) -> dict:
    trace = cognitive.get("reasoning_trace") or {}
    thought_quality = clamp(safe_float(trace.get("thought_quality_score")))
    missing = len(trace.get("missing_evidence") or [])
    contradictions = len(trace.get("contradictions") or [])
    active_beliefs = int((ledger_summary.get("by_status") or {}).get("active", 0) or 0)
    belief_score = clamp(active_beliefs / 12)
    score = clamp(thought_quality * 0.55 + belief_score * 0.25 - min(0.25, missing * 0.04) - min(0.25, contradictions * 0.08) + 0.2)
    return {
        "score": round(score, 4),
        "thought_quality_score": thought_quality,
        "missing_evidence_count": missing,
        "contradiction_count": contradictions,
        "active_beliefs": active_beliefs,
    }


def detect_blindspots(inputs: dict, scores: dict) -> list[dict]:
    blindspots: list[dict] = []
    shadow = inputs["shadow"]
    paper = inputs["paper"]
    market_model = inputs["market_model"]
    setups = inputs["setups"]
    beliefs = inputs["beliefs"]
    dq = scores["data_quality"]
    coverage = scores["evidence_coverage"]
    edge = scores["edge_quality"]
    reasoning = scores["reasoning_quality"]

    if coverage["shadow_closed"] < 500:
        blindspots.append({"type": "insufficient_shadow_sample", "severity": "high", "detail": f"Only {coverage['shadow_closed']} closed shadow trades; target >= 500 before trusting promotion."})
    if edge["shadow_expectancy"] <= 0 or edge["shadow_profit_factor"] < 1:
        blindspots.append({"type": "negative_shadow_edge", "severity": "critical", "detail": "Shadow expectancy/PF is not positive; no live promotion should happen."})
    if dq["api_error_count"] > 0:
        blindspots.append({"type": "market_data_gap", "severity": "high", "detail": f"Shadow evaluator has {dq['api_error_count']} API/data errors; backfill/caching needed."})
    if paper.get("closes", 0) < 20:
        blindspots.append({"type": "paper_sample_gap", "severity": "medium", "detail": "Paper closes are too low for intraday confidence scoring."})
    weak_setups = [row for row in setups if int(row.get("trades", 0) or 0) >= 5 and safe_float(row.get("expectancy")) <= 0]
    if weak_setups:
        blindspots.append({"type": "weak_setup_skills", "severity": "high", "detail": ", ".join(row["setup_id"] for row in weak_setups[:6])})
    low_sample_setups = [row for row in setups if int(row.get("trades", 0) or 0) < 20]
    if low_sample_setups:
        blindspots.append({"type": "setup_sample_gap", "severity": "medium", "detail": f"{len(low_sample_setups)} setup skills have <20 samples."})
    if reasoning["missing_evidence_count"] or reasoning["contradiction_count"]:
        blindspots.append({"type": "reasoning_gaps", "severity": "medium", "detail": f"missing={reasoning['missing_evidence_count']} contradictions={reasoning['contradiction_count']}"})
    if safe_float((market_model.get("last_rules") or {}).get("min_signal_score")) >= 8 and edge["score"] < 0.4:
        blindspots.append({"type": "strict_but_not_learning", "severity": "medium", "detail": "Risk is strict but measured edge is still weak; prioritize diagnostics over more filters."})
    if beliefs.get("belief_count", 0) < 10:
        blindspots.append({"type": "belief_memory_gap", "severity": "medium", "detail": "Belief ledger is too sparse; convert repeated lessons into testable beliefs."})
    return blindspots


def curriculum_from_blindspots(inputs: dict, blindspots: list[dict]) -> list[dict]:
    tasks: list[dict] = []
    shadow = inputs["shadow"]
    setups = inputs["setups"]
    paper = inputs["paper"]
    for blindspot in blindspots:
        btype = blindspot["type"]
        if btype == "negative_shadow_edge":
            tasks.append({"priority": 1, "task": "Freeze promotion", "action": "Keep Live disabled; only Paper/Shadow until expectancy > 0 and PF >= 1.2 for 500+ closed shadow trades.", "evidence": blindspot["detail"]})
        elif btype == "market_data_gap":
            tasks.append({"priority": 2, "task": "Backfill shadow data", "action": "Run shadow evaluator with cache/backoff and record unresolved/api_error ratio before next promotion review.", "evidence": blindspot["detail"]})
        elif btype == "weak_setup_skills":
            tasks.append({"priority": 2, "task": "Review weak setups", "action": f"Audit setup outcomes: {blindspot['detail']}; add invalidation rules instead of increasing leverage.", "evidence": blindspot["detail"]})
        elif btype == "setup_sample_gap":
            low = [row["setup_id"] for row in setups if int(row.get("trades", 0) or 0) < 20][:5]
            tasks.append({"priority": 3, "task": "Collect setup samples", "action": f"Force Shadow tagging for low-sample setups: {', '.join(low)}.", "evidence": blindspot["detail"]})
        elif btype == "reasoning_gaps":
            tasks.append({"priority": 3, "task": "Resolve reasoning gaps", "action": "Use cognitive_supervisor missing_evidence/contradictions as the next curiosity focus.", "evidence": blindspot["detail"]})
        elif btype == "belief_memory_gap":
            tasks.append({"priority": 4, "task": "Promote lessons to beliefs", "action": "Convert repeated reflection lessons into belief_ledger entries with for/against evidence.", "evidence": blindspot["detail"]})
    kill_candidates = shadow.get("kill_candidates") or []
    for row in kill_candidates[:5]:
        tasks.append({"priority": 2, "task": "Kill-candidate audit", "action": f"Investigate {row.get('group')}:{row.get('key')} closed={row.get('closed')} WR={row.get('win_rate')} exp={row.get('expectancy')}; block until improved.", "evidence": "shadow kill candidate"})
    for reason, count in (paper.get("top_block_reasons") or [])[:3]:
        tasks.append({"priority": 4, "task": "Risk-block clustering", "action": f"Study repeated block reason `{reason}` count={count}; decide whether it is protecting capital or hiding stale sleep state.", "evidence": "paper/risk logs"})
    tasks.sort(key=lambda item: item["priority"])
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for task in tasks:
        key = (task["task"], task["action"])
        if key not in seen:
            seen.add(key)
            deduped.append(task)
    return deduped[:12]


def build_guardrail_proposal(inputs: dict, scores: dict, blindspots: list[dict]) -> dict:
    bias = inputs["bias"]
    min_score = int(bias.get("min_signal_score") or 6)
    critical = any(item.get("severity") == "critical" for item in blindspots)
    high = sum(1 for item in blindspots if item.get("severity") == "high")
    if critical:
        min_score = max(min_score, 8)
    elif high >= 2:
        min_score = max(min_score, 7)
    return {
        "mode": "proposal_only",
        "can_loosen": False,
        "can_trade_live": False,
        "recommended_min_signal_score": min_score,
        "requires_human_review": True,
        "promotion_requirements": [
            "shadow_closed >= 500",
            "shadow_expectancy > 0",
            "shadow_profit_factor >= 1.2",
            "data_quality.score >= 0.75",
            "no critical blindspots",
        ],
        "reason": "Self-improvement agent is allowed to propose research/tightening only; it never loosens risk automatically.",
        "score_snapshot": {key: value.get("score") for key, value in scores.items()},
    }


def render_report(result: dict) -> str:
    lines = [
        "# Self Improvement Report",
        "",
        f"Generated: {result.get('ts')}",
        f"Overall learning score: `{result.get('overall_learning_score')}`",
        f"Readiness: `{result.get('readiness')}`",
        "",
        "## Scores",
    ]
    for name, payload in (result.get("scores") or {}).items():
        lines.append(f"- {name}: {payload.get('score')} `{payload}`")
    lines.extend(["", "## Blindspots"])
    for item in result.get("blindspots") or []:
        lines.append(f"- {item.get('severity')} `{item.get('type')}`: {item.get('detail')}")
    if not result.get("blindspots"):
        lines.append("- none")
    lines.extend(["", "## Learning Curriculum"])
    for task in result.get("learning_curriculum") or []:
        lines.append(f"- P{task.get('priority')} {task.get('task')}: {task.get('action')}")
    if not result.get("learning_curriculum"):
        lines.append("- Continue collecting Paper/Shadow evidence.")
    lines.extend(["", "## Guardrail Proposal", "```json", json.dumps(result.get("guardrail_proposal") or {}, ensure_ascii=True, indent=2, sort_keys=True), "```", ""])
    return "\n".join(lines)


def load_inputs(max_log_lines: int = 1000) -> dict:
    ledger_summary = compact_ledger(load_ledger())
    library = load_library()
    paper = summarize_paper(read_jsonl_tail(SCALP_LOG, max_log_lines))
    return {
        "bias": read_json(BIAS_PATH),
        "market_model": read_json(MARKET_MODEL_PATH),
        "cognitive": read_json(COGNITIVE_LATEST),
        "profile": read_json(REFLECTION_PROFILE),
        "shadow": read_json(SHADOW_PERFORMANCE),
        "news": read_json(NEWS_LATEST),
        "beliefs": ledger_summary,
        "setups": setup_rows(library),
        "paper": paper,
    }


def run_once(max_log_lines: int = 1000) -> dict:
    ts = utc_now()
    inputs = load_inputs(max_log_lines)
    scores = {
        "data_quality": score_data_quality(inputs["shadow"], inputs["news"], inputs["cognitive"]),
        "evidence_coverage": score_evidence_coverage(inputs["paper"], inputs["shadow"], inputs["setups"], inputs["beliefs"]),
        "edge_quality": score_edge_quality(inputs["paper"], inputs["shadow"], inputs["setups"]),
        "reasoning_quality": score_reasoning_quality(inputs["cognitive"], inputs["beliefs"]),
    }
    overall = round(sum(item["score"] for item in scores.values()) / max(1, len(scores)), 4)
    blindspots = detect_blindspots(inputs, scores)
    curriculum = curriculum_from_blindspots(inputs, blindspots)
    proposal = build_guardrail_proposal(inputs, scores, blindspots)
    readiness = "not_ready" if blindspots else "observe_ready"
    if overall >= 0.75 and not any(item.get("severity") in {"critical", "high"} for item in blindspots):
        readiness = "paper_candidate_ready"
    result = {
        "ts": ts,
        "pid": os.getpid(),
        "overall_learning_score": overall,
        "readiness": readiness,
        "scores": scores,
        "blindspots": blindspots,
        "learning_curriculum": curriculum,
        "guardrail_proposal": proposal,
        "snapshot": {
            "paper": inputs["paper"],
            "shadow_overall": inputs["shadow"].get("overall") or {},
            "shadow_data_quality": inputs["shadow"].get("data_quality") or {},
            "beliefs": inputs["beliefs"],
            "setup_count": len(inputs["setups"]),
            "market_regime": (inputs["market_model"].get("last_market_state") or {}).get("primary_regime"),
        },
    }
    write_json(LATEST_JSON, result)
    REPORT_MD.write_text(render_report(result), encoding="utf-8")
    append_jsonl(HISTORY_JSONL, result)
    safe_append_snapshot("self_improvement_agent", "self_improvement", result, ts=ts)
    safe_append_event("self_improvement_agent", "self_improvement_update", {"overall_learning_score": overall, "readiness": readiness, "blindspots": len(blindspots)}, ts=ts)
    write_heartbeat("ok", {"overall_learning_score": overall, "readiness": readiness, "blindspots": len(blindspots)})
    return result


def write_heartbeat(status: str, payload: dict | None = None) -> None:
    row = {"ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json(HEARTBEAT_PATH, row)
    safe_upsert_heartbeat("self_improvement_agent", status, row, ts=row["ts"])


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
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction Stop; if (-not $p) {{ exit 1 }}{script_check}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def status() -> int:
    pid = read_pid(PID_FILE)
    print(f"self_improvement_agent_pid={pid} running={is_pid_running(pid, 'self_improvement_agent.py')}")
    print(f"latest={LATEST_JSON}")
    print(f"report={REPORT_MD}")
    print(f"heartbeat={HEARTBEAT_PATH}")
    print(f"stop_file={STOP_FILE}")
    return 0


def run_loop(args: argparse.Namespace) -> int:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(PID_FILE)
    if not args.once and existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid, "self_improvement_agent.py"):
        print(f"self improvement agent already running pid={existing_pid}", flush=True)
        return 0
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        try:
            result = run_once(args.max_log_lines)
            print(f"self_improvement ts={result['ts']} score={result['overall_learning_score']} readiness={result['readiness']} blindspots={len(result['blindspots'])}", flush=True)
        except Exception as exc:
            write_heartbeat("error", {"error": str(exc)[:300]})
            print(f"self_improvement_error {str(exc)[:160]}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_hours * 3600)
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily self-improvement audit for the trading agent")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-hours", type=float, default=24.0)
    parser.add_argument("--max-log-lines", type=int, default=1000)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_hours <= 0:
        parser.error("--interval-hours must be positive")
    if args.max_log_lines < 50:
        parser.error("--max-log-lines must be >= 50")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        return status()
    return run_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
