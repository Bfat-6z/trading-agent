"""Background dream/simulation loop for the trading agent.

The dream cycle runs while the executor is sleeping. It does not place trades
and it never loosens risk controls. Its only allowed bias mutation is to make
execution stricter when simulations show crowded, exhausted, or unwind risk.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from curiosity_scheduler import choose_focus, write_focus
from event_store import safe_append_event, safe_append_snapshot, safe_upsert_heartbeat
from market_learner import classify_market, safe_float

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
BIAS_PATH = MEMORY_DIR / "execution_bias.json"
MARKET_MODEL_PATH = MEMORY_DIR / "market_model.json"
DREAMS_MD = MEMORY_DIR / "DREAMS.md"
DREAM_LATEST_JSON = MEMORY_DIR / "dream_cycle_latest.json"
DREAM_CANDIDATES_JSONL = MEMORY_DIR / "dream_candidates.jsonl"
SIMULATION_RESULTS_JSONL = MEMORY_DIR / "simulation_results.jsonl"
HEARTBEAT_PATH = STATE_DIR / "dream_cycle_heartbeat.json"
PID_FILE = STATE_DIR / "dream_cycle.pid"
STOP_FILE = STATE_DIR / "STOP_DREAM_CYCLE"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")


def row_symbol(row: dict) -> str | None:
    value = row.get("symbol")
    return str(value).upper() if value else None


def rows_by_symbol(snapshot: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for key in ("hot", "top_volume", "top_gainers", "top_losers", "funding_extremes", "majors"):
        for row in snapshot.get(key, []) if isinstance(snapshot, dict) else []:
            symbol = row_symbol(row)
            if symbol:
                result.setdefault(symbol, row)
                result[symbol].update(row)
    return result


def choose_symbols(snapshot: dict, bias: dict, limit: int = 12, focus: dict | None = None) -> list[str]:
    seen: list[str] = []
    focus_symbol = str((focus or {}).get("symbol") or "").upper()
    if focus_symbol:
        seen.append(focus_symbol)
    for key in ("hot", "funding_extremes", "top_volume"):
        for row in snapshot.get(key, []) if isinstance(snapshot, dict) else []:
            symbol = row_symbol(row)
            if symbol and symbol not in seen:
                seen.append(symbol)
            if len(seen) >= limit:
                return seen
    for symbol in bias.get("blocked_symbols", []) if isinstance(bias, dict) else []:
        symbol = str(symbol).upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
        if len(seen) >= limit:
            break
    return seen


def simulate_side(symbol: str, row: dict, side: str, market_state: dict, bias: dict) -> dict:
    change = safe_float(row.get("change_pct"))
    range_pos = safe_float(row.get("range_pos"), 0.5)
    funding = safe_float(row.get("funding_pct"))
    volume_m = safe_float(row.get("quote_volume")) / 1_000_000
    tags = {str(tag) for tag in market_state.get("tags", [])}
    blocked_symbols = {str(item).upper() for item in bias.get("blocked_symbols", []) if item}
    blocked_sides = {str(item).upper() for item in bias.get("blocked_sides", []) if item}

    risk = 2.0
    reward = 2.0
    reasons: list[str] = []

    if symbol in blocked_symbols:
        risk += 2.0
        reasons.append("already_blocked_symbol")
    if side in blocked_sides:
        risk += 2.0
        reasons.append("already_blocked_side")
    if abs(change) >= 50:
        risk += 2.0
        reasons.append("extreme_24h_move")
    elif abs(change) >= 25:
        risk += 1.2
        reasons.append("large_24h_move")
    if abs(funding) >= 0.3:
        risk += 1.8
        reasons.append("very_crowded_funding")
    elif abs(funding) >= 0.15:
        risk += 1.0
        reasons.append("crowded_funding")
    if range_pos >= 0.93 or range_pos <= 0.07:
        risk += 1.4
        reasons.append("range_extreme")
    if "alt_mania" in tags or "liquidation_unwind" in tags:
        risk += 1.1
        reasons.append("unstable_regime")

    if volume_m >= 500:
        reward += 1.2
        reasons.append("deep_liquidity")
    elif volume_m >= 100:
        reward += 0.7
        reasons.append("good_liquidity")
    if side == "LONG" and 0.2 <= range_pos <= 0.78 and 0 <= change <= 12:
        reward += 1.5
        reasons.append("long_not_exhausted")
    if side == "SHORT" and (range_pos >= 0.82 or change >= 18):
        reward += 1.3
        reasons.append("short_exhaustion_candidate")
    if side == "SHORT" and change <= -20 and range_pos <= 0.18:
        risk += 1.5
        reasons.append("late_short_after_unwind")
    if side == "LONG" and (range_pos >= 0.9 or change >= 25):
        risk += 1.8
        reasons.append("late_long_chase")

    quality = round(reward - risk, 4)
    if risk >= 7.0:
        verdict = "block"
    elif quality >= 0.8 and risk <= 5.2:
        verdict = "paper_candidate"
    else:
        verdict = "observe_only"
    return {
        "symbol": symbol,
        "side": side,
        "risk_score": round(risk, 4),
        "reward_score": round(reward, 4),
        "quality_score": quality,
        "verdict": verdict,
        "change_pct": change,
        "range_pos": range_pos,
        "funding_pct": funding,
        "quote_volume_m": round(volume_m, 4),
        "reasons": reasons[:10],
    }


def simulate_market(snapshot: dict, bias: dict, limit: int = 12, focus: dict | None = None) -> dict:
    market_state = classify_market(snapshot)
    by_symbol = rows_by_symbol(snapshot)
    simulations: list[dict] = []
    for symbol in choose_symbols(snapshot, bias, limit=limit, focus=focus):
        row = by_symbol.get(symbol, {"symbol": symbol})
        simulations.append(simulate_side(symbol, row, "LONG", market_state, bias))
        simulations.append(simulate_side(symbol, row, "SHORT", market_state, bias))
    blocks = [sim for sim in simulations if sim["verdict"] == "block"]
    candidates = [sim for sim in simulations if sim["verdict"] == "paper_candidate"]
    return {
        "market_state": market_state,
        "simulations": simulations,
        "blocks": blocks,
        "paper_candidates": sorted(candidates, key=lambda item: item["quality_score"], reverse=True)[:8],
    }


def derive_bias_patch(cycle: dict, current_bias: dict) -> dict:
    simulations = cycle.get("simulations") or []
    blocks = cycle.get("blocks") or []
    blocked_symbols: list[str] = []
    blocked_sides = set(str(side).upper() for side in current_bias.get("blocked_sides", []) if side)
    for sim in blocks:
        symbol = str(sim.get("symbol") or "").upper()
        side = str(sim.get("side") or "").upper()
        if symbol and symbol not in blocked_symbols:
            blocked_symbols.append(symbol)
        if side == "LONG" and sim.get("risk_score", 0) >= 7.5:
            blocked_sides.add("LONG")
    high_risk_count = sum(1 for sim in simulations if safe_float(sim.get("risk_score")) >= 7.0)
    min_score = int(current_bias.get("min_signal_score") or 6)
    if high_risk_count >= 8:
        min_score = max(min_score, 8)
    elif high_risk_count >= 4:
        min_score = max(min_score, 7)
    return {
        "min_signal_score": min_score,
        "blocked_symbols": blocked_symbols[:16],
        "blocked_sides": sorted(blocked_sides),
        "high_risk_count": high_risk_count,
        "paper_candidates": cycle.get("paper_candidates", [])[:5],
    }


def tighten_bias(current_bias: dict, patch: dict, ts: str) -> dict:
    updated = dict(current_bias)
    updated["min_signal_score"] = max(int(current_bias.get("min_signal_score") or 6), int(patch.get("min_signal_score") or 6))
    current_symbols = [str(item).upper() for item in current_bias.get("blocked_symbols", []) if item]
    patch_symbols = [str(item).upper() for item in patch.get("blocked_symbols", []) if item]
    updated["blocked_symbols"] = list(dict.fromkeys([*current_symbols, *patch_symbols]))[:20]
    current_sides = [str(item).upper() for item in current_bias.get("blocked_sides", []) if item]
    patch_sides = [str(item).upper() for item in patch.get("blocked_sides", []) if item]
    updated["blocked_sides"] = list(dict.fromkeys([*current_sides, *patch_sides]))[:2]
    updated["dream_learning"] = {
        "updated_at": ts,
        "high_risk_count": patch.get("high_risk_count", 0),
        "paper_candidates": patch.get("paper_candidates", []),
    }
    reasons = list(updated.get("reasons") or [])
    dream_reason = f"Dream cycle found {patch.get('high_risk_count', 0)} high-risk simulated paths; keep executor strict."
    if dream_reason not in reasons:
        reasons.append(dream_reason)
    updated["reasons"] = reasons[:10]
    return updated


def render_dream(cycle: dict, patch: dict) -> str:
    state = cycle.get("market_state") or {}
    focus = cycle.get("curiosity_focus") or {}
    lines = [
        "# Dream Cycle",
        "",
        f"Generated: {cycle.get('ts')}",
        f"Regime: `{state.get('primary_regime', 'unknown')}` tags={', '.join(state.get('tags') or [])}",
        f"Focus: `{focus.get('focus_type', 'none')}` {focus.get('focus_id', '')}",
        f"High-risk paths: {patch.get('high_risk_count', 0)}",
        f"Bias min score after dream: {patch.get('min_signal_score')}",
        "",
        "## Simulated Blocks",
    ]
    for sim in (cycle.get("blocks") or [])[:10]:
        lines.append(f"- {sim['symbol']} {sim['side']}: risk={sim['risk_score']} quality={sim['quality_score']} reasons={', '.join(sim['reasons'][:4])}")
    if not cycle.get("blocks"):
        lines.append("- No block-level simulated risk.")
    lines.extend(["", "## Paper Candidates"])
    for sim in patch.get("paper_candidates", [])[:5]:
        lines.append(f"- {sim['symbol']} {sim['side']}: quality={sim['quality_score']} risk={sim['risk_score']} reward={sim['reward_score']}")
    if not patch.get("paper_candidates"):
        lines.append("- No simulated paper candidate strong enough.")
    return "\n".join(lines) + "\n"


def write_heartbeat(status: str, payload: dict | None = None) -> None:
    row = {"ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json(HEARTBEAT_PATH, row)
    safe_upsert_heartbeat("dream_cycle", status, row, ts=row["ts"])


def run_once(apply_bias: bool = True, limit: int = 12) -> dict:
    ts = utc_now()
    snapshot = read_json(MARKET_LATEST)
    bias = read_json(BIAS_PATH)
    focus = write_focus(choose_focus(), ts)
    cycle = {"ts": ts, "curiosity_focus": focus, **simulate_market(snapshot, bias, limit=limit, focus=focus)}
    patch = derive_bias_patch(cycle, bias)
    report = render_dream(cycle, patch)

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    result = {"ts": ts, "cycle": cycle, "bias_patch": patch, "applied_bias": False}
    write_json(DREAM_LATEST_JSON, result)
    append_jsonl(DREAM_CANDIDATES_JSONL, {"ts": ts, "paper_candidates": patch.get("paper_candidates", [])})
    append_jsonl(SIMULATION_RESULTS_JSONL, {"ts": ts, "simulations": cycle.get("simulations", [])})
    with DREAMS_MD.open("a", encoding="utf-8") as fh:
        fh.write("\n\n" + report)

    if apply_bias and bias:
        tightened = tighten_bias(bias, patch, ts)
        if tightened != bias:
            write_json(BIAS_PATH, tightened)
            result["applied_bias"] = True
            write_json(DREAM_LATEST_JSON, result)

    safe_append_snapshot("dream_cycle", "dream", result, ts=ts)
    safe_append_event("dream_cycle", "dream_update", {"high_risk_count": patch.get("high_risk_count"), "applied_bias": result["applied_bias"], "focus": focus}, ts=ts)
    write_heartbeat("ok", {"high_risk_count": patch.get("high_risk_count"), "applied_bias": result["applied_bias"], "focus_type": focus.get("focus_type")})
    return result


def heartbeat_age_seconds() -> float | None:
    row = read_json(HEARTBEAT_PATH)
    try:
        ts = datetime.fromisoformat(str(row.get("ts")).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


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
    print(f"dream_cycle_pid={pid} running={is_pid_running(pid, 'dream_cycle.py')}")
    print(f"heartbeat={HEARTBEAT_PATH} age_seconds={heartbeat_age_seconds()}")
    print(f"latest={DREAM_LATEST_JSON}")
    print(f"dreams={DREAMS_MD}")
    print(f"stop_file={STOP_FILE}")
    return 0


def run_loop(args: argparse.Namespace) -> int:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(PID_FILE)
    if not args.once and existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid, "dream_cycle.py"):
        print(f"dream cycle already running pid={existing_pid}", flush=True)
        return 0
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        try:
            result = run_once(apply_bias=not args.no_apply_bias, limit=args.limit)
            print(
                f"dream_cycle ts={result['ts']} high_risk={result['bias_patch'].get('high_risk_count')} applied={result['applied_bias']}",
                flush=True,
            )
        except Exception as exc:
            write_heartbeat("error", {"error": str(exc)[:300]})
            print(f"dream_error {str(exc)[:160]}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_minutes * 60)
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run background dream/simulation cycles while executor sleeps")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--no-apply-bias", action="store_true", help="write dreams only; do not tighten execution bias")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be positive")
    if args.limit < 3:
        parser.error("--limit must be >= 3")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        return status()
    return run_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
