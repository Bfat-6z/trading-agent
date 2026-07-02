"""Adapter + compact summary for the owner's (previously-orphaned) SMC chart
detectors: chart_pivot_detector / chart_zone_detector / chart_structure_detector.
They are no-lookahead-rigorous (confirmed pivots gated by decision_cutoff) and
materially richer than the hand-coded FVG/swings. This wires them into the live
LLM trader.

PAPER-ONLY: read-only compute, no live path.
"""
from __future__ import annotations

from typing import Any

# timeframes the chart contract accepts
_TF_OK = {"1D", "4h", "1h", "15m", "5m", "1m"}


def to_candle_batch(bars: list[dict[str, Any]], symbol: str, timeframe: str) -> dict[str, Any]:
    """orderflow_data.fetch_klines_with_flow bars -> valid ChartCandleBatch.v1.

    Invariants (the two silent 0-pivot traps): every bar's four finality stamps =
    its close_time (an ISO-8601 string, NEVER a raw ms int), and decision_cutoff =
    the last close_time (>= every bar's known_at, else all bars are filtered).
    """
    ob: list[dict[str, Any]] = []
    last = None
    for b in bars:
        k = str(b["close_time"])
        last = k
        ob.append({
            "open_time": str(b["open_time"]), "close_time": k,
            "open": float(b["open"]), "high": float(b["high"]),
            "low": float(b["low"]), "close": float(b["close"]),
            "volume": float(b.get("volume") or 0.0), "is_final": True,
            "known_at": k, "available_at": k, "ingested_at": k, "finalized_at": k,
        })
    return {
        "schema_version": 1, "chart_model_version": "chart_intelligence_v1",
        "contract": "ChartCandleBatch.v1", "symbol": symbol.upper(), "timeframe": timeframe,
        "closed_only": True, "price_basis": "last", "native_timeframe": True,
        "source_ids": ["orderflow_data.fetch_klines_with_flow"],
        "batch_id": f"cb:{symbol.upper()}:{timeframe}:{last}",
        "input_event_ids": [f"klines:{symbol.upper()}:{timeframe}:{last}"],
        "decision_cutoff": last, "cutoff_proof": {"ok": True, "errors": []},
        "degradation_state": "ok", "bars": ob,
    }


def _num(x, d=None):
    try:
        return float(x)
    except Exception:
        return d


def smc_summary(bars: list[dict[str, Any]], symbol: str, timeframe: str = "15m") -> dict[str, Any]:
    """Run the SMC detectors; return {"summary": {...}, "hlines": [...]} — a compact
    market-structure read for the LLM prompt + nearest S/R edges for the chart.
    Best-effort: returns {} on any failure so it never breaks the trading loop."""
    try:
        if not bars or len(bars) < 30 or timeframe not in _TF_OK:
            return {}
        import chart_pivot_detector as cp
        import chart_zone_detector as cz
        import chart_structure_detector as csd
        batch = to_candle_batch(bars, symbol, timeframe)
        piv = cp.compute_pivot_bundle(batch)
        if piv.get("degradation_state") == "quarantined":
            return {}
        zon = cz.compute_zone_bundle(piv, candle_batch=batch)
        strc = csd.compute_market_structure_bundle(piv, candle_batch=batch)
        zd = zon.get("structures") or {}
        sd = strc.get("structures") or {}
        pd = piv.get("structures") or {}
        cur = float(bars[-1]["close"])

        zones = zd.get("zones") or []
        sup = [z for z in zones if _num(z.get("upper"), 0) <= cur]
        res = [z for z in zones if _num(z.get("lower"), 1e18) >= cur]
        n_sup = max(sup, key=lambda z: _num(z.get("upper"), 0), default=None)
        n_res = min(res, key=lambda z: _num(z.get("lower"), 1e18), default=None)

        def zfmt(z):
            if not z:
                return None
            return {"lo": round(_num(z.get("lower"), 0), 4), "hi": round(_num(z.get("upper"), 0), 4),
                    "strength": round(_num(z.get("strength"), 0), 2), "quality": z.get("quality"),
                    "touches": z.get("touch_count"), "rel": z.get("price_relation")}

        # CORRECT keys (audit fix): the structure bundle emits `structure_events`
        # (each with `event_type` e.g. CHOCH_UP/BOS_UP), and swing labels live on
        # each pivot row under `structure_label` — not events/kind/swing_labels.
        events = sd.get("structure_events") or []
        summary = {
            "trend": sd.get("trend_state"), "bias": sd.get("side_bias"),
            "confidence": sd.get("confidence"),
            "invalidation": sd.get("invalidation_level"),
            "last_structure_event": events[-1].get("event_type") if events else None,
            "swing_labels": [p.get("structure_label") for p in (sd.get("pivots") or [])
                             if p.get("structure_label")][-6:],
            "nearest_support": zfmt(n_sup), "nearest_resistance": zfmt(n_res),
            "n_zones": len(zones), "n_pivots": pd.get("pivot_count"),
        }
        hlines = []
        if n_sup:
            hlines.append((_num(n_sup.get("upper"), 0), "SUP", "#26a69a"))
        if n_res:
            hlines.append((_num(n_res.get("lower"), 0), "RES", "#ef5350"))
        return {"summary": summary, "hlines": [h for h in hlines if h[0]]}
    except Exception:
        return {}
