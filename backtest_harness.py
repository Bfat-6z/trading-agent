"""Tiny deterministic backtest harness for baselines and paper signals."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
EVALUATION_DIR = ROOT / "state" / "evaluation_runs"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def assert_no_lookahead(signal: dict[str, Any], candles: list[dict[str, Any]]) -> None:
    index = int(signal.get("index") or 0)
    if index <= 0 or index >= len(candles):
        raise ValueError("invalid_signal_index")
    feature_ts = signal.get("feature_ts")
    entry_ts = candles[index].get("ts")
    if feature_ts and parse_utc(feature_ts) and parse_utc(entry_ts) and parse_utc(feature_ts) > parse_utc(entry_ts):
        raise ValueError("lookahead_feature_ts")


def pnl_for_signal(signal: dict[str, Any], candles: list[dict[str, Any]], fee_rate: float = 0.0005, slippage_bps: float = 2.0) -> float:
    assert_no_lookahead(signal, candles)
    index = int(signal["index"])
    entry = safe_float(candles[index]["open"])
    exit_price = safe_float(candles[min(index + 1, len(candles) - 1)]["close"])
    slip = slippage_bps / 10000
    if signal.get("side") == "LONG":
        gross = (exit_price * (1 - slip) - entry * (1 + slip)) / entry if entry else 0.0
    else:
        gross = (entry * (1 - slip) - exit_price * (1 + slip)) / entry if entry else 0.0
    return gross - fee_rate * 2

def run_decision_time_backtest(
    name: str,
    candles: list[dict[str, Any]],
    strategy: Callable[[list[dict[str, Any]]], dict[str, Any] | None],
    assumptions: dict[str, Any] | None = None,
    output_dir: Path = EVALUATION_DIR,
) -> dict[str, Any]:
    assumptions = {"fee_rate": 0.0005, "slippage_bps": 2.0, **(assumptions or {})}
    signals: list[dict[str, Any]] = []
    for index in range(1, max(1, len(candles) - 1)):
        visible = candles[: index + 1]
        signal = strategy(visible)
        if not signal:
            continue
        signal_index = int(signal.get("index") or index + 1)
        if signal_index != index + 1:
            raise ValueError("strategy_must_enter_next_candle")
        feature_ts = signal.get("feature_ts") or visible[-1].get("ts")
        if parse_utc(feature_ts) and parse_utc(candles[index].get("ts")) and parse_utc(feature_ts) > parse_utc(candles[index].get("ts")):
            raise ValueError("lookahead_feature_ts")
        signals.append({**signal, "index": signal_index, "feature_ts": feature_ts, "decision_ts": visible[-1].get("ts")})
    pnls = [pnl_for_signal(sig, candles, assumptions["fee_rate"], assumptions["slippage_bps"]) for sig in signals]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"eval_{name}_{utc_now().replace(':', '').replace('+', 'Z')}",
        "name": name,
        "created_at": utc_now(),
        "assumptions": assumptions,
        "decision_time_safe": True,
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else 0.0,
        "expectancy_after_fees": round(sum(pnls) / len(pnls), 8) if pnls else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else (999.0 if wins else 0.0),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output_dir / f"{result['run_id']}.json", result)
    return result


def run_backtest(name: str, candles: list[dict[str, Any]], strategy: Callable[[list[dict[str, Any]]], list[dict[str, Any]]], assumptions: dict[str, Any] | None = None, output_dir: Path = EVALUATION_DIR) -> dict[str, Any]:
    assumptions = {"fee_rate": 0.0005, "slippage_bps": 2.0, **(assumptions or {})}
    signals = strategy(candles)
    pnls = [pnl_for_signal(sig, candles, assumptions["fee_rate"], assumptions["slippage_bps"]) for sig in signals]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"eval_{name}_{utc_now().replace(':', '').replace('+', 'Z')}",
        "name": name,
        "created_at": utc_now(),
        "assumptions": assumptions,
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else 0.0,
        "expectancy_after_fees": round(sum(pnls) / len(pnls), 8) if pnls else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else (999.0 if wins else 0.0),
        "confidence_interval_note": "point estimate only; require larger sample for statistical confidence" if len(pnls) < 30 else "sample >= 30; still validate out of sample",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output_dir / f"{result['run_id']}.json", result)
    return result
