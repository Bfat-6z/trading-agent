"""Cognitive supervisor for the trading agent.

This coordinates the self-thinking loop without placing trades:
observe -> retrieve -> focus -> hypothesize -> critique -> propose tighten-only bias.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from belief_ledger import compact_ledger, load_ledger
from event_store import safe_append_event, safe_append_snapshot, safe_upsert_heartbeat
from hypothesis_engine import generate_hypotheses, read_jsonl_tail as read_hypothesis_jsonl_tail, save_result
from market_learner import safe_float, valid_paper_close
from reasoning_trace import build_reasoning_trace, save_trace
from setup_skill_library import load_library, skill_summary

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"

MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
MARKET_MODEL_PATH = MEMORY_DIR / "market_model.json"
BIAS_PATH = MEMORY_DIR / "execution_bias.json"
DREAM_LATEST = MEMORY_DIR / "dream_cycle_latest.json"
HYPOTHESES_LATEST = MEMORY_DIR / "hypotheses_latest.json"
MANUAL_THESES_PATH = MEMORY_DIR / "manual_theses.jsonl"
SEMANTIC_MEMORY_PATH = MEMORY_DIR / "semantic_memory.json"
SCALP_LOG = STATE_DIR / "scalp_autotrader.jsonl"

COGNITIVE_LATEST = MEMORY_DIR / "cognitive_state_latest.json"
COGNITIVE_HISTORY = MEMORY_DIR / "cognitive_state_history.jsonl"
COGNITIVE_REPORT = MEMORY_DIR / "cognitive_state_latest.md"
HEARTBEAT_PATH = STATE_DIR / "cognitive_supervisor_heartbeat.json"
PID_FILE = STATE_DIR / "cognitive_supervisor.pid"
STOP_FILE = STATE_DIR / "STOP_COGNITIVE_SUPERVISOR"


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


def read_jsonl_tail(path: Path, max_lines: int = 500) -> list[dict]:
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


def summarize_recent_paper(rows: list[dict]) -> dict:
    closes = [row for row in rows if valid_paper_close(row)]
    recent_closes = closes[-20:]
    wins = sum(1 for row in recent_closes if safe_float(row.get("net")) > 0)
    losses = sum(1 for row in recent_closes if safe_float(row.get("net")) < 0)
    net = sum(safe_float(row.get("net")) for row in recent_closes)
    latest_loss = next((row for row in reversed(recent_closes) if safe_float(row.get("net")) < 0), None)
    return {
        "closed_window": len(recent_closes),
        "wins": wins,
        "losses": losses,
        "net": round(net, 8),
        "win_rate": round(wins / len(recent_closes), 4) if recent_closes else 0.0,
        "latest_loss": latest_loss,
        "risk_blocks": sum(1 for row in rows[-200:] if row.get("event") in {"risk_block", "memory_bias_filter"}),
    }


def hypothesis_focus_score(hypothesis: dict) -> float:
    base = safe_float(hypothesis.get("confidence_prior"), 0.5)
    symbols = len(hypothesis.get("symbols") or [])
    source_bonus = 0.05 if hypothesis.get("source") == "manual_thesis" else 0.0
    return base + min(0.12, symbols * 0.015) + source_bonus


def weakest_setup_focus(library: dict) -> dict | None:
    rows = []
    for setup_id, skill in (library.get("skills") or {}).items():
        if not skill.get("enabled", True):
            continue
        stats = skill.get("stats") or {}
        trades = int(stats.get("trades", 0) or 0)
        expectancy = safe_float(stats.get("expectancy"))
        rows.append((trades, expectancy, setup_id))
    if not rows:
        return None
    rows.sort(key=lambda item: (item[0] >= 5, item[1], -item[0]))
    trades, expectancy, setup_id = rows[0]
    return {
        "focus_type": "setup_learning_gap",
        "setup_id": setup_id,
        "reason": "Setup has low sample count or weak expectancy; prioritize learning before trusting it.",
        "trades": trades,
        "expectancy": round(expectancy, 8),
    }


def choose_focus(paper: dict, hypotheses: list[dict], library: dict, dream: dict, bias: dict) -> dict:
    latest_loss = paper.get("latest_loss")
    if latest_loss:
        return {
            "focus_type": "confusing_loss",
            "reason": "Recent paper loss needs critique before more risk is allowed.",
            "event": latest_loss,
        }
    dream_high_risk = safe_float((dream.get("bias_patch") or {}).get("high_risk_count"))
    if dream_high_risk >= 8:
        return {
            "focus_type": "dream_high_risk",
            "reason": "Dream cycle found many high-risk paths; study common block reasons.",
            "high_risk_count": int(dream_high_risk),
        }
    if hypotheses:
        top = sorted(hypotheses, key=hypothesis_focus_score, reverse=True)[0]
        return {
            "focus_type": "hypothesis_test",
            "reason": "Highest-priority hypothesis should drive next paper/replay observations.",
            "hypothesis_id": top.get("hypothesis_id"),
            "setup_id": top.get("setup_id"),
            "symbols": top.get("symbols", []),
            "statement": top.get("statement"),
        }
    setup_focus = weakest_setup_focus(library)
    if setup_focus:
        return setup_focus
    return {
        "focus_type": "observe",
        "reason": "No strong focus available; keep collecting market and paper data.",
        "min_signal_score": bias.get("min_signal_score"),
    }


def build_experiment_plan(focus: dict, hypotheses: list[dict]) -> dict:
    focus_type = focus.get("focus_type")
    if focus_type == "confusing_loss":
        return {
            "mode": "post_trade_review",
            "questions": [
                "Was the setup label valid at entry time?",
                "Was the trade taken during sleep/risk-block conditions?",
                "Did MAE exceed planned stop assumptions?",
            ],
            "outputs": ["loss_reason", "belief_evidence", "setup_adjustment"],
        }
    if focus_type == "dream_high_risk":
        return {
            "mode": "dream_cluster_review",
            "questions": ["Which symbols repeat in high-risk paths?", "Which side is most blocked?", "Are blocks caused by crowding or exhaustion?"],
            "outputs": ["blocked_symbol_review", "bias_tightening", "hypothesis_update"],
        }
    if focus_type == "hypothesis_test":
        hyp = next((item for item in hypotheses if item.get("hypothesis_id") == focus.get("hypothesis_id")), {})
        return {
            "mode": "paper_or_shadow_test",
            "hypothesis_id": focus.get("hypothesis_id"),
            "setup_id": focus.get("setup_id"),
            "symbols": focus.get("symbols", []),
            "metrics": hyp.get("metrics", ["tp_before_sl", "expectancy", "mae", "mfe"]),
            "invalidation": hyp.get("invalidation", []),
        }
    if focus_type == "setup_learning_gap":
        return {
            "mode": "sample_collection",
            "setup_id": focus.get("setup_id"),
            "questions": ["Which regimes produce clean examples?", "What invalidations fire most often?"],
            "outputs": ["setup_examples", "setup_stats"],
        }
    return {"mode": "observe", "outputs": ["more_data"]}


def propose_bias(current_bias: dict, focus: dict, paper: dict, dream: dict) -> dict:
    min_score = int(current_bias.get("min_signal_score") or 6)
    blocked_symbols = [str(item).upper() for item in current_bias.get("blocked_symbols", []) if item]
    blocked_sides = [str(item).upper() for item in current_bias.get("blocked_sides", []) if item]
    reasons = []

    if focus.get("focus_type") == "confusing_loss":
        min_score = max(min_score, 8 if paper.get("losses", 0) >= 2 else 7)
        reasons.append("Recent paper loss under review; tighten entries until critique completes.")
    if focus.get("focus_type") == "dream_high_risk":
        min_score = max(min_score, 8)
        reasons.append("Dream cluster shows high risk; require stronger confirmation.")
    for symbol in (dream.get("bias_patch") or {}).get("blocked_symbols", [])[:10]:
        symbol = str(symbol).upper()
        if symbol and symbol not in blocked_symbols:
            blocked_symbols.append(symbol)
    for side in (dream.get("bias_patch") or {}).get("blocked_sides", []):
        side = str(side).upper()
        if side in {"LONG", "SHORT"} and side not in blocked_sides:
            blocked_sides.append(side)

    return {
        "mode": "tighten_only",
        "min_signal_score": min_score,
        "blocked_symbols": blocked_symbols[:24],
        "blocked_sides": blocked_sides[:2],
        "reasons": reasons or ["No additional tightening proposed."],
        "can_loosen": False,
    }


def render_report(state: dict) -> str:
    focus = state.get("focus") or {}
    proposal = state.get("bias_proposal") or {}
    reasoning = state.get("reasoning_trace") or {}
    decision = reasoning.get("decision") or {}
    lines = [
        "# Cognitive Supervisor",
        "",
        f"Generated: {state.get('ts')}",
        f"Focus: `{focus.get('focus_type', 'unknown')}`",
        f"Reason: {focus.get('reason', '')}",
        f"Thought quality: `{reasoning.get('thought_quality_score', 'n/a')}`",
        f"Decision: `{decision.get('mode', 'n/a')}` allow_paper=`{decision.get('allow_paper_entry', 'n/a')}`",
        "",
        "## Experiment Plan",
        "```json",
        json.dumps(state.get("experiment_plan", {}), ensure_ascii=True, indent=2, sort_keys=True),
        "```",
        "",
        "## Reasoning Gaps",
        "### Contradictions",
    ]
    lines.extend(f"- {item}" for item in (reasoning.get("contradictions") or ["none"]))
    lines.extend(["", "### Missing Evidence"])
    lines.extend(f"- {item}" for item in (reasoning.get("missing_evidence") or ["none"]))
    lines.extend([
        "",
        "### Next Actions",
    ])
    lines.extend(f"- {item}" for item in (reasoning.get("next_actions") or ["none"]))
    lines.extend([
        "",
        "## Bias Proposal",
        "```json",
        json.dumps(proposal, ensure_ascii=True, indent=2, sort_keys=True),
        "```",
    ])
    return "\n".join(lines) + "\n"


def ensure_hypotheses(snapshot: dict, market_model: dict, library: dict, ledger: dict, bias: dict) -> dict:
    latest = read_json(HYPOTHESES_LATEST)
    if latest.get("hypotheses"):
        return latest
    result = generate_hypotheses(
        snapshot,
        market_model,
        library,
        ledger,
        bias,
        read_hypothesis_jsonl_tail(MANUAL_THESES_PATH, 20),
    )
    save_result(result)
    return result


def run_once() -> dict:
    ts = utc_now()
    snapshot = read_json(MARKET_LATEST)
    market_model = read_json(MARKET_MODEL_PATH)
    bias = read_json(BIAS_PATH)
    dream = read_json(DREAM_LATEST)
    semantic_memory = read_json(SEMANTIC_MEMORY_PATH)
    ledger = load_ledger()
    library = load_library()
    paper = summarize_recent_paper(read_jsonl_tail(SCALP_LOG, 500))
    hypotheses_result = ensure_hypotheses(snapshot, market_model, library, ledger, bias)
    hypotheses = hypotheses_result.get("hypotheses", [])
    focus = choose_focus(paper, hypotheses, library, dream, bias)
    experiment_plan = build_experiment_plan(focus, hypotheses)
    bias_proposal = propose_bias(bias, focus, paper, dream)
    state = {
        "ts": ts,
        "pid": os.getpid(),
        "paper": paper,
        "focus": focus,
        "experiment_plan": experiment_plan,
        "bias_proposal": bias_proposal,
        "hypothesis_count": len(hypotheses),
        "belief_summary": compact_ledger(ledger),
        "setup_summary": skill_summary(library),
        "market_regime": (market_model.get("last_market_state") or {}).get("primary_regime"),
        "dream_high_risk_count": (dream.get("bias_patch") or {}).get("high_risk_count"),
    }
    reasoning = build_reasoning_trace(state, snapshot, bias, dream, hypotheses_result, semantic_memory, ts=ts)
    save_trace(reasoning)
    state["reasoning_trace"] = {
        "thought_quality_score": reasoning.get("thought_quality_score"),
        "decision": reasoning.get("decision"),
        "contradictions": reasoning.get("contradictions", []),
        "missing_evidence": reasoning.get("missing_evidence", []),
        "next_actions": reasoning.get("next_actions", []),
    }
    write_json(COGNITIVE_LATEST, state)
    COGNITIVE_REPORT.write_text(render_report(state), encoding="utf-8")
    append_jsonl(COGNITIVE_HISTORY, state)
    safe_append_snapshot("cognitive_supervisor", "cognitive_state", state, ts=ts)
    safe_append_event("cognitive_supervisor", "cognitive_update", {"focus_type": focus.get("focus_type"), "hypothesis_count": len(hypotheses)}, ts=ts)
    write_heartbeat("ok", {"focus_type": focus.get("focus_type"), "hypothesis_count": len(hypotheses), "bias_min_signal_score": bias_proposal.get("min_signal_score")})
    return state


def write_heartbeat(status: str, payload: dict | None = None) -> None:
    row = {"ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json(HEARTBEAT_PATH, row)
    safe_upsert_heartbeat("cognitive_supervisor", status, row, ts=row["ts"])


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
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction Stop; if (-not $p) {{ exit 1 }}{script_check}",
            ],
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
    print(f"cognitive_supervisor_pid={pid} running={is_pid_running(pid, 'cognitive_supervisor.py')}")
    print(f"heartbeat={HEARTBEAT_PATH}")
    print(f"latest={COGNITIVE_LATEST}")
    print(f"report={COGNITIVE_REPORT}")
    print(f"stop_file={STOP_FILE}")
    return 0


def run_loop(args: argparse.Namespace) -> int:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(PID_FILE)
    if not args.once and existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid, "cognitive_supervisor.py"):
        print(f"cognitive supervisor already running pid={existing_pid}", flush=True)
        return 0
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        try:
            state = run_once()
            print(f"cognitive_cycle ts={state['ts']} focus={state['focus'].get('focus_type')} hypotheses={state['hypothesis_count']}", flush=True)
        except Exception as exc:
            write_heartbeat("error", {"error": str(exc)[:300]})
            print(f"cognitive_error {str(exc)[:160]}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_minutes * 60)
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run cognitive supervisor loop")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=20.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be positive")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        return status()
    return run_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
