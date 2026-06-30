"""Deterministic setup skill library for the trading agent.

This module turns vague "A+" language into named, versioned setup skills with
rules and outcome stats. It does not place trades and it does not rely on LLMs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from event_store import safe_append_event, safe_append_snapshot

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
LIBRARY_PATH = MEMORY_DIR / "setup_skills.json"
REPORT_PATH = MEMORY_DIR / "setup_skills_latest.md"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def contract_hash(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def empty_stats() -> dict:
    return {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "net": 0.0,
        "win_rate": 0.0,
        "expectancy": 0.0,
        "by_regime": {},
        "recent": [],
    }


def skill(
    setup_id: str,
    name: str,
    description: str,
    prerequisites: list[str],
    invalidations: list[str],
    entry_pattern: str,
    stop_template: str,
    target_template: str,
    expected_hold_seconds: int,
    enabled: bool = True,
) -> dict:
    contract = {
        "setup_id": setup_id,
        "semver": "1.0.0",
        "matcher_version": "setup_matcher_v1",
        "ranker_version": "setup_ranker_v1",
        "risk_version": "paper_risk_policy_v1",
        "allowed_sides": ["LONG", "SHORT"],
        "allowed_timeframes": ["1m", "3m", "5m"],
        "required_features": ["symbol", "side", "price", "liquidity", "spread"],
        "entry_pattern": entry_pattern,
        "stop_template": stop_template,
        "target_template": target_template,
        "invalidations": invalidations,
        "no_trade_criteria": invalidations,
        "setup_quality_tier": "unrated",
    }
    return {
        "setup_id": setup_id,
        "version": 1,
        "setup_version": "1.0.0",
        "setup_contract_id": f"{setup_id}.contract.v1",
        "setup_contract_hash": contract_hash(contract),
        "matcher_version": contract["matcher_version"],
        "ranker_version": contract["ranker_version"],
        "risk_version": contract["risk_version"],
        "allowed_sides": contract["allowed_sides"],
        "allowed_timeframes": contract["allowed_timeframes"],
        "required_features": contract["required_features"],
        "setup_quality_tier": contract["setup_quality_tier"],
        "name": name,
        "description": description,
        "enabled": enabled,
        "prerequisites": prerequisites,
        "invalidations": invalidations,
        "entry_pattern": entry_pattern,
        "stop_template": stop_template,
        "target_template": target_template,
        "expected_hold_seconds": expected_hold_seconds,
        "stats": empty_stats(),
    }


def default_skills() -> dict[str, dict]:
    return {
        "momentum_continuation": skill(
            "momentum_continuation",
            "Momentum Continuation",
            "Trade with broad regime and non-exhausted trend continuation.",
            ["regime aligns with side", "range position not exhausted", "liquidity acceptable"],
            ["late chase", "crowded funding against side", "stale market data"],
            "Enter after pullback/reclaim, not at fresh vertical extension.",
            "Stop beyond pullback invalidation or local structure break.",
            "Scale at measured move or prior liquidity pocket.",
            300,
        ),
        "exhaustion_fade": skill(
            "exhaustion_fade",
            "Exhaustion Fade",
            "Fade an overextended move when range and 24h move are extreme.",
            ["range extreme", "large 24h move", "weakening continuation quality"],
            ["fresh catalyst", "deep squeeze still active", "spread unstable"],
            "Enter only after failed continuation or loss of reclaim.",
            "Stop beyond exhaustion high/low.",
            "Target mean reversion to range midpoint or VWAP proxy.",
            240,
        ),
        "liquidation_snapback": skill(
            "liquidation_snapback",
            "Liquidation Snapback",
            "Trade rebound after forced liquidation burst and reclaim.",
            ["liquidation burst", "price stabilizes", "reclaim signal"],
            ["continued forced unwind", "no reclaim", "thin book"],
            "Enter after burst exhaustion and reclaim confirmation.",
            "Stop beyond liquidation sweep extreme.",
            "Target first liquidity gap or pre-burst level.",
            180,
        ),
        "funding_squeeze": skill(
            "funding_squeeze",
            "Funding Squeeze",
            "Trade against crowded funding when price starts moving against crowded side.",
            ["funding crowded", "crowded side vulnerable", "confirmation against crowd"],
            ["OI still expanding with trend", "no price confirmation", "macro shock"],
            "Enter only after confirmation that crowded side is trapped.",
            "Stop if crowd trend resumes.",
            "Target forced squeeze impulse and reduce quickly.",
            240,
        ),
        "range_breakout": skill(
            "range_breakout",
            "Range Breakout",
            "Trade controlled breakout from range with volume support.",
            ["range compression", "breakout level", "volume expansion"],
            ["breakout immediately fails", "low volume", "near major resistance"],
            "Enter after breakout and hold/retest confirmation.",
            "Stop back inside range.",
            "Target range extension.",
            360,
        ),
        "false_breakout": skill(
            "false_breakout",
            "False Breakout",
            "Trade failed breakout back into prior range.",
            ["breakout failure", "range re-entry", "liquidity sweep"],
            ["breakout reclaims", "strong catalyst", "spread unstable"],
            "Enter after failed breakout loses level and rejects reclaim.",
            "Stop beyond failed breakout extreme.",
            "Target range midpoint then opposite side.",
            300,
        ),
        "news_catalyst_chase": skill(
            "news_catalyst_chase",
            "News Catalyst Chase",
            "Trade short-lived catalyst momentum only when liquidity and risk are acceptable.",
            ["fresh catalyst", "liquidity deep", "spread stable"],
            ["unclear source", "headline chaos", "already exhausted"],
            "Enter only on verified catalyst plus controlled pullback.",
            "Stop if catalyst impulse fails.",
            "Take profit quickly into liquidity expansion.",
            180,
        ),
    }


def default_library() -> dict:
    return {"created_at": utc_now(), "updated_at": None, "version": 1, "skills": default_skills(), "history": []}


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def merge_skill(default: dict, persisted: dict) -> dict:
    merged = dict(default)
    for key, value in persisted.items():
        if key == "stats":
            stats = empty_stats()
            if isinstance(value, dict):
                stats.update(value)
                if not isinstance(stats.get("by_regime"), dict):
                    stats["by_regime"] = {}
                if not isinstance(stats.get("recent"), list):
                    stats["recent"] = []
            merged["stats"] = stats
        elif key in {"enabled", "version", "metadata"}:
            merged[key] = value
    return merged


def load_library(path: Path = LIBRARY_PATH) -> dict:
    persisted = read_json(path)
    library = default_library()
    if not persisted:
        return library
    library.update({key: value for key, value in persisted.items() if key not in {"skills", "history"}})
    persisted_skills = persisted.get("skills") if isinstance(persisted.get("skills"), dict) else {}
    skills = default_skills()
    for setup_id, default in skills.items():
        if isinstance(persisted_skills.get(setup_id), dict):
            skills[setup_id] = merge_skill(default, persisted_skills[setup_id])
    for setup_id, value in persisted_skills.items():
        if setup_id not in skills and isinstance(value, dict):
            skills[setup_id] = value
    library["skills"] = skills
    library["history"] = persisted.get("history") if isinstance(persisted.get("history"), list) else []
    return library


def save_library(library: dict, path: Path = LIBRARY_PATH, write_report: bool = True) -> dict:
    library["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(library, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if write_report:
        report_path = REPORT_PATH if path.resolve() == LIBRARY_PATH.resolve() else path.with_suffix(".md")
        report_path.write_text(render_report(library), encoding="utf-8")
    if path.resolve() == LIBRARY_PATH.resolve():
        safe_append_snapshot("setup_skill_library", "setup_skills", skill_summary(library), ts=library["updated_at"])
    return library


def append_history(library: dict, event: str, payload: dict, ts: str | None = None) -> None:
    history = list(library.get("history") or [])
    history.append({"ts": ts or utc_now(), "event": event, **payload})
    library["history"] = history[-500:]


def row_for_symbol(snapshot: dict, symbol: str) -> dict:
    target = str(symbol or "").upper()
    merged: dict = {"symbol": target}
    for key in ("hot", "funding_extremes", "top_volume", "top_gainers", "top_losers", "majors"):
        for row in snapshot.get(key, []) if isinstance(snapshot, dict) else []:
            if str(row.get("symbol") or "").upper() == target:
                merged.update(row)
    return merged


def market_tags(snapshot: dict, context: dict | None = None) -> set[str]:
    tags = set(str(tag) for tag in (context or {}).get("tags", []) if tag)
    state = (context or {}).get("market_state") if isinstance(context, dict) else None
    if isinstance(state, dict):
        tags.update(str(tag) for tag in state.get("tags", []) if tag)
        if state.get("primary_regime"):
            tags.add(str(state["primary_regime"]))
    if not tags:
        majors = snapshot.get("majors", []) if isinstance(snapshot, dict) else []
        changes = [safe_float(row.get("change_pct")) for row in majors]
        if changes and sum(changes) / len(changes) >= 1.0:
            tags.add("risk_on")
        elif changes and sum(changes) / len(changes) <= -1.0:
            tags.add("risk_off")
    return tags


def add_match(matches: list[dict], setup_id: str, score: float, reasons: list[str]) -> None:
    matches.append({"setup_id": setup_id, "confidence": round(clamp(score), 4), "reasons": reasons[:8]})


def match_setup(signal: dict, snapshot: dict, context: dict | None = None, library: dict | None = None) -> list[dict]:
    library = library or load_library()
    skills = library.get("skills") or {}
    symbol = str(signal.get("symbol") or "").upper()
    side = str(signal.get("side") or "").upper()
    if not symbol or side not in {"LONG", "SHORT"}:
        return []
    row = row_for_symbol(snapshot, symbol)
    tags = market_tags(snapshot, context)
    change = safe_float(row.get("change_pct"))
    range_pos = safe_float(row.get("range_pos"), 0.5)
    funding = safe_float(row.get("funding_pct"))
    quote_volume_m = safe_float(row.get("quote_volume")) / 1_000_000
    ctx = context or {}
    matches: list[dict] = []

    if skills.get("momentum_continuation", {}).get("enabled", True):
        long_ok = side == "LONG" and "risk_on" in tags and 0 <= change <= 18 and 0.25 <= range_pos <= 0.85
        short_ok = side == "SHORT" and "risk_off" in tags and -18 <= change <= 0 and 0.15 <= range_pos <= 0.75
        if long_ok or short_ok:
            add_match(matches, "momentum_continuation", 0.62 + min(0.18, quote_volume_m / 5000), ["regime_aligned", "not_exhausted"])

    if skills.get("exhaustion_fade", {}).get("enabled", True):
        short_fade = side == "SHORT" and (change >= 18 or range_pos >= 0.88)
        long_fade = side == "LONG" and (change <= -18 or range_pos <= 0.12)
        if short_fade or long_fade:
            add_match(matches, "exhaustion_fade", 0.58 + min(0.22, abs(change) / 250), ["range_or_24h_extreme", "fade_candidate"])

    if skills.get("funding_squeeze", {}).get("enabled", True) and abs(funding) >= 0.15:
        if (funding > 0 and side == "SHORT") or (funding < 0 and side == "LONG"):
            add_match(matches, "funding_squeeze", 0.64 + min(0.2, abs(funding) / 2), ["crowded_funding", "against_crowd"])

    if skills.get("liquidation_snapback", {}).get("enabled", True):
        if ctx.get("liquidation_burst") and ctx.get("reclaim"):
            add_match(matches, "liquidation_snapback", 0.72, ["liquidation_burst", "reclaim"])

    if skills.get("range_breakout", {}).get("enabled", True):
        breakout = ctx.get("breakout") or (side == "LONG" and 0.75 <= range_pos <= 0.92 and change >= 2) or (side == "SHORT" and 0.08 <= range_pos <= 0.25 and change <= -2)
        if breakout and quote_volume_m >= 25:
            add_match(matches, "range_breakout", 0.55 + min(0.2, quote_volume_m / 2000), ["breakout_zone", "volume_present"])

    if skills.get("false_breakout", {}).get("enabled", True):
        if ctx.get("failed_breakout") or ctx.get("liquidity_sweep"):
            add_match(matches, "false_breakout", 0.68, ["failed_breakout", "range_reentry"])

    if skills.get("news_catalyst_chase", {}).get("enabled", True):
        catalyst_score = safe_float(ctx.get("catalyst_score"))
        if catalyst_score >= 0.7 and quote_volume_m >= 25:
            add_match(matches, "news_catalyst_chase", 0.55 + min(0.3, catalyst_score / 3), ["fresh_catalyst", "liquidity_present"])

    matches.sort(key=lambda item: item["confidence"], reverse=True)
    return matches


def finalize_stats(stats: dict) -> dict:
    trades = int(stats.get("trades", 0))
    wins = int(stats.get("wins", 0))
    net = safe_float(stats.get("net"))
    stats["win_rate"] = round(wins / trades, 4) if trades else 0.0
    stats["expectancy"] = round(net / trades, 8) if trades else 0.0
    stats["net"] = round(net, 8)
    return stats


def record_setup_outcome(
    library: dict,
    setup_id: str,
    net: float,
    regime: str = "unknown",
    symbol: str | None = None,
    side: str | None = None,
    ts: str | None = None,
    evidence_id: str | None = None,
    evidence_source: str = "manual",
) -> dict:
    skills = library.setdefault("skills", {})
    if setup_id not in skills:
        raise KeyError(f"unknown setup_id: {setup_id}")
    row_ts = ts or utc_now()
    if not evidence_id:
        append_history(library, "setup_outcome_rejected", {"setup_id": setup_id, "reason": "missing_objective_evidence_id", "regime": regime, "symbol": symbol, "side": side}, row_ts)
        safe_append_event("setup_skill_library", "setup_outcome_rejected", {"setup_id": setup_id, "reason": "missing_objective_evidence_id", "regime": regime, "symbol": symbol, "side": side}, ts=row_ts)
        return skills[setup_id]
    value = safe_float(net)
    skill_row = skills[setup_id]
    stats = skill_row.setdefault("stats", empty_stats())
    stats["trades"] = int(stats.get("trades", 0)) + 1
    stats["net"] = safe_float(stats.get("net")) + value
    if value > 0:
        stats["wins"] = int(stats.get("wins", 0)) + 1
    elif value < 0:
        stats["losses"] = int(stats.get("losses", 0)) + 1
    by_regime = stats.setdefault("by_regime", {})
    bucket = by_regime.setdefault(str(regime or "unknown"), {"trades": 0, "wins": 0, "losses": 0, "net": 0.0, "win_rate": 0.0, "expectancy": 0.0})
    bucket["trades"] += 1
    bucket["net"] = safe_float(bucket.get("net")) + value
    if value > 0:
        bucket["wins"] += 1
    elif value < 0:
        bucket["losses"] += 1
    finalize_stats(bucket)
    recent = list(stats.get("recent") or [])
    recent.append({"ts": row_ts, "symbol": symbol, "side": side, "regime": regime, "net": round(value, 8), "evidence_id": evidence_id, "evidence_source": evidence_source})
    stats["recent"] = recent[-100:]
    finalize_stats(stats)
    append_history(library, "setup_outcome", {"setup_id": setup_id, "net": round(value, 8), "regime": regime, "symbol": symbol, "side": side, "evidence_id": evidence_id, "evidence_source": evidence_source}, row_ts)
    safe_append_event("setup_skill_library", "setup_outcome", {"setup_id": setup_id, "net": round(value, 8), "regime": regime, "symbol": symbol, "side": side, "evidence_id": evidence_id, "evidence_source": evidence_source}, ts=row_ts)
    return skill_row


def skill_summary(library: dict) -> dict:
    rows = []
    for setup_id, skill_row in sorted((library.get("skills") or {}).items()):
        stats = finalize_stats(dict(skill_row.get("stats") or empty_stats()))
        rows.append(
            {
                "setup_id": setup_id,
                "enabled": bool(skill_row.get("enabled", True)),
                "version": skill_row.get("version", 1),
                "trades": stats.get("trades", 0),
                "win_rate": stats.get("win_rate", 0.0),
                "expectancy": stats.get("expectancy", 0.0),
                "net": stats.get("net", 0.0),
            }
        )
    return {"updated_at": library.get("updated_at"), "skill_count": len(rows), "skills": rows}


def render_report(library: dict) -> str:
    summary = skill_summary(library)
    lines = ["# Setup Skill Library", "", f"Generated: {utc_now()}", f"Skills: {summary['skill_count']}", "", "## Skills"]
    for row in summary["skills"]:
        lines.append(
            f"- `{row['setup_id']}` enabled={row['enabled']} trades={row['trades']} "
            f"win_rate={row['win_rate']} expectancy={row['expectancy']} net={row['net']:+.6f}"
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage setup skill library")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--record-setup")
    parser.add_argument("--net", type=float, default=0.0)
    parser.add_argument("--regime", default="unknown")
    parser.add_argument("--symbol")
    parser.add_argument("--side")
    parser.add_argument("--evidence-id")
    parser.add_argument("--evidence-source", default="manual")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    library = load_library()
    changed = False
    if args.record_setup:
        record_setup_outcome(library, args.record_setup, args.net, args.regime, args.symbol, args.side, evidence_id=args.evidence_id, evidence_source=args.evidence_source)
        changed = True
    if args.init or changed:
        save_library(library)
    if args.status or not changed:
        print(json.dumps(skill_summary(library), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
