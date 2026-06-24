"""Structured reasoning trace for the trading agent.

This module is not a consciousness layer. It records a deterministic chain from
current evidence to the next allowed action so the agent can audit its own
paper/shadow decisions and learn from missing evidence.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from event_store import safe_append_event, safe_append_snapshot
from market_learner import safe_float

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
BIAS_PATH = MEMORY_DIR / "execution_bias.json"
DREAM_LATEST = MEMORY_DIR / "dream_cycle_latest.json"
HYPOTHESES_LATEST = MEMORY_DIR / "hypotheses_latest.json"
SEMANTIC_MEMORY_PATH = MEMORY_DIR / "semantic_memory.json"
COGNITIVE_LATEST = MEMORY_DIR / "cognitive_state_latest.json"
REASONING_LATEST = MEMORY_DIR / "reasoning_trace_latest.json"
REASONING_REPORT = MEMORY_DIR / "reasoning_trace_latest.md"
REASONING_HISTORY = MEMORY_DIR / "reasoning_trace_history.jsonl"


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


def parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def sleep_active(bias: dict, now: datetime | None = None) -> bool:
    target = parse_ts(bias.get("sleep_until"))
    return bool(target and target > (now or datetime.now(timezone.utc)))


def summarize_market(snapshot: dict) -> dict:
    majors = snapshot.get("majors") if isinstance(snapshot.get("majors"), list) else []
    hot = snapshot.get("hot") if isinstance(snapshot.get("hot"), list) else []
    major_changes = [safe_float(row.get("change_pct")) for row in majors]
    return {
        "ts": snapshot.get("ts"),
        "universe_count": snapshot.get("universe_count"),
        "major_avg_24h_pct": round(sum(major_changes) / len(major_changes), 4) if major_changes else 0.0,
        "hot_symbols": [row.get("symbol") for row in hot[:8] if row.get("symbol")],
    }


def top_beliefs(ledger_summary: dict, limit: int = 5) -> list[dict]:
    rows = ledger_summary.get("top_beliefs") or ledger_summary.get("beliefs") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    result = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "belief_id": row.get("belief_id"),
                "statement": row.get("statement"),
                "confidence": safe_float(row.get("confidence"), 0.5),
                "status": row.get("status"),
            }
        )
    result.sort(key=lambda item: safe_float(item.get("confidence"), 0.5), reverse=True)
    return result[:limit]


def hypothesis_quality(hypothesis: dict) -> dict:
    metrics = hypothesis.get("metrics") if isinstance(hypothesis.get("metrics"), list) else []
    invalidation = hypothesis.get("invalidation") if isinstance(hypothesis.get("invalidation"), list) else []
    return {
        "hypothesis_id": hypothesis.get("hypothesis_id"),
        "setup_id": hypothesis.get("setup_id"),
        "symbols": hypothesis.get("symbols", []),
        "confidence_prior": safe_float(hypothesis.get("confidence_prior"), 0.5),
        "has_metrics": bool(metrics),
        "has_invalidation": bool(invalidation),
    }


def build_observations(cognitive_state: dict, snapshot: dict, bias: dict, dream: dict, semantic_memory: dict) -> dict:
    latest_memory = semantic_memory.get("latest") if isinstance(semantic_memory.get("latest"), dict) else {}
    return {
        "market": summarize_market(snapshot),
        "paper": cognitive_state.get("paper", {}),
        "bias": {
            "risk_posture": bias.get("risk_posture"),
            "min_signal_score": bias.get("min_signal_score"),
            "sleep_until": bias.get("sleep_until"),
            "sleep_active": sleep_active(bias),
            "blocked_sides": bias.get("blocked_sides", []),
            "blocked_symbols_count": len(bias.get("blocked_symbols", []) or []),
        },
        "dream": {
            "high_risk_count": (dream.get("bias_patch") or {}).get("high_risk_count"),
            "paper_candidates": (dream.get("bias_patch") or {}).get("paper_candidates", [])[:3],
            "focus": (dream.get("cycle") or {}).get("curiosity_focus", {}),
        },
        "semantic_memory": {
            "event_count": latest_memory.get("event_count"),
            "risk_blocks": latest_memory.get("risk_blocks", {}),
            "repeated_lessons": latest_memory.get("repeated_lessons", [])[:5],
        },
    }


def find_contradictions(observations: dict, hypotheses: list[dict]) -> list[str]:
    contradictions: list[str] = []
    bias = observations.get("bias", {})
    if bias.get("sleep_active") and observations.get("dream", {}).get("paper_candidates"):
        contradictions.append("dream_has_paper_candidates_while_executor_is_asleep")
    blocked_sides = {str(side).upper() for side in bias.get("blocked_sides", [])}
    for hyp in hypotheses:
        side = str((hyp.get("prediction") or {}).get("side") or "").upper()
        if side and side in blocked_sides:
            contradictions.append(f"hypothesis_side_{side}_is_currently_blocked")
            break
    if observations.get("paper", {}).get("losses", 0) > observations.get("paper", {}).get("wins", 0) and bias.get("risk_posture") != "defensive":
        contradictions.append("paper_losses_exceed_wins_but_bias_is_not_defensive")
    return contradictions[:8]


def missing_evidence(observations: dict, hypotheses: list[dict]) -> list[str]:
    missing: list[str] = []
    paper = observations.get("paper", {})
    if int(paper.get("closed_window", 0) or 0) < 20:
        missing.append("need_at_least_20_recent_closed_paper_trades_for_reliable_window")
    if not hypotheses:
        missing.append("need_falsifiable_hypotheses_for_next_cycle")
    elif any(not hypothesis_quality(item)["has_metrics"] or not hypothesis_quality(item)["has_invalidation"] for item in hypotheses[:5]):
        missing.append("some_hypotheses_need_metrics_and_invalidation_rules")
    if not observations.get("semantic_memory", {}).get("event_count"):
        missing.append("semantic_memory_has_no_recent_compacted_events")
    if not observations.get("market", {}).get("ts"):
        missing.append("market_snapshot_timestamp_missing")
    return missing[:8]


def decide_next_action(observations: dict, contradictions: list[str], missing: list[str]) -> dict:
    bias = observations.get("bias", {})
    if bias.get("sleep_active"):
        return {
            "mode": "sleep_observe_and_shadow",
            "allow_paper_entry": False,
            "reason": "executor is in memory sleep; continue observation, dream, and shadow evidence collection",
        }
    if contradictions:
        return {
            "mode": "resolve_contradictions_first",
            "allow_paper_entry": False,
            "reason": "active contradictions must be reviewed before paper entry",
        }
    if "need_at_least_20_recent_closed_paper_trades_for_reliable_window" in missing:
        return {
            "mode": "paper_scan_with_shadow_logging",
            "allow_paper_entry": True,
            "reason": "paper entries are allowed by sleep gate, but every blocked signal should still be shadow logged for samples",
        }
    return {
        "mode": "paper_scan_allowed",
        "allow_paper_entry": True,
        "reason": "no sleep or hard contradiction detected; inner critic still owns final entry decision",
    }


def quality_score(observations: dict, hypotheses: list[dict], contradictions: list[str], missing: list[str]) -> float:
    score = 0.25
    if observations.get("market", {}).get("ts"):
        score += 0.15
    if observations.get("semantic_memory", {}).get("event_count"):
        score += 0.15
    if hypotheses:
        score += 0.15
    if hypotheses and all(hypothesis_quality(item)["has_metrics"] and hypothesis_quality(item)["has_invalidation"] for item in hypotheses[:5]):
        score += 0.15
    if observations.get("paper", {}).get("closed_window", 0) >= 20:
        score += 0.15
    score -= min(0.2, len(contradictions) * 0.05)
    score -= min(0.2, len(missing) * 0.03)
    return round(max(0.0, min(1.0, score)), 4)


def build_reasoning_trace(
    cognitive_state: dict,
    snapshot: dict,
    bias: dict,
    dream: dict,
    hypotheses_result: dict,
    semantic_memory: dict,
    ts: str | None = None,
) -> dict:
    row_ts = ts or utc_now()
    hypotheses = hypotheses_result.get("hypotheses") if isinstance(hypotheses_result.get("hypotheses"), list) else []
    observations = build_observations(cognitive_state, snapshot, bias, dream, semantic_memory)
    contradictions = find_contradictions(observations, hypotheses)
    missing = missing_evidence(observations, hypotheses)
    decision = decide_next_action(observations, contradictions, missing)
    trace = {
        "ts": row_ts,
        "focus": cognitive_state.get("focus", {}),
        "question": (cognitive_state.get("experiment_plan") or {}).get("mode", "observe"),
        "observations": observations,
        "beliefs_used": top_beliefs(cognitive_state.get("belief_summary", {})),
        "hypotheses_to_test": [hypothesis_quality(item) for item in hypotheses[:5]],
        "contradictions": contradictions,
        "missing_evidence": missing,
        "decision": decision,
        "next_actions": next_actions(cognitive_state.get("focus", {}), decision, missing),
    }
    trace["thought_quality_score"] = quality_score(observations, hypotheses, contradictions, missing)
    return trace


def next_actions(focus: dict, decision: dict, missing: list[str]) -> list[str]:
    actions = []
    focus_type = focus.get("focus_type")
    if focus_type in {"under_sampled_setup", "setup_learning_gap"}:
        setup_id = focus.get("setup_id") or focus.get("focus_id")
        actions.append(f"collect_shadow_examples_for_setup:{setup_id}")
    if focus_type == "confusing_loss":
        actions.append("run_post_trade_loss_review")
    if decision.get("mode") == "sleep_observe_and_shadow":
        actions.append("do_not_open_paper_until_sleep_expires")
        actions.append("log_would_trade_candidates_for_later_replay")
    if "need_at_least_20_recent_closed_paper_trades_for_reliable_window" in missing:
        actions.append("prioritize_sample_collection_before_live_readiness_claims")
    if not actions:
        actions.append("continue_inner_critic_gated_paper_scan")
    return actions[:8]


def render_markdown(trace: dict) -> str:
    decision = trace.get("decision", {})
    obs = trace.get("observations", {})
    lines = [
        "# Reasoning Trace",
        "",
        f"Generated: {trace.get('ts')}",
        f"Focus: `{(trace.get('focus') or {}).get('focus_type', 'unknown')}`",
        f"Question: `{trace.get('question')}`",
        f"Thought quality: {trace.get('thought_quality_score')}",
        "",
        "## Decision",
        f"- Mode: `{decision.get('mode')}`",
        f"- Allow paper entry: `{decision.get('allow_paper_entry')}`",
        f"- Reason: {decision.get('reason')}",
        "",
        "## Observations",
        f"- Market ts: {(obs.get('market') or {}).get('ts')} hot={', '.join((obs.get('market') or {}).get('hot_symbols') or [])}",
        f"- Bias: sleep_active={(obs.get('bias') or {}).get('sleep_active')} min_score={(obs.get('bias') or {}).get('min_signal_score')} blocked_sides={', '.join((obs.get('bias') or {}).get('blocked_sides') or [])}",
        f"- Paper: closes={(obs.get('paper') or {}).get('closed_window')} wins={(obs.get('paper') or {}).get('wins')} losses={(obs.get('paper') or {}).get('losses')} net={(obs.get('paper') or {}).get('net')}",
        "",
        "## Contradictions",
    ]
    lines.extend(f"- {item}" for item in (trace.get("contradictions") or ["none"]))
    lines.extend(["", "## Missing Evidence"])
    lines.extend(f"- {item}" for item in (trace.get("missing_evidence") or ["none"]))
    lines.extend(["", "## Next Actions"])
    lines.extend(f"- {item}" for item in trace.get("next_actions", []))
    return "\n".join(lines) + "\n"


def save_trace(trace: dict, latest_path: Path = REASONING_LATEST, report_path: Path = REASONING_REPORT, history_path: Path = REASONING_HISTORY) -> dict:
    write_json(latest_path, trace)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown(trace), encoding="utf-8")
    append_jsonl(history_path, trace)
    if latest_path.resolve() == REASONING_LATEST.resolve():
        safe_append_snapshot("reasoning_trace", "reasoning_trace", trace, ts=trace.get("ts"))
        safe_append_event("reasoning_trace", "reasoning_update", {"decision": trace.get("decision"), "quality": trace.get("thought_quality_score")}, ts=trace.get("ts"))
    return trace


def run_once() -> dict:
    trace = build_reasoning_trace(
        read_json(COGNITIVE_LATEST),
        read_json(MARKET_LATEST),
        read_json(BIAS_PATH),
        read_json(DREAM_LATEST),
        read_json(HYPOTHESES_LATEST),
        read_json(SEMANTIC_MEMORY_PATH),
    )
    return save_trace(trace)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a structured reasoning trace for the trading agent")
    parser.add_argument("--status", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    trace = read_json(REASONING_LATEST) if args.status else run_once()
    print(json.dumps(trace or {"status": "no_trace"}, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
