"""Falsifiable hypothesis engine for the trading agent.

The engine converts market state, setup skill stats, beliefs, and optional
operator/whale theses into testable hypotheses. It does not place trades.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from belief_ledger import compact_ledger, load_ledger
from event_store import safe_append_event, safe_append_snapshot
from market_learner import classify_market, safe_float
from setup_skill_library import load_library, skill_summary

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
MARKET_MODEL_PATH = MEMORY_DIR / "market_model.json"
BIAS_PATH = MEMORY_DIR / "execution_bias.json"
HYPOTHESES_LATEST = MEMORY_DIR / "hypotheses_latest.json"
HYPOTHESES_HISTORY = MEMORY_DIR / "hypotheses_history.jsonl"
MANUAL_THESES_PATH = MEMORY_DIR / "manual_theses.jsonl"
REPORT_PATH = MEMORY_DIR / "hypotheses_latest.md"

VALID_STATUSES = {"candidate", "testable", "validated", "rejected"}


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


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")


def read_jsonl_tail(path: Path, max_lines: int = 50) -> list[dict]:
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


def hypothesis_id(statement: str, setup_id: str, regime: str, symbols: list[str]) -> str:
    raw = json.dumps(
        {"statement": statement, "setup_id": setup_id, "regime": regime, "symbols": sorted(symbols)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "hyp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def make_hypothesis(
    statement: str,
    setup_id: str,
    regime: str,
    symbols: list[str],
    prediction: dict,
    invalidation: list[str],
    metrics: list[str] | None = None,
    confidence_prior: float = 0.5,
    evidence_refs: list[str] | None = None,
    source: str = "engine",
    status: str = "testable",
) -> dict:
    if status not in VALID_STATUSES:
        status = "candidate"
    clean_symbols = [str(symbol).upper() for symbol in symbols if symbol]
    return {
        "hypothesis_id": hypothesis_id(statement, setup_id, regime, clean_symbols),
        "statement": statement,
        "setup_id": setup_id,
        "regime": regime or "unknown",
        "symbols": clean_symbols,
        "prediction": prediction,
        "invalidation": invalidation,
        "metrics": metrics or ["tp_before_sl", "expectancy", "mae", "mfe", "slippage"],
        "confidence_prior": round(max(0.0, min(1.0, safe_float(confidence_prior, 0.5))), 4),
        "evidence_refs": evidence_refs or [],
        "source": source,
        "status": status,
        "created_at": utc_now(),
    }


def latest_market_state(snapshot: dict, market_model: dict) -> dict:
    state = market_model.get("last_market_state") if isinstance(market_model.get("last_market_state"), dict) else {}
    if state:
        return state
    return classify_market(snapshot)


def top_beliefs(ledger: dict, limit: int = 5) -> list[dict]:
    compact = compact_ledger(ledger)
    return compact.get("top_beliefs", [])[:limit]


def setup_expectancy(library: dict, setup_id: str) -> float:
    try:
        return safe_float(library.get("skills", {}).get(setup_id, {}).get("stats", {}).get("expectancy"))
    except Exception:
        return 0.0


def generate_from_market(snapshot: dict, market_model: dict, library: dict, ledger: dict, bias: dict) -> list[dict]:
    state = latest_market_state(snapshot, market_model)
    regime = str(state.get("primary_regime") or "unknown")
    tags = set(str(tag) for tag in state.get("tags", []) if tag)
    hot = [str(symbol).upper() for symbol in state.get("hot_symbols", [])[:8] if symbol]
    crowded = [str(symbol).upper() for symbol in state.get("crowded_symbols", [])[:8] if symbol]
    extreme_up = [str(symbol).upper() for symbol in state.get("extreme_up_symbols", [])[:8] if symbol]
    extreme_down = [str(symbol).upper() for symbol in state.get("extreme_down_symbols", [])[:8] if symbol]
    blocked = set(str(symbol).upper() for symbol in bias.get("blocked_symbols", []) if symbol)
    evidence_refs = [belief.get("belief_id") for belief in top_beliefs(ledger) if belief.get("belief_id")]
    hypotheses: list[dict] = []

    if regime == "risk_on" and hot:
        symbols = [symbol for symbol in hot if symbol not in blocked][:5] or hot[:5]
        hypotheses.append(
            make_hypothesis(
                "In risk_on regime, non-exhausted high-liquidity pullbacks should outperform blind fades.",
                "momentum_continuation",
                regime,
                symbols,
                {"side": "LONG", "condition": "pullback_reclaim", "expected": "tp_before_sl_above_baseline"},
                ["range_pos_above_0_9", "funding_extreme_against_long", "major_regime_flips_risk_off", "data_stale"],
                confidence_prior=0.56 + max(0.0, setup_expectancy(library, "momentum_continuation")),
                evidence_refs=evidence_refs,
            )
        )

    if "crowded_funding" in tags and crowded:
        hypotheses.append(
            make_hypothesis(
                "Crowded funding symbols should not be chased; squeeze/fade setups need confirmation against the crowd.",
                "funding_squeeze",
                regime,
                crowded[:6],
                {"side": "AGAINST_CROWD", "condition": "price_confirms_trap", "expected": "lower_mae_than_chase"},
                ["oi_expands_with_trend", "taker_flow_supports_crowd", "no_reclaim_or_break", "news_catalyst_overrides"],
                confidence_prior=0.62 + max(0.0, setup_expectancy(library, "funding_squeeze")),
                evidence_refs=evidence_refs,
            )
        )

    if "alt_mania" in tags and extreme_up:
        hypotheses.append(
            make_hypothesis(
                "Alt mania extremes have worse late-long expectancy unless they reset through a controlled pullback.",
                "exhaustion_fade",
                regime,
                extreme_up[:6],
                {"side": "SHORT_OR_NO_CHASE", "condition": "failed_continuation", "expected": "late_longs_underperform"},
                ["fresh_breakout_with_volume", "funding_normalizes", "major_continuation_accelerates", "spread_unstable"],
                confidence_prior=0.64 + max(0.0, setup_expectancy(library, "exhaustion_fade")),
                evidence_refs=evidence_refs,
            )
        )

    if extreme_down:
        hypotheses.append(
            make_hypothesis(
                "Large unwind symbols should be observed for liquidation snapback only after reclaim, not bought during freefall.",
                "liquidation_snapback",
                regime,
                extreme_down[:6],
                {"side": "LONG", "condition": "post_unwind_reclaim", "expected": "snapback_quality_positive"},
                ["lower_low_without_reclaim", "liquidation_burst_continues", "majors_turn_risk_off", "thin_liquidity"],
                confidence_prior=0.52 + max(0.0, setup_expectancy(library, "liquidation_snapback")),
                evidence_refs=evidence_refs,
            )
        )

    return hypotheses


def manual_thesis_to_hypothesis(row: dict) -> dict | None:
    symbol = str(row.get("symbol") or "").upper()
    side = str(row.get("side") or "").upper()
    if not symbol or side not in {"LONG", "SHORT"}:
        return None
    entry = safe_float(row.get("entry"))
    stop = safe_float(row.get("stop"))
    targets = [safe_float(target) for target in row.get("targets", []) if safe_float(target) > 0]
    if entry <= 0 or stop <= 0 or not targets:
        return None
    risk_pct = abs(entry - stop) / entry * 100
    reward_pct = abs(targets[-1] - entry) / entry * 100
    rr = reward_pct / risk_pct if risk_pct else 0
    return make_hypothesis(
        f"Manual thesis on {symbol} {side}: entry {entry:g}, stop {stop:g}, targets {', '.join(f'{target:g}' for target in targets)}.",
        str(row.get("setup_id") or "manual_chart_thesis"),
        str(row.get("regime") or "manual"),
        [symbol],
        {"side": side, "entry": entry, "stop": stop, "targets": targets, "risk_pct": round(risk_pct, 4), "reward_pct": round(reward_pct, 4), "rr_to_final": round(rr, 4)},
        ["price_closes_beyond_stop", "market_context_invalidates", "data_stale", "risk_reward_degrades_before_entry"],
        metrics=["tp_before_sl", "rr_realized", "mae", "mfe", "time_to_target"],
        confidence_prior=safe_float(row.get("confidence_prior"), 0.5),
        evidence_refs=[str(row.get("source") or "manual")],
        source="manual_thesis",
        status="testable",
    )


def generate_hypotheses(
    snapshot: dict,
    market_model: dict,
    library: dict,
    ledger: dict,
    bias: dict,
    manual_theses: list[dict] | None = None,
    limit: int = 12,
) -> dict:
    hypotheses = generate_from_market(snapshot, market_model, library, ledger, bias)
    for row in manual_theses or []:
        hyp = manual_thesis_to_hypothesis(row)
        if hyp:
            hypotheses.append(hyp)

    deduped: dict[str, dict] = {}
    for hypothesis in hypotheses:
        deduped[hypothesis["hypothesis_id"]] = hypothesis
    ranked = sorted(deduped.values(), key=lambda item: (safe_float(item.get("confidence_prior")), item.get("created_at", "")), reverse=True)[:limit]
    return {
        "ts": utc_now(),
        "market_state": latest_market_state(snapshot, market_model),
        "setup_summary": skill_summary(library),
        "hypotheses": ranked,
    }


def render_report(result: dict) -> str:
    state = result.get("market_state") or {}
    lines = [
        "# Hypotheses",
        "",
        f"Generated: {result.get('ts')}",
        f"Regime: `{state.get('primary_regime', 'unknown')}` tags={', '.join(state.get('tags') or [])}",
        "",
        "## Testable Hypotheses",
    ]
    for item in result.get("hypotheses", []):
        lines.append(f"- `{item['hypothesis_id']}` {item['setup_id']} prior={item['confidence_prior']}: {item['statement']}")
        lines.append(f"  - Prediction: {json.dumps(item.get('prediction', {}), ensure_ascii=True, sort_keys=True)}")
        lines.append(f"  - Invalidation: {', '.join(item.get('invalidation') or [])}")
    if not result.get("hypotheses"):
        lines.append("- No testable hypothesis. Keep observing.")
    return "\n".join(lines) + "\n"


def save_result(result: dict, latest_path: Path = HYPOTHESES_LATEST) -> None:
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    REPORT_PATH.write_text(render_report(result), encoding="utf-8")
    append_jsonl(HYPOTHESES_HISTORY, {"ts": result.get("ts"), "hypotheses": result.get("hypotheses", [])})
    safe_append_snapshot("hypothesis_engine", "hypotheses", result, ts=result.get("ts"))
    safe_append_event("hypothesis_engine", "hypotheses_update", {"count": len(result.get("hypotheses", []))}, ts=result.get("ts"))


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate testable trading hypotheses")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--add-manual-thesis", action="store_true")
    parser.add_argument("--symbol")
    parser.add_argument("--side")
    parser.add_argument("--entry", type=float)
    parser.add_argument("--stop", type=float)
    parser.add_argument("--targets", nargs="*", type=float, default=[])
    parser.add_argument("--source", default="manual")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.add_manual_thesis:
        if not args.symbol or not args.side or not args.entry or not args.stop or not args.targets:
            raise SystemExit("manual thesis requires --symbol --side --entry --stop --targets")
        append_jsonl(
            MANUAL_THESES_PATH,
            {"ts": utc_now(), "symbol": args.symbol, "side": args.side, "entry": args.entry, "stop": args.stop, "targets": args.targets, "source": args.source},
        )
    if args.status and HYPOTHESES_LATEST.exists():
        print(HYPOTHESES_LATEST.read_text(encoding="utf-8"))
        return 0
    result = generate_hypotheses(
        read_json(MARKET_LATEST),
        read_json(MARKET_MODEL_PATH),
        load_library(),
        load_ledger(),
        read_json(BIAS_PATH),
        read_jsonl_tail(MANUAL_THESES_PATH, 20),
        limit=args.limit,
    )
    save_result(result)
    print(json.dumps({"count": len(result.get("hypotheses", [])), "latest": str(HYPOTHESES_LATEST)}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
