"""Market learning layer for the trading agent.

This module converts raw market snapshots and paper trade outcomes into a
small persistent model. It is deliberately deterministic: the model records
what market regime was observed, which symbols/sides have recently failed,
and what execution bias should be more conservative on the next cycle.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from event_store import safe_append_event, safe_append_snapshot

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
SCALP_LOG = STATE_DIR / "scalp_autotrader.jsonl"
MARKET_MODEL_PATH = MEMORY_DIR / "market_model.json"
MARKET_LEARNING_MD = MEMORY_DIR / "market_learning_latest.md"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default

def valid_paper_open(row: dict) -> bool:
    if row.get("event") != "paper_open":
        return False
    position = row.get("position") if isinstance(row.get("position"), dict) else {}
    return safe_float(position.get("qty")) > 0

def valid_paper_close(row: dict) -> bool:
    if row.get("event") != "paper_close":
        return False
    position = row.get("position") if isinstance(row.get("position"), dict) else {}
    if safe_float(row.get("qty")) > 0 or safe_float(position.get("qty")) > 0:
        return True
    return any(abs(safe_float(row.get(field))) > 0 for field in ("gross", "fees", "net"))


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


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


def symbol(row: dict) -> str | None:
    value = row.get("symbol")
    if value:
        return str(value).upper()
    return None


def unique_symbols(rows: Iterable[dict], limit: int = 20) -> list[str]:
    result: list[str] = []
    for row in rows:
        value = symbol(row)
        if value and value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


def classify_market(snapshot: dict) -> dict:
    majors = snapshot.get("majors", []) if isinstance(snapshot, dict) else []
    hot = snapshot.get("hot", []) if isinstance(snapshot, dict) else []
    funding_extremes = snapshot.get("funding_extremes", []) if isinstance(snapshot, dict) else []

    major_changes = [safe_float(row.get("change_pct")) for row in majors]
    major_avg = sum(major_changes) / len(major_changes) if major_changes else 0.0
    major_positive = sum(1 for value in major_changes if value > 0)
    major_range_avg = sum(safe_float(row.get("range_pos"), 0.5) for row in majors) / len(majors) if majors else 0.5

    extreme_up = [row for row in hot if safe_float(row.get("change_pct")) >= 25 or safe_float(row.get("range_pos"), 0.5) >= 0.93]
    extreme_down = [row for row in hot if safe_float(row.get("change_pct")) <= -20 or safe_float(row.get("range_pos"), 0.5) <= 0.08]
    crowded = [
        row
        for row in funding_extremes
        if abs(safe_float(row.get("funding_pct"))) >= 0.15
        or abs(safe_float(row.get("change_pct"))) >= 30
    ]

    if major_avg >= 1.0 and major_positive >= 3:
        primary_regime = "risk_on"
    elif major_avg <= -1.0 and major_positive <= 1:
        primary_regime = "risk_off"
    else:
        primary_regime = "mixed"

    alt_mania = len(extreme_up) >= 3 and any(safe_float(row.get("change_pct")) >= 50 for row in hot[:5])
    liquidation_unwind = len(extreme_down) >= 3
    crowded_funding = len(crowded) >= 3
    chase_risk = alt_mania or liquidation_unwind or crowded_funding

    tags = [primary_regime]
    if alt_mania:
        tags.append("alt_mania")
    if liquidation_unwind:
        tags.append("liquidation_unwind")
    if crowded_funding:
        tags.append("crowded_funding")
    if major_range_avg >= 0.82:
        tags.append("majors_near_highs")

    blocked_symbols = unique_symbols([*extreme_up, *extreme_down, *crowded], limit=16)
    blocked_sides: list[str] = []
    if primary_regime == "risk_off" or liquidation_unwind:
        blocked_sides.append("LONG")

    min_signal_score = 6
    if chase_risk:
        min_signal_score = 7
    if alt_mania and crowded_funding:
        min_signal_score = 8

    notes: list[str] = []
    if primary_regime == "risk_on":
        notes.append("Majors are aligned up; longs can work only after pullback confirmation, not on exhausted highs.")
    elif primary_regime == "risk_off":
        notes.append("Majors are weak; alt longs require stronger confirmation or should be blocked.")
    else:
        notes.append("Majors are mixed; avoid assuming broad market support for single-symbol momentum.")
    if alt_mania:
        notes.append("Several alts are extended; chase entries need score >= 8 or should be skipped.")
    if crowded_funding:
        notes.append("Funding is crowded on multiple symbols; expect squeezes and faster reversals.")
    if liquidation_unwind:
        notes.append("Multiple hot symbols are breaking down; avoid late longs during unwind.")

    return {
        "ts": snapshot.get("ts") if isinstance(snapshot, dict) else None,
        "primary_regime": primary_regime,
        "tags": tags,
        "major_avg_24h_pct": round(major_avg, 4),
        "major_positive_count": major_positive,
        "major_range_avg": round(major_range_avg, 4),
        "hot_symbols": unique_symbols(hot, limit=12),
        "extreme_up_symbols": unique_symbols(extreme_up, limit=12),
        "extreme_down_symbols": unique_symbols(extreme_down, limit=12),
        "crowded_symbols": unique_symbols(crowded, limit=12),
        "blocked_symbols": blocked_symbols,
        "blocked_sides": blocked_sides,
        "recommended_min_signal_score": min_signal_score,
        "chase_risk": chase_risk,
        "notes": notes,
    }


def summarize_trade_outcomes(events: list[dict]) -> dict:
    by_symbol: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "net": 0.0})
    by_side: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "net": 0.0})
    by_pair: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "net": 0.0})
    for row in events:
        if not valid_paper_close(row):
            continue
        row_symbol = str(row.get("symbol") or row.get("position", {}).get("symbol") or "UNKNOWN").upper()
        row_side = str(row.get("side") or row.get("position", {}).get("side") or "UNKNOWN").upper()
        net = safe_float(row.get("net"))
        for bucket, key in ((by_symbol, row_symbol), (by_side, row_side), (by_pair, f"{row_symbol}:{row_side}")):
            item = bucket[key]
            item["trades"] += 1
            item["net"] += net
            if net > 0:
                item["wins"] += 1
            elif net < 0:
                item["losses"] += 1

    def finalize(bucket: dict[str, dict]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for key, item in bucket.items():
            trades = max(1, int(item["trades"]))
            result[key] = {
                "trades": item["trades"],
                "wins": item["wins"],
                "losses": item["losses"],
                "net": round(item["net"], 8),
                "win_rate": round(item["wins"] / trades, 4),
            }
        return result

    return {"by_symbol": finalize(by_symbol), "by_side": finalize(by_side), "by_pair": finalize(by_pair)}


def derive_learning_rules(market_state: dict, outcomes: dict) -> dict:
    blocked_symbols = list(market_state.get("blocked_symbols") or [])
    blocked_sides = list(market_state.get("blocked_sides") or [])
    rules = list(market_state.get("notes") or [])

    for key, item in outcomes.get("by_pair", {}).items():
        if item.get("trades", 0) >= 2 and item.get("net", 0.0) < 0 and item.get("losses", 0) >= 2:
            pair_symbol, _, pair_side = key.partition(":")
            if pair_symbol and pair_symbol != "UNKNOWN" and pair_symbol not in blocked_symbols:
                blocked_symbols.append(pair_symbol)
            rules.append(f"Recent paper edge is negative on {key}; block the symbol until a new reflection clears it.")
            if pair_side in {"LONG", "SHORT"} and item.get("losses", 0) >= 3 and pair_side not in blocked_sides:
                blocked_sides.append(pair_side)

    for side, item in outcomes.get("by_side", {}).items():
        if side in {"LONG", "SHORT"} and item.get("trades", 0) >= 4 and item.get("net", 0.0) < 0 and item.get("win_rate", 1.0) <= 0.25:
            if side not in blocked_sides:
                blocked_sides.append(side)
            rules.append(f"Recent {side} paper trades have poor expectancy; block this side until more evidence improves.")

    min_signal_score = int(market_state.get("recommended_min_signal_score") or 6)
    if any(item.get("trades", 0) >= 2 and item.get("net", 0.0) < 0 for item in outcomes.get("by_symbol", {}).values()):
        min_signal_score = max(min_signal_score, 7)

    return {
        "blocked_symbols": blocked_symbols[:16],
        "blocked_sides": blocked_sides[:2],
        "min_signal_score": min_signal_score,
        "rules": rules[:12],
    }


def default_model() -> dict:
    return {
        "created_at": utc_now(),
        "updated_at": None,
        "cycles": 0,
        "regime_counts": {},
        "symbol_outcomes": {},
        "side_outcomes": {},
        "last_market_state": {},
        "last_trade_outcomes": {},
        "last_rules": {},
        "history": [],
    }


def merge_outcomes(existing: dict, latest: dict) -> dict:
    merged = {key: dict(value) for key, value in existing.items() if isinstance(value, dict)}
    for key, item in latest.items():
        target = merged.setdefault(key, {"trades": 0, "wins": 0, "losses": 0, "net": 0.0})
        target["trades"] = int(target.get("trades", 0)) + int(item.get("trades", 0))
        target["wins"] = int(target.get("wins", 0)) + int(item.get("wins", 0))
        target["losses"] = int(target.get("losses", 0)) + int(item.get("losses", 0))
        target["net"] = round(safe_float(target.get("net")) + safe_float(item.get("net")), 8)
        trades = max(1, int(target["trades"]))
        target["win_rate"] = round(int(target["wins"]) / trades, 4)
    return dict(sorted(merged.items())[-300:])


def render_report(model: dict) -> str:
    state = model.get("last_market_state") or {}
    rules = model.get("last_rules") or {}
    outcomes = model.get("last_trade_outcomes") or {}
    lines = [
        "# Market Learning",
        "",
        f"Generated: {utc_now()}",
        f"Cycle: {model.get('cycles')}",
        f"Regime: `{state.get('primary_regime', 'unknown')}` tags={', '.join(state.get('tags') or [])}",
        f"Majors avg 24h: {safe_float(state.get('major_avg_24h_pct')):+.2f}% positive={state.get('major_positive_count')}",
        "",
        "## Execution Bias From Learning",
        f"- Min signal score: {rules.get('min_signal_score')}",
        f"- Blocked symbols: {', '.join(rules.get('blocked_symbols') or []) or 'none'}",
        f"- Blocked sides: {', '.join(rules.get('blocked_sides') or []) or 'none'}",
        "",
        "## Rules",
    ]
    lines.extend(f"- {rule}" for rule in (rules.get("rules") or ["Collect more samples before changing behavior."]))
    lines.extend(["", "## Recent Paper Outcomes"])
    by_pair = outcomes.get("by_pair") or {}
    if by_pair:
        for key, item in sorted(by_pair.items(), key=lambda pair: (pair[1].get("net", 0), pair[0]))[:10]:
            lines.append(f"- {key}: trades={item.get('trades')} win_rate={item.get('win_rate')} net={safe_float(item.get('net')):+.6f}")
    else:
        lines.append("- No closed paper trades in the learning window.")
    return "\n".join(lines) + "\n"


def update_market_model(
    snapshot: dict,
    trade_events: list[dict],
    model_path: Path = MARKET_MODEL_PATH,
    report_path: Path = MARKET_LEARNING_MD,
) -> dict:
    model = read_json(model_path) or default_model()
    market_state = classify_market(snapshot)
    outcomes = summarize_trade_outcomes(trade_events)
    rules = derive_learning_rules(market_state, outcomes)

    model["cycles"] = int(model.get("cycles", 0)) + 1
    model["updated_at"] = utc_now()
    regime_counts = dict(model.get("regime_counts") or {})
    regime = str(market_state.get("primary_regime") or "unknown")
    regime_counts[regime] = int(regime_counts.get(regime, 0)) + 1
    model["regime_counts"] = regime_counts
    model["symbol_outcomes"] = merge_outcomes(model.get("symbol_outcomes") or {}, outcomes.get("by_symbol") or {})
    model["side_outcomes"] = merge_outcomes(model.get("side_outcomes") or {}, outcomes.get("by_side") or {})
    model["last_market_state"] = market_state
    model["last_trade_outcomes"] = outcomes
    model["last_rules"] = rules
    history = list(model.get("history") or [])
    history.append({"ts": model["updated_at"], "regime": regime, "tags": market_state.get("tags"), "rules": rules})
    model["history"] = history[-200:]

    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(json.dumps(model, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(render_report(model), encoding="utf-8")
    if model_path.resolve() == MARKET_MODEL_PATH.resolve():
        safe_append_snapshot("market_learner", "market_model", model, ts=model["updated_at"])
        safe_append_event("market_learner", "learning_update", {"regime": regime, "rules": rules}, ts=model["updated_at"])
    return model


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update market learning model from latest snapshot and paper logs")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--trade-events", type=int, default=500)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        model = read_json(MARKET_MODEL_PATH)
        print(json.dumps(model or {"status": "no_model", "path": str(MARKET_MODEL_PATH)}, ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    model = update_market_model(read_json(MARKET_LATEST), read_jsonl_tail(SCALP_LOG, args.trade_events))
    print(json.dumps(model.get("last_rules", {}), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
