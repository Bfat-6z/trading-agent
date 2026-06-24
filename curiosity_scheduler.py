"""Curiosity scheduler for the trading agent.

The scheduler picks one explicit learning focus per cycle. It is deterministic
and read-only: it does not trade and it does not loosen execution controls.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from belief_ledger import load_ledger
from event_store import query_recent_events, safe_append_event, safe_append_snapshot
from market_learner import safe_float, valid_paper_close
from setup_skill_library import load_library

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
MARKET_MODEL_PATH = MEMORY_DIR / "market_model.json"
CURIOSITY_LATEST = MEMORY_DIR / "curiosity_focus_latest.json"
CURIOSITY_HISTORY = MEMORY_DIR / "curiosity_focus_history.jsonl"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")


def focus_payload(focus_type: str, focus_id: str, score: float, reasons: list[str], **extra: object) -> dict:
    return {
        "focus_type": focus_type,
        "focus_id": focus_id,
        "score": round(score, 4),
        "reasons": reasons[:8],
        "expected_learning_value": extra.pop("expected_learning_value", "reduce uncertainty before the next paper entry"),
        **extra,
    }


def confusing_loss_candidates(events: list[dict]) -> list[dict]:
    candidates: list[dict] = []
    for row in events:
        if not valid_paper_close(row):
            continue
        net = safe_float(row.get("net"))
        if net >= 0:
            continue
        symbol = str(row.get("symbol") or row.get("position", {}).get("symbol") or "UNKNOWN").upper()
        side = str(row.get("side") or row.get("position", {}).get("side") or "UNKNOWN").upper()
        magnitude = min(20.0, abs(net) * 100)
        candidates.append(
            focus_payload(
                "confusing_loss",
                f"{symbol}:{side}",
                100.0 + magnitude,
                ["recent_paper_loss", "explain_before_next_entry"],
                symbol=symbol,
                side=side,
                net=round(net, 8),
                ts=row.get("ts"),
                expected_learning_value="identify why a real paper loss happened and whether the setup should tighten",
            )
        )
    return candidates


def setup_candidates(library: dict) -> list[dict]:
    skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
    candidates: list[dict] = []
    for setup_id, skill in skills.items():
        stats = skill.get("stats") if isinstance(skill, dict) else {}
        trades = int(safe_float((stats or {}).get("trades"), 0))
        win_rate = safe_float((stats or {}).get("win_rate"), 0)
        expectancy = safe_float((stats or {}).get("expectancy"), 0)
        net = safe_float((stats or {}).get("net"), 0)
        if trades >= 3 and (expectancy < 0 or net < 0 or win_rate <= 0.34):
            candidates.append(
                focus_payload(
                    "weakest_setup",
                    str(setup_id),
                    85.0 + min(10.0, abs(net) + abs(expectancy) * 10),
                    ["negative_setup_stats", "needs_rule_review"],
                    setup_id=setup_id,
                    stats={"trades": trades, "win_rate": win_rate, "expectancy": expectancy, "net": net},
                    expected_learning_value="decide whether this setup should be blocked, tightened, or redesigned",
                )
            )
        elif trades < 3:
            candidates.append(
                focus_payload(
                    "under_sampled_setup",
                    str(setup_id),
                    60.0 + (3 - trades) * 3,
                    ["setup_has_too_few_samples", "needs_shadow_observation"],
                    setup_id=setup_id,
                    stats={"trades": trades, "win_rate": win_rate, "expectancy": expectancy, "net": net},
                    expected_learning_value="collect more paper/shadow evidence before trusting this setup",
                )
            )
    return candidates


def regime_candidates(market_model: dict) -> list[dict]:
    counts = market_model.get("regime_counts") if isinstance(market_model.get("regime_counts"), dict) else {}
    if not counts:
        return [focus_payload("under_sampled_regime", "unknown", 45.0, ["no_regime_samples_yet"], regime="unknown")]
    regime, count = min(((str(key), int(safe_float(value, 0))) for key, value in counts.items()), key=lambda item: (item[1], item[0]))
    if count >= 5:
        return []
    return [
        focus_payload(
            "under_sampled_regime",
            regime,
            50.0 + (5 - count),
            ["regime_has_few_samples"],
            regime=regime,
            sample_count=count,
            expected_learning_value="observe how setups behave in a regime with low sample count",
        )
    ]


def contradictory_belief_candidates(ledger: dict) -> list[dict]:
    beliefs = ledger.get("beliefs") if isinstance(ledger.get("beliefs"), dict) else {}
    candidates: list[dict] = []
    for belief_id, belief in beliefs.items():
        evidence_for = belief.get("evidence_for") if isinstance(belief, dict) else []
        evidence_against = belief.get("evidence_against") if isinstance(belief, dict) else []
        for_count = len(evidence_for or [])
        against_count = len(evidence_against or [])
        confidence = safe_float((belief or {}).get("confidence"), 0.5)
        if against_count > 0 and (against_count >= for_count or confidence <= 0.4):
            candidates.append(
                focus_payload(
                    "contradictory_belief",
                    str(belief_id),
                    90.0 + against_count - for_count + max(0.0, 0.5 - confidence) * 10,
                    ["belief_has_conflicting_evidence", "needs_retest"],
                    belief_id=belief_id,
                    confidence=confidence,
                    evidence_for=for_count,
                    evidence_against=against_count,
                    expected_learning_value="decide whether to weaken, retire, or retest this belief",
                )
            )
    return candidates


def choose_focus(
    events: list[dict] | None = None,
    library: dict | None = None,
    ledger: dict | None = None,
    market_model: dict | None = None,
) -> dict:
    events = events if events is not None else query_recent_events(source="scalp_autotrader", lookback_hours=24, limit=300)
    library = library if library is not None else load_library()
    ledger = ledger if ledger is not None else load_ledger()
    market_model = market_model if market_model is not None else read_json(MARKET_MODEL_PATH)

    candidates = [
        *confusing_loss_candidates(events),
        *contradictory_belief_candidates(ledger),
        *setup_candidates(library),
        *regime_candidates(market_model),
    ]
    if not candidates:
        return focus_payload("observe_market", "fresh_context", 10.0, ["no_specific_uncertainty_found"])
    priority = {"confusing_loss": 0, "contradictory_belief": 1, "weakest_setup": 2, "under_sampled_setup": 3, "under_sampled_regime": 4}
    return sorted(candidates, key=lambda item: (-safe_float(item.get("score")), priority.get(str(item.get("focus_type")), 99), str(item.get("focus_id"))))[0]


def write_focus(focus: dict, ts: str | None = None) -> dict:
    row_ts = ts or str(focus.get("ts") or utc_now())
    row = {"ts": row_ts, **{key: value for key, value in focus.items() if key != "ts"}}
    CURIOSITY_LATEST.parent.mkdir(parents=True, exist_ok=True)
    CURIOSITY_LATEST.write_text(json.dumps(row, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_jsonl(CURIOSITY_HISTORY, row)
    safe_append_snapshot("curiosity_scheduler", "curiosity_focus", row, ts=row_ts)
    safe_append_event("curiosity_scheduler", "focus_selected", {key: value for key, value in row.items() if key != "ts"}, ts=row_ts)
    return row


def run_once(write_state: bool = True) -> dict:
    focus = choose_focus()
    if write_state:
        return write_focus(focus)
    return {"ts": utc_now(), **focus}


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pick one learning focus for the next trading-agent cycle")
    parser.add_argument("--status", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    focus = read_json(CURIOSITY_LATEST) if args.status else run_once()
    print(json.dumps(focus or {"status": "no_focus"}, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
