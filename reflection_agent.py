"""Memory, sleep, and reflection loop for the trading agent.

This is not consciousness. It is a practical memory system: read yesterday's
market/trade logs, extract lessons, write a dream journal, and publish an
execution bias file that other agents can consume.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from event_store import query_recent_events, safe_append_event, safe_append_snapshot, safe_upsert_heartbeat
from market_learner import update_market_model, valid_paper_close, valid_paper_open
from memory_compactor import compact_memory

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PROFILE_PATH = MEMORY_DIR / "profile.json"
BIAS_PATH = MEMORY_DIR / "execution_bias.json"
REFLECTION_LATEST_MD = MEMORY_DIR / "daily_reflection_latest.md"
DREAM_JOURNAL_MD = MEMORY_DIR / "dream_journal.md"
LESSONS_JSONL = MEMORY_DIR / "lessons.jsonl"
STOP_FILE = STATE_DIR / "STOP_REFLECTION_AGENT"
PID_FILE = STATE_DIR / "reflection_agent.pid"
HEARTBEAT_PATH = STATE_DIR / "reflection_agent_heartbeat.json"

SCALP_LOG = STATE_DIR / "scalp_autotrader.jsonl"
MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
MARKET_LOG = STATE_DIR / "market_updates.jsonl"
IMPORTANT_TRADE_EVENTS = {"start", "signal", "paper_open", "paper_close", "live_open", "live_skip", "risk_block", "memory_bias_filter"}


@dataclass
class TradeStats:
    paper_opens: int
    paper_closes: int
    wins: int
    losses: int
    net: float
    last_risk_block: dict | None
    signal_counts: dict[str, int]
    symbols_seen: dict[str, int]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_jsonl_tail(path: Path, max_lines: int = 500) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


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


def read_recent_events(path: Path, max_events: int = 500, lookback_hours: float = 24.0) -> list[dict]:
    """Read recent important events without letting repeated risk_block lines drown out closes."""
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    buckets: dict[str, deque] = {event: deque(maxlen=max(20, max_events // len(IMPORTANT_TRADE_EVENTS))) for event in IMPORTANT_TRADE_EVENTS}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        event = row.get("event")
        if event not in IMPORTANT_TRADE_EVENTS:
            continue
        ts = parse_ts(row.get("ts"))
        if ts and ts < cutoff:
            continue
        buckets[event].append(row)
    rows = [row for bucket in buckets.values() for row in bucket]
    rows.sort(key=lambda row: row.get("ts", ""))
    return rows[-max_events:]

def read_recent_trade_events(max_events: int = 500, lookback_hours: float = 24.0) -> list[dict]:
    try:
        if SCALP_LOG.resolve() == (STATE_DIR / "scalp_autotrader.jsonl").resolve():
            rows = query_recent_events(
                source="scalp_autotrader",
                events=sorted(IMPORTANT_TRADE_EVENTS),
                lookback_hours=lookback_hours,
                limit=max_events,
            )
            if rows:
                return rows
    except Exception:
        pass
    return read_recent_events(SCALP_LOG, max_events, lookback_hours)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def default_profile() -> dict:
    return {
        "identity": {
            "name": "APlus Memory Agent",
            "created_at": utc_now(),
            "purpose": "Remember trading behavior, market context, and lessons; publish conservative execution bias.",
        },
        "cycles": 0,
        "risk_posture": "defensive",
        "beliefs": [
            "A+ pure means mechanical confirmation, catalyst/context check, SL/TP, and no hidden operational risk.",
            "LLM agents advise; deterministic execution and risk gates decide.",
            "After repeated losses, the correct action is sleep, observe, and tighten filters.",
        ],
        "lessons": [],
        "symbol_notes": {},
        "last_reflection_at": None,
    }


def load_profile() -> dict:
    profile = read_json(PROFILE_PATH)
    if not profile:
        return default_profile()
    base = default_profile()
    base.update(profile)
    base.setdefault("identity", default_profile()["identity"])
    base.setdefault("beliefs", default_profile()["beliefs"])
    base.setdefault("lessons", [])
    base.setdefault("symbol_notes", {})
    return base


def summarize_trades(events: list[dict]) -> TradeStats:
    wins = 0
    losses = 0
    net = 0.0
    signal_counts: Counter[str] = Counter()
    symbols_seen: Counter[str] = Counter()
    last_risk_block = None
    paper_opens = 0
    paper_closes = 0
    for row in events:
        event = row.get("event")
        if event == "signal":
            sig = row.get("signal", {})
            symbol = str(sig.get("symbol", "UNKNOWN"))
            side = str(sig.get("side", "UNKNOWN"))
            signal_counts[f"{symbol}:{side}"] += 1
            symbols_seen[symbol] += 1
        elif event == "paper_open" and valid_paper_open(row):
            paper_opens += 1
            pos = row.get("position", {})
            if pos.get("symbol"):
                symbols_seen[str(pos["symbol"])] += 1
        elif event == "paper_close" and valid_paper_close(row):
            paper_closes += 1
            value = float(row.get("net", 0) or 0)
            net += value
            if value > 0:
                wins += 1
            elif value < 0:
                losses += 1
        elif event == "risk_block":
            last_risk_block = row
    return TradeStats(
        paper_opens=paper_opens,
        paper_closes=paper_closes,
        wins=wins,
        losses=losses,
        net=net,
        last_risk_block=last_risk_block,
        signal_counts=dict(signal_counts),
        symbols_seen=dict(symbols_seen),
    )


def extract_market_context(snapshot: dict) -> dict:
    majors = snapshot.get("majors", []) if isinstance(snapshot, dict) else []
    hot = snapshot.get("hot", []) if isinstance(snapshot, dict) else []
    top_major_moves = {row.get("symbol"): row.get("change_pct") for row in majors[:4]}
    hot_symbols = [row.get("symbol") for row in hot[:8] if row.get("symbol")]
    hot_extremes = [
        row.get("symbol")
        for row in hot[:12]
        if row.get("symbol") and (abs(float(row.get("change_pct") or 0)) >= 30 or float(row.get("range_pos") or 0.5) >= 0.9 or float(row.get("range_pos") or 0.5) <= 0.1)
    ]
    return {
        "ts": snapshot.get("ts") if isinstance(snapshot, dict) else None,
        "universe_count": snapshot.get("universe_count") if isinstance(snapshot, dict) else None,
        "major_24h_pct": top_major_moves,
        "hot_symbols": hot_symbols,
        "hot_extreme_symbols": hot_extremes,
    }


def derive_lessons(stats: TradeStats, market: dict) -> list[str]:
    lessons: list[str] = []
    if stats.losses >= 2 or (stats.last_risk_block and stats.last_risk_block.get("reason") == "max_consecutive_losses"):
        lessons.append("Two-loss sequence detected: enter sleep mode, stop forcing entries, and require stronger score on next wake cycle.")
    if stats.paper_closes and stats.net <= 0:
        lessons.append("Recent closed paper trades are not net positive after fees; keep live trading disabled until expectancy improves.")
    if stats.paper_opens > stats.paper_closes + 1:
        lessons.append("There are more opens than closes in the recent window; inspect monitor timing before trusting performance stats.")
    major_moves = market.get("major_24h_pct") or {}
    if any(float(v or 0) < -2.0 for v in major_moves.values()):
        lessons.append("Majors are broadly red; alt scalp longs need stricter confirmation or should be avoided.")
    hot_symbols = market.get("hot_symbols") or []
    if hot_symbols:
        lessons.append(f"Current attention list: {', '.join(hot_symbols[:5])}; observe volatility first, do not chase extremes blindly.")
    if not lessons:
        lessons.append("No major failure pattern detected; continue paper observation and collect more samples.")
    return lessons


def dream_scenarios(stats: TradeStats, market: dict, count: int) -> list[dict]:
    hot_symbols = list(market.get("hot_symbols") or [])[: max(count, 1)]
    if not hot_symbols:
        hot_symbols = [symbol for symbol, _ in sorted(stats.symbols_seen.items(), key=lambda item: item[1], reverse=True)[:count]]
    dreams: list[dict] = []
    for symbol in hot_symbols[:count]:
        dreams.append(
            {
                "symbol": symbol,
                "scenario": f"Imagine {symbol} gives a fast signal while majors are weak.",
                "failure_mode": "Entry is technically valid on 1m but gets reversed by broader market flow or exhaustion.",
                "next_rule": "Require score >= 7 plus tight spread and no recent two-loss block before considering the signal.",
            }
        )
    return dreams


def publish_bias(stats: TradeStats, lessons: list[str], market: dict, sleep_hours: float = 6.0, learning_model: dict | None = None) -> dict:
    defensive = stats.losses >= 2 or bool(stats.last_risk_block)
    major_moves = market.get("major_24h_pct") or {}
    block_longs = any(float(value or 0) < -2.0 for value in major_moves.values())
    learning_rules = (learning_model or {}).get("last_rules") or {}
    sleep_until = None
    if defensive:
        sleep_until = (datetime.now(timezone.utc) + timedelta(hours=sleep_hours)).isoformat(timespec="seconds")
    blocked_sides = ["LONG"] if block_longs else []
    for side in learning_rules.get("blocked_sides") or []:
        side = str(side).upper()
        if side in {"LONG", "SHORT"} and side not in blocked_sides:
            blocked_sides.append(side)
    blocked_symbols = list(dict.fromkeys([*(market.get("hot_extreme_symbols") or []), *(learning_rules.get("blocked_symbols") or [])]))[:16]
    try:
        learned_min_score = int(learning_rules.get("min_signal_score") or 0)
    except Exception:
        learned_min_score = 0
    reasons = list(lessons)
    for rule in learning_rules.get("rules") or []:
        if rule not in reasons:
            reasons.append(rule)
    bias = {
        "updated_at": utc_now(),
        "risk_posture": "defensive" if defensive else "normal",
        "allow_new_entries": True,
        "min_signal_score": max(7 if defensive else 6, learned_min_score),
        "paper_sleep_after_losses": defensive,
        "sleep_until": sleep_until,
        "blocked_sides": blocked_sides,
        "blocked_symbols": blocked_symbols,
        "max_trades_until_next_reflection": 1 if defensive else 3,
        "market_learning": {
            "regime": (learning_model or {}).get("last_market_state", {}).get("primary_regime"),
            "tags": (learning_model or {}).get("last_market_state", {}).get("tags", []),
            "model_updated_at": (learning_model or {}).get("updated_at"),
        },
        "reasons": reasons[:8],
    }
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    BIAS_PATH.write_text(json.dumps(bias, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return bias


def update_profile(profile: dict, lessons: list[str], dreams: list[dict], stats: TradeStats, market: dict) -> dict:
    now = utc_now()
    known_lessons = list(profile.get("lessons", []))
    for lesson in lessons:
        if lesson not in known_lessons:
            known_lessons.append(lesson)
    profile["lessons"] = known_lessons[-100:]
    profile["cycles"] = int(profile.get("cycles", 0)) + 1
    profile["last_reflection_at"] = now
    profile["risk_posture"] = "defensive" if stats.losses >= 2 or stats.last_risk_block else "normal"
    notes = defaultdict(dict, profile.get("symbol_notes", {}))
    for symbol, count in stats.symbols_seen.items():
        existing = dict(notes.get(symbol, {}))
        existing["recent_mentions"] = count
        existing["last_seen_at"] = now
        notes[symbol] = existing
    for symbol in market.get("hot_symbols") or []:
        existing = dict(notes.get(symbol, {}))
        existing["hot_watchlist"] = True
        existing["last_hot_at"] = now
        notes[symbol] = existing
    profile["symbol_notes"] = dict(sorted(notes.items())[-200:])
    profile["last_dreams"] = dreams
    return profile


def render_reflection(profile: dict, stats: TradeStats, market: dict, lessons: list[str], dreams: list[dict], bias: dict, learning_model: dict | None = None) -> str:
    market_state = (learning_model or {}).get("last_market_state") or {}
    learning_rules = (learning_model or {}).get("last_rules") or {}
    lines = [
        "# Daily Reflection",
        "",
        f"Generated: {utc_now()}",
        f"Cycle: {profile.get('cycles')}",
        f"Risk posture: `{profile.get('risk_posture')}`",
        "",
        "## What I Remember",
        f"- Paper opens: {stats.paper_opens}",
        f"- Paper closes: {stats.paper_closes}",
        f"- Recent W/L: {stats.wins}/{stats.losses}",
        f"- Recent net: {stats.net:+.6f}",
    ]
    if stats.last_risk_block:
        lines.append(f"- Last risk block: `{stats.last_risk_block.get('reason')}` at {stats.last_risk_block.get('ts')}")
    lines.extend(["", "## Market Context"])
    for symbol, change in (market.get("major_24h_pct") or {}).items():
        lines.append(f"- {symbol}: {float(change or 0):+.2f}% 24h")
    if market.get("hot_symbols"):
        lines.append(f"- Hot symbols: {', '.join(market['hot_symbols'][:8])}")
    if market_state:
        lines.extend([
            "",
            "## Market Learning",
            f"- Regime: `{market_state.get('primary_regime')}` tags={', '.join(market_state.get('tags') or [])}",
            f"- Learned min score: {learning_rules.get('min_signal_score')}",
            f"- Learned blocked symbols: {', '.join(learning_rules.get('blocked_symbols') or []) or 'none'}",
        ])
    lines.extend(["", "## Lessons"])
    lines.extend(f"- {lesson}" for lesson in lessons)
    lines.extend(["", "## Dreams / Simulations"])
    for dream in dreams:
        lines.append(f"- {dream['symbol']}: {dream['scenario']} Failure: {dream['failure_mode']} Rule: {dream['next_rule']}")
    lines.extend(["", "## Published Execution Bias", "```json", json.dumps(bias, ensure_ascii=True, indent=2, sort_keys=True), "```", ""])
    return "\n".join(lines)


def append_lessons(lessons: list[str]) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with LESSONS_JSONL.open("a", encoding="utf-8") as fh:
        for lesson in lessons:
            ts = utc_now()
            fh.write(json.dumps({"ts": ts, "lesson": lesson}, ensure_ascii=True, sort_keys=True) + "\n")
            safe_append_event("reflection_agent", "lesson", {"lesson": lesson}, ts=ts)


def append_dreams(markdown: str) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with DREAM_JOURNAL_MD.open("a", encoding="utf-8") as fh:
        fh.write("\n\n" + markdown + "\n")


def write_heartbeat(status: str, payload: dict | None = None) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    HEARTBEAT_PATH.write_text(json.dumps(row, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    safe_upsert_heartbeat("reflection_agent", status, row, ts=row["ts"])


def heartbeat_age_seconds() -> float | None:
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        row = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
        ts = parse_ts(row.get("ts"))
        if not ts:
            return None
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def interruptible_sleep(seconds: float, stop_file: Path) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not stop_file.exists():
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def run_once(args: argparse.Namespace) -> dict:
    profile = load_profile()
    trade_events = read_recent_trade_events(args.trade_events, getattr(args, "lookback_hours", 24.0))
    stats = summarize_trades(trade_events)
    snapshot = read_json(MARKET_LATEST)
    market = extract_market_context(snapshot)
    learning_model = update_market_model(snapshot, trade_events, MEMORY_DIR / "market_model.json", MEMORY_DIR / "market_learning_latest.md")
    lessons = derive_lessons(stats, market)
    dreams = dream_scenarios(stats, market, args.dreams)
    bias = publish_bias(stats, lessons, market, getattr(args, "sleep_hours", 6.0), learning_model)
    profile = update_profile(profile, lessons, dreams, stats, market)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    reflection = render_reflection(profile, stats, market, lessons, dreams, bias, learning_model)
    REFLECTION_LATEST_MD.write_text(reflection, encoding="utf-8")
    append_lessons(lessons)
    append_dreams(reflection)
    semantic_memory = compact_memory()
    safe_append_snapshot(
        "reflection_agent",
        "reflection",
        {
            "profile_cycles": profile.get("cycles"),
            "risk_posture": profile.get("risk_posture"),
            "stats": asdict(stats),
            "market": market,
            "lessons": lessons,
            "dreams": dreams,
            "bias": bias,
            "learning_model": {
                "updated_at": learning_model.get("updated_at"),
                "last_market_state": learning_model.get("last_market_state"),
                "last_rules": learning_model.get("last_rules"),
            },
            "semantic_memory": {
                "updated_at": semantic_memory.get("updated_at"),
                "event_count": (semantic_memory.get("latest") or {}).get("event_count"),
                "promoted_beliefs": (semantic_memory.get("latest") or {}).get("promoted_beliefs", []),
            },
        },
        ts=profile.get("last_reflection_at"),
    )
    return {"profile": profile, "stats": stats, "market": market, "learning_model": learning_model, "lessons": lessons, "dreams": dreams, "bias": bias, "semantic_memory": semantic_memory}


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None


def is_pid_running(pid: int | None, expected_script: str | None = None) -> bool:
    if not pid:
        return False
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
        )
        return result.returncode == 0
    except Exception:
        return False


def status() -> int:
    pid = read_pid(PID_FILE)
    print(f"reflection_agent_pid={pid} running={is_pid_running(pid, 'reflection_agent.py')}")
    print(f"profile={PROFILE_PATH}")
    print(f"bias={BIAS_PATH}")
    print(f"latest_reflection={REFLECTION_LATEST_MD}")
    print(f"dream_journal={DREAM_JOURNAL_MD}")
    print(f"heartbeat={HEARTBEAT_PATH} age_seconds={heartbeat_age_seconds()}")
    print(f"stop_file={STOP_FILE}")
    return 0


def run_loop(args: argparse.Namespace) -> int:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = read_pid(PID_FILE)
    if not args.once and existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid, "reflection_agent.py"):
        print(f"reflection agent already running pid={existing_pid}", flush=True)
        return 0
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        try:
            result = run_once(args)
            write_heartbeat(
                "ok",
                {
                    "cycle": result["profile"].get("cycles"),
                    "risk_posture": result["profile"].get("risk_posture"),
                    "lesson_count": len(result["lessons"]),
                    "bias_updated_at": result["bias"].get("updated_at"),
                },
            )
            print(
                f"reflection_cycle cycle={result['profile'].get('cycles')} posture={result['profile'].get('risk_posture')} lessons={len(result['lessons'])}",
                flush=True,
            )
        except Exception as exc:
            write_heartbeat("error", {"error": str(exc)[:300]})
            print(f"reflection_error {str(exc)[:160]}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_hours * 3600, STOP_FILE)
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trading agent memory/sleep/dream reflection loop")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-hours", type=float, default=24.0)
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument("--sleep-hours", type=float, default=6.0)
    parser.add_argument("--trade-events", type=int, default=500)
    parser.add_argument("--dreams", type=int, default=5)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_hours <= 0:
        parser.error("--interval-hours must be positive")
    if args.trade_events < 10:
        parser.error("--trade-events must be >= 10")
    if args.lookback_hours <= 0 or args.sleep_hours <= 0:
        parser.error("--lookback-hours and --sleep-hours must be positive")
    if args.dreams < 1:
        parser.error("--dreams must be >= 1")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        return status()
    return run_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
