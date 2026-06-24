"""Portfolio concentration and correlation guard for paper positions."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
PORTFOLIO_RISK_LATEST = ROOT / "state" / "agent_memory" / "portfolio_risk_latest.json"

BTC_BETA_TAGS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def evaluate_portfolio_risk(positions: list[dict[str, Any]], equity: float = 100.0, output_path: Path = PORTFOLIO_RISK_LATEST) -> dict[str, Any]:
    same_side: dict[str, float] = {}
    btc_beta_notional = 0.0
    open_stop_loss = 0.0
    for pos in positions:
        side = str(pos.get("side") or "UNKNOWN").upper()
        notional = safe_float(pos.get("notional"), safe_float(pos.get("margin")) * safe_float(pos.get("leverage"), 1.0))
        same_side[side] = same_side.get(side, 0.0) + notional
        if str(pos.get("symbol") or "").upper() in BTC_BETA_TAGS or pos.get("btc_beta"):
            btc_beta_notional += notional
        open_stop_loss += abs(safe_float(pos.get("estimated_loss")))
    errors = []
    warnings = []
    if equity and btc_beta_notional / equity > 1.5:
        errors.append("btc_beta_concentration")
    if equity and open_stop_loss / equity > 0.05:
        errors.append("open_stop_loss_above_cap")
    if max(same_side.values(), default=0.0) > equity * 2:
        warnings.append("same_side_concentration")
    payload = {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "status": "critical" if errors else "warn" if warnings else "ok", "errors": errors, "warnings": warnings, "position_count": len(positions), "btc_beta_notional": round(btc_beta_notional, 6), "open_stop_loss": round(open_stop_loss, 6), "same_side_notional": same_side, "can_increase_size": not errors}
    write_json_atomic(output_path, payload)
    return payload
