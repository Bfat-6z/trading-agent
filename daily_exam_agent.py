"""Daily paper-only exam for the trading agent.

The goal is to grade learning quality once per local day. This agent never
places live orders and never touches exchange API keys. If the selected exam is
trade-like, it records a paper/shadow candidate for later evaluation.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from event_store import safe_append_event, safe_append_snapshot, safe_upsert_heartbeat
from market_learner import safe_float, valid_paper_close
from setup_skill_library import load_library, skill_summary

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"

MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
SCALP_LOG = STATE_DIR / "scalp_autotrader.jsonl"
BIAS_PATH = MEMORY_DIR / "execution_bias.json"
NEWS_LATEST = MEMORY_DIR / "news_latest.json"
SHADOW_PERFORMANCE = MEMORY_DIR / "shadow_performance_latest.json"
SELF_IMPROVEMENT = MEMORY_DIR / "self_improvement_latest.json"
COGNITIVE_LATEST = MEMORY_DIR / "cognitive_state_latest.json"
LIVE_READINESS = MEMORY_DIR / "live_readiness_latest.json"
SELF_MODEL = MEMORY_DIR / "self_model_latest.json"

LATEST_JSON = MEMORY_DIR / "daily_exam_latest.json"
HISTORY_JSONL = MEMORY_DIR / "daily_exam_history.jsonl"
REPORT_MD = MEMORY_DIR / "daily_exam_latest.md"
HEARTBEAT_PATH = STATE_DIR / "daily_exam_agent_heartbeat.json"
PID_FILE = STATE_DIR / "daily_exam_agent.pid"
STOP_FILE = STATE_DIR / "STOP_DAILY_EXAM_AGENT"

EXAM_TYPES = [
    "paper_trade_candidate",
    "risk_gate_review",
    "setup_defense",
    "news_market_context",
    "shadow_edge_review",
]

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def local_now() -> datetime:
    return datetime.now().astimezone()

def local_date_key(value: datetime | None = None) -> str:
    return (value or local_now()).date().isoformat()

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
    blocks = Counter(str(row.get("reason") or row.get("block_reason") or "unknown") for row in rows if row.get("event") in {"risk_block", "memory_bias_filter"})
    return {
        "closes": len(closes),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(closes), 4) if closes else 0.0,
        "net": round(net, 8),
        "risk_blocks": sum(blocks.values()),
        "top_block_reasons": blocks.most_common(5),
    }

def load_inputs(max_log_lines: int = 1500) -> dict:
    library = load_library()
    return {
        "bias": read_json(BIAS_PATH),
        "market": read_json(MARKET_LATEST),
        "news": read_json(NEWS_LATEST),
        "shadow": read_json(SHADOW_PERFORMANCE),
        "self_improvement": read_json(SELF_IMPROVEMENT),
        "cognitive": read_json(COGNITIVE_LATEST),
        "live_readiness": read_json(LIVE_READINESS),
        "self_model": read_json(SELF_MODEL),
        "setups": skill_summary(library).get("skills") or [],
        "paper": summarize_paper(read_jsonl_tail(SCALP_LOG, max_log_lines)),
        "previous_exam": read_json(LATEST_JSON),
    }

def score_data_freshness(inputs: dict) -> dict:
    market_age = age_hours(inputs["market"].get("ts"))
    news_age = age_hours(inputs["news"].get("ts"))
    cognitive_age = age_hours(inputs["cognitive"].get("ts"))
    improvement_age = age_hours(inputs["self_improvement"].get("ts"))
    parts = [
        1.0 if market_age is not None and market_age <= 0.25 else 0.6 if market_age is not None and market_age <= 1 else 0.2,
        1.0 if news_age is not None and news_age <= 1 else 0.7 if news_age is not None and news_age <= 6 else 0.25,
        1.0 if cognitive_age is not None and cognitive_age <= 1 else 0.65 if cognitive_age is not None and cognitive_age <= 6 else 0.25,
        1.0 if improvement_age is not None and improvement_age <= 8 else 0.55 if improvement_age is not None and improvement_age <= 24 else 0.2,
    ]
    return {
        "score": round(sum(parts) / len(parts), 4),
        "market_age_hours": round(market_age, 3) if market_age is not None else None,
        "news_age_hours": round(news_age, 3) if news_age is not None else None,
        "cognitive_age_hours": round(cognitive_age, 3) if cognitive_age is not None else None,
        "self_improvement_age_hours": round(improvement_age, 3) if improvement_age is not None else None,
    }

def score_risk_discipline(inputs: dict) -> dict:
    bias = inputs["bias"]
    live = inputs["live_readiness"]
    self_improvement = inputs["self_improvement"]
    guard = self_improvement.get("guardrail_proposal") if isinstance(self_improvement.get("guardrail_proposal"), dict) else {}
    min_score = safe_float(bias.get("min_signal_score"), 0)
    mode = str(live.get("mode") or live.get("status") or "paper").lower()
    paper_only_score = 1.0 if mode != "live" and not guard.get("can_trade_live") else 0.0
    min_score_score = clamp(min_score / 8.0)
    tighten_score = 1.0 if guard.get("can_loosen") is False or not guard else 0.7
    sleep_score = 1.0 if bias.get("risk_posture") in {"defensive", "normal", "unknown", None} else 0.6
    score = paper_only_score * 0.45 + min_score_score * 0.25 + tighten_score * 0.2 + sleep_score * 0.1
    return {
        "score": round(clamp(score), 4),
        "mode": mode,
        "min_signal_score": min_score,
        "can_trade_live": bool(guard.get("can_trade_live")),
        "can_loosen": bool(guard.get("can_loosen")),
        "risk_posture": bias.get("risk_posture") or "unknown",
    }

def score_evidence_coverage(inputs: dict) -> dict:
    shadow_closed = safe_float((inputs["shadow"].get("overall") or {}).get("closed"))
    paper_closed = safe_float(inputs["paper"].get("closes"))
    setup_samples = sum(min(25, int(row.get("trades", 0) or 0)) for row in inputs["setups"])
    score = clamp((shadow_closed / 500) * 0.55 + (paper_closed / 100) * 0.25 + (setup_samples / 175) * 0.2)
    return {
        "score": round(score, 4),
        "shadow_closed": int(shadow_closed),
        "paper_closed": int(paper_closed),
        "setup_sample_units": int(setup_samples),
    }

def score_edge_quality(inputs: dict) -> dict:
    shadow = inputs["shadow"].get("overall") or {}
    paper = inputs["paper"]
    shadow_wr = safe_float(shadow.get("win_rate"))
    shadow_exp = safe_float(shadow.get("expectancy"))
    shadow_pf = safe_float(shadow.get("profit_factor"))
    paper_wr = safe_float(paper.get("win_rate"))
    exp_score = 1.0 if shadow_exp > 0 else 0.35 if shadow_exp == 0 else 0.05
    pf_score = clamp(shadow_pf / 1.5) if shadow_pf < 999 else 1.0
    wr_score = clamp(shadow_wr / 0.55)
    paper_score = clamp(paper_wr / 0.55) if paper.get("closes") else 0.0
    score = wr_score * 0.3 + exp_score * 0.3 + pf_score * 0.25 + paper_score * 0.15
    return {
        "score": round(clamp(score), 4),
        "shadow_win_rate": shadow_wr,
        "shadow_expectancy": shadow_exp,
        "shadow_profit_factor": shadow_pf,
        "paper_win_rate": paper_wr,
    }

def score_learning_progress(inputs: dict) -> dict:
    self_improvement = inputs["self_improvement"]
    cognitive = inputs["cognitive"]
    previous = inputs.get("previous_exam") or {}
    current_learning = safe_float(self_improvement.get("overall_learning_score"))
    reasoning = cognitive.get("reasoning_trace") if isinstance(cognitive.get("reasoning_trace"), dict) else {}
    thought_quality = clamp(safe_float(reasoning.get("thought_quality_score"), 0.4))
    previous_score = safe_float(previous.get("quality_score"))
    trend_bonus = 0.1 if previous_score and current_learning >= previous_score else 0.0
    score = clamp(current_learning * 0.55 + thought_quality * 0.35 + trend_bonus)
    return {
        "score": round(score, 4),
        "self_improvement_score": current_learning,
        "thought_quality_score": thought_quality,
        "previous_quality_score": previous_score,
    }

def quality_rubric(inputs: dict) -> dict:
    scores = {
        "data_freshness": score_data_freshness(inputs),
        "risk_discipline": score_risk_discipline(inputs),
        "evidence_coverage": score_evidence_coverage(inputs),
        "edge_quality": score_edge_quality(inputs),
        "learning_progress": score_learning_progress(inputs),
    }
    weights = {
        "data_freshness": 0.2,
        "risk_discipline": 0.25,
        "evidence_coverage": 0.2,
        "edge_quality": 0.2,
        "learning_progress": 0.15,
    }
    quality = sum(scores[key]["score"] * weight for key, weight in weights.items())
    return {"quality_score": round(quality * 100, 2), "scores": scores, "weights": weights}

def grade_letter(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 75:
        return "B"
    if score >= 65:
        return "C"
    if score >= 50:
        return "D"
    return "F"

def deterministic_rng(local_date: str) -> random.Random:
    return random.Random(f"daily_exam:{local_date}")

def choose_exam_type(inputs: dict, local_date: str) -> str:
    rng = deterministic_rng(local_date)
    gaps = set(inputs.get("self_model", {}).get("known_gaps") or [])
    if "trade_lifecycle_not_clean" in gaps:
        return "risk_gate_review"
    if "no_post_trade_reviews_yet" in gaps or "no_counterfactual_replays_yet" in gaps:
        return "setup_defense"
    weak = sorted(((name, row.get("score", 0.0)) for name, row in quality_rubric(inputs)["scores"].items()), key=lambda item: item[1])
    if weak and weak[0][1] < 0.45:
        mapping = {
            "data_freshness": "news_market_context",
            "risk_discipline": "risk_gate_review",
            "evidence_coverage": "setup_defense",
            "edge_quality": "shadow_edge_review",
            "learning_progress": "setup_defense",
        }
        if rng.random() < 0.65:
            return mapping.get(weak[0][0], rng.choice(EXAM_TYPES))
    return rng.choice(EXAM_TYPES)

def top_market_symbol(market: dict) -> dict:
    rows = market.get("hot") if isinstance(market.get("hot"), list) else []
    if rows:
        return rows[0] if isinstance(rows[0], dict) else {"symbol": rows[0]}
    majors = market.get("majors") if isinstance(market.get("majors"), list) else []
    if majors:
        return majors[0] if isinstance(majors[0], dict) else {"symbol": majors[0]}
    return {"symbol": "BTCUSDT"}

def weakest_setup(setups: list[dict]) -> dict:
    if not setups:
        return {"setup_id": "unknown", "trades": 0, "expectancy": 0.0, "win_rate": 0.0}
    return sorted(setups, key=lambda row: (int(row.get("trades", 0) or 0) >= 20, safe_float(row.get("expectancy")), safe_float(row.get("win_rate"))))[0]

def build_exam_task(exam_type: str, inputs: dict, local_date: str) -> dict:
    symbol_row = top_market_symbol(inputs["market"])
    symbol = str(symbol_row.get("symbol") or "BTCUSDT").upper()
    change = safe_float(symbol_row.get("change_pct") or symbol_row.get("change_24h_pct"))
    side = "SHORT" if change >= 15 else "LONG" if change <= -15 else "OBSERVE"
    setup = weakest_setup(inputs["setups"])
    if exam_type == "paper_trade_candidate":
        return {
            "prompt": "Choose whether to record one paper/shadow trade candidate from the hottest market symbol.",
            "symbol": symbol,
            "side": side,
            "setup_id": "exhaustion_fade" if abs(change) >= 15 else "observe_only",
            "change_pct": round(change, 4),
            "constraints": ["paper_or_shadow_only", "no_live_order", "must_name_invalidation", "must_respect_min_signal_score"],
        }
    if exam_type == "risk_gate_review":
        return {
            "prompt": "Decide whether the agent may loosen risk or move toward live trading after today's evidence.",
            "min_signal_score": inputs["bias"].get("min_signal_score"),
            "risk_posture": inputs["bias"].get("risk_posture"),
            "self_model_gaps": inputs.get("self_model", {}).get("known_gaps") or [],
            "constraints": ["tighten_only_without_human_review", "no_live_promotion"],
        }
    if exam_type == "setup_defense":
        return {
            "prompt": "Defend or reject the weakest setup skill based on current evidence.",
            "setup_id": setup.get("setup_id"),
            "trades": setup.get("trades", 0),
            "win_rate": setup.get("win_rate", 0.0),
            "expectancy": setup.get("expectancy", 0.0),
            "self_model_curriculum": inputs.get("self_model", {}).get("curriculum") or [],
            "constraints": ["do_not_promote_under_sampled_setup", "produce_next_sample_target"],
        }
    if exam_type == "news_market_context":
        return {
            "prompt": "Explain how current news and market freshness should affect today's paper trading posture.",
            "macro_risk_score": inputs["news"].get("macro_risk_score"),
            "headline_chaos": inputs["news"].get("headline_chaos"),
            "market_ts": inputs["market"].get("ts"),
            "news_ts": inputs["news"].get("ts"),
            "constraints": ["stale_data_blocks_confidence", "headline_chaos_tightens_risk"],
        }
    return {
        "prompt": "Review shadow edge and decide whether the agent deserves any promotion.",
        "shadow_overall": inputs["shadow"].get("overall") or {},
        "data_quality": inputs["shadow"].get("data_quality") or {},
        "constraints": ["positive_expectancy_required", "sample_size_required", "data_quality_required"],
    }

def answer_exam(exam_type: str, task: dict, inputs: dict, rubric: dict) -> dict:
    risk = rubric["scores"]["risk_discipline"]
    evidence = rubric["scores"]["evidence_coverage"]
    edge = rubric["scores"]["edge_quality"]
    if exam_type == "paper_trade_candidate":
        allow_candidate = task.get("side") in {"LONG", "SHORT"} and risk["score"] >= 0.7 and evidence["score"] >= 0.15
        action = "record_shadow_candidate" if allow_candidate else "observe_only"
        return {
            "action": action,
            "can_place_live_order": False,
            "symbol": task.get("symbol"),
            "side": task.get("side") if allow_candidate else None,
            "setup_id": task.get("setup_id"),
            "reason": "Paper/shadow candidate only; live trading remains disabled during data collection.",
            "invalidation": "Reject if spread/data freshness/risk gate deteriorates before simulated entry.",
        }
    if exam_type == "risk_gate_review":
        return {
            "action": "keep_tight_or_tighten",
            "can_loosen": False,
            "can_trade_live": False,
            "recommended_min_signal_score": max(8 if edge["score"] < 0.55 else 7, int(safe_float(inputs["bias"].get("min_signal_score"), 7))),
            "reason": "No automatic risk loosening; promotion requires sustained shadow edge and human review.",
        }
    if exam_type == "setup_defense":
        reject = safe_float(task.get("expectancy")) <= 0 or int(task.get("trades", 0) or 0) < 20
        return {
            "action": "collect_more_samples" if reject else "keep_enabled_paper_only",
            "setup_id": task.get("setup_id"),
            "can_promote": False,
            "next_sample_target": max(20, int(task.get("trades", 0) or 0) + 10),
            "reason": "Under-sampled or weak setup must earn more paper/shadow evidence before promotion." if reject else "Setup can stay enabled for paper/shadow, not live.",
        }
    if exam_type == "news_market_context":
        stale = rubric["scores"]["data_freshness"]["score"] < 0.6
        return {
            "action": "tighten_risk" if stale else "paper_only_observe",
            "can_place_live_order": False,
            "freshness_ok": not stale,
            "reason": "Stale or chaotic context blocks confidence; fresh context still only permits paper/shadow collection.",
        }
    promote = edge["shadow_expectancy"] > 0 and edge["shadow_profit_factor"] >= 1.2 and evidence["shadow_closed"] >= 500
    return {
        "action": "promotion_review_candidate" if promote else "no_promotion",
        "can_trade_live": False,
        "shadow_expectancy": edge["shadow_expectancy"],
        "shadow_profit_factor": edge["shadow_profit_factor"],
        "shadow_closed": evidence["shadow_closed"],
        "reason": "Shadow edge is not sufficient for live promotion." if not promote else "Metrics qualify for human review only, not automatic live trading.",
    }

def grade_exam(exam_type: str, answer: dict, inputs: dict, rubric: dict) -> dict:
    points = 0
    checks: list[dict] = []
    def add(name: str, ok: bool, weight: int, detail: str) -> None:
        nonlocal points
        if ok:
            points += weight
        checks.append({"check": name, "ok": bool(ok), "weight": weight, "detail": detail})

    add("no_live_execution", answer.get("can_place_live_order") is not True and answer.get("can_trade_live") is not True, 30, "Exam must stay paper/shadow only.")
    add("risk_contract", answer.get("can_loosen") is not True and answer.get("can_promote") is not True, 20, "No automatic loosening/promotion.")
    add("uses_current_evidence", rubric["scores"]["data_freshness"]["score"] >= 0.35, 15, "Market/news/cognitive data must be usable.")
    add("sample_awareness", rubric["scores"]["evidence_coverage"].get("shadow_closed", 0) >= 50 or answer.get("action") in {"collect_more_samples", "observe_only", "no_promotion", "tighten_risk"}, 15, "Agent must recognize sample gaps.")
    add("edge_awareness", rubric["scores"]["edge_quality"]["score"] >= 0.55 or answer.get("action") in {"no_promotion", "keep_tight_or_tighten", "collect_more_samples", "observe_only"}, 20, "Weak edge should block promotion.")
    return {"exam_score": points, "passed": points >= 70, "checks": checks}

def learning_targets(inputs: dict, rubric: dict, grade: dict) -> list[str]:
    targets: list[str] = []
    scores = rubric["scores"]
    if scores["data_freshness"]["score"] < 0.7:
        targets.append("Improve data freshness before trusting any setup decision.")
    if scores["evidence_coverage"]["score"] < 0.5:
        targets.append("Collect more closed Shadow/Paper samples, especially by setup_id.")
    if scores["edge_quality"]["score"] < 0.55:
        targets.append("Do not promote; study why shadow expectancy/profit factor is weak.")
    if scores["risk_discipline"]["score"] < 0.85:
        targets.append("Keep Risk gate strict and block automatic loosening.")
    if not grade["passed"]:
        targets.append("Repeat a similar exam tomorrow and compare the answer against today's failure checks.")
    return targets[:8] or ["Maintain paper-only collection and expand clean labeled samples."]

def render_report(result: dict) -> str:
    lines = [
        "# Daily Agent Exam",
        "",
        f"Generated: {result.get('ts')}",
        f"Local date: `{result.get('local_date')}`",
        f"Exam type: `{result.get('exam_type')}`",
        f"Quality score: `{result.get('quality_score')}` grade=`{result.get('quality_grade')}`",
        f"Exam score: `{result.get('exam_score')}` passed=`{result.get('passed')}`",
        "",
        "## Task",
        "```json",
        json.dumps(result.get("task") or {}, ensure_ascii=True, indent=2, sort_keys=True),
        "```",
        "",
        "## Answer",
        "```json",
        json.dumps(result.get("answer") or {}, ensure_ascii=True, indent=2, sort_keys=True),
        "```",
        "",
        "## Rubric",
    ]
    for name, row in (result.get("rubric") or {}).get("scores", {}).items():
        lines.append(f"- {name}: {row.get('score')} `{row}`")
    lines.extend(["", "## Learning Targets"])
    lines.extend(f"- {item}" for item in result.get("learning_targets") or [])
    return "\n".join(lines) + "\n"

def run_once(force: bool = False, max_log_lines: int = 1500, now: datetime | None = None) -> dict:
    local_date = local_date_key(now)
    latest = read_json(LATEST_JSON)
    if not force and latest.get("local_date") == local_date:
        write_heartbeat("ok", {"last_exam_date": local_date, "skipped": True, "reason": "already_ran_today"})
        return latest
    inputs = load_inputs(max_log_lines)
    rubric = quality_rubric(inputs)
    exam_type = choose_exam_type(inputs, local_date)
    task = build_exam_task(exam_type, inputs, local_date)
    answer = answer_exam(exam_type, task, inputs, rubric)
    grade = grade_exam(exam_type, answer, inputs, rubric)
    ts = utc_now()
    result = {
        "ts": ts,
        "pid": os.getpid(),
        "local_date": local_date,
        "exam_id": f"daily_exam_{local_date.replace('-', '')}",
        "exam_type": exam_type,
        "quality_score": rubric["quality_score"],
        "quality_grade": grade_letter(rubric["quality_score"]),
        "exam_score": grade["exam_score"],
        "passed": grade["passed"],
        "rubric": rubric,
        "task": task,
        "answer": answer,
        "grade": grade,
        "learning_targets": learning_targets(inputs, rubric, grade),
        "contract": {"paper_only": True, "can_place_live_orders": False, "can_loosen_risk": False},
    }
    write_json(LATEST_JSON, result)
    append_jsonl(HISTORY_JSONL, result)
    REPORT_MD.write_text(render_report(result), encoding="utf-8")
    safe_append_snapshot("daily_exam_agent", "daily_exam", result, ts=ts)
    safe_append_event("daily_exam_agent", "daily_exam_completed", {"local_date": local_date, "exam_type": exam_type, "quality_score": rubric["quality_score"], "exam_score": grade["exam_score"], "passed": grade["passed"]}, ts=ts)
    if exam_type == "paper_trade_candidate" and answer.get("action") == "record_shadow_candidate":
        safe_append_event("daily_exam_agent", "paper_exam_trade_candidate", {"symbol": answer.get("symbol"), "side": answer.get("side"), "setup_id": answer.get("setup_id"), "no_execution": True, "reason": answer.get("reason")}, ts=ts)
    write_heartbeat("ok", {"last_exam_date": local_date, "quality_score": rubric["quality_score"], "exam_score": grade["exam_score"], "passed": grade["passed"], "exam_type": exam_type})
    return result

def write_heartbeat(status: str, payload: dict | None = None) -> None:
    row = {"ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json(HEARTBEAT_PATH, row)
    safe_upsert_heartbeat("daily_exam_agent", status, row, ts=row["ts"])

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

def seconds_until_next_midnight(now: datetime | None = None) -> float:
    current = now or local_now()
    tomorrow = current.date() + timedelta(days=1)
    next_midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=current.tzinfo)
    return max(1.0, (next_midnight - current).total_seconds())

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        time.sleep(min(1.0, max(0.0, deadline - time.time())))

def status() -> int:
    pid = read_pid(PID_FILE)
    print(f"daily_exam_agent_pid={pid} running={is_pid_running(pid, 'daily_exam_agent.py')}")
    print(f"latest={LATEST_JSON}")
    print(f"report={REPORT_MD}")
    print(f"heartbeat={HEARTBEAT_PATH}")
    print(f"stop_file={STOP_FILE}")
    return 0

def run_loop(args: argparse.Namespace) -> int:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(PID_FILE)
    if not args.once and existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid, "daily_exam_agent.py"):
        print(f"daily exam agent already running pid={existing_pid}", flush=True)
        return 0
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        try:
            result = run_once(force=args.force, max_log_lines=args.max_log_lines)
            print(f"daily_exam date={result.get('local_date')} type={result.get('exam_type')} quality={result.get('quality_score')} exam={result.get('exam_score')} passed={result.get('passed')}", flush=True)
        except Exception as exc:
            write_heartbeat("error", {"error": str(exc)[:300]})
            print(f"daily_exam_error {str(exc)[:160]}", flush=True)
        if args.once:
            break
        write_heartbeat("ok", {"waiting_for": "next_midnight", "seconds_until_next_midnight": round(seconds_until_next_midnight(), 1), "last_exam_date": read_json(LATEST_JSON).get("local_date")})
        interruptible_sleep(min(args.check_seconds, seconds_until_next_midnight()))
    return 0

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily paper-only exam for trading agent quality")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check-seconds", type=float, default=300.0)
    parser.add_argument("--max-log-lines", type=int, default=1500)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.check_seconds <= 0:
        parser.error("--check-seconds must be positive")
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
