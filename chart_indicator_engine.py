"""Deterministic chart indicator engine.

Consumes `ChartCandleBatch.v1` payloads and emits `ChartIndicatorBundle.v1`.
No TradingView-only values are trusted as decision evidence here.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd

from agent_data_contracts import CHART_MODEL_VERSION, SCHEMA_VERSION, validate_chart_contract
from atomic_state import append_jsonl, canonical_json, write_json_atomic
from timebase import parse_utc, utc_now

try:
    from tradingagents.dataflows import crypto_indicators as ci
except Exception:  # pragma: no cover - fallback for local source layout
    from tradingagents_crypto_src.tradingagents.dataflows import crypto_indicators as ci

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
INDICATOR_DIR = STATE_DIR / "chart" / "indicators"

SMA_PERIODS = (9, 20, 50, 100, 200)
EMA_PERIODS = (9, 20, 50, 100, 200)
FULL_WARMUP_CANDLES = 200


def stable_digest(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]


def safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        result = float(value)
        if pd.isna(result):
            return None
        return result
    except Exception:
        return None


def rounded(value: Any, digits: int = 10) -> float | None:
    number = safe_float(value)
    if number is None:
        return None
    return round(number, digits)


def last_value(series: pd.Series) -> float | None:
    if series.empty:
        return None
    value = series.iloc[-1]
    return rounded(value)


def candles_to_dataframe(candle_batch: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows = []
    missing_volume = 0
    for bar in candle_batch.get("bars") or []:
        dt = parse_utc(bar.get("close_time") or bar.get("open_time"))
        if not dt:
            continue
        volume = safe_float(bar.get("volume"))
        if volume is None:
            missing_volume += 1
            volume = 0.0
        rows.append(
            {
                "dt": dt,
                "open": safe_float(bar.get("open")),
                "high": safe_float(bar.get("high")),
                "low": safe_float(bar.get("low")),
                "close": safe_float(bar.get("close")),
                "volume": volume,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["open", "high", "low", "close"]).sort_values("dt").reset_index(drop=True)
    return df, {"missing_volume_count": missing_volume}


def rsi14(close: pd.Series) -> pd.Series:
    delta = close.diff()
    gains = delta.where(delta > 0, 0)
    losses = (-delta).where(delta < 0, 0)
    flat = (gains.rolling(14, min_periods=14).sum() == 0) & (losses.rolling(14, min_periods=14).sum() == 0)
    result = ci.rsi(close, 14)
    result = result.mask(flat, 50.0)
    return result


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period, min_periods=period).mean()
    plus_di = 100 * plus_dm.rolling(period, min_periods=period).mean() / atr.replace(0, float("nan"))
    minus_di = 100 * minus_dm.rolling(period, min_periods=period).mean() / atr.replace(0, float("nan"))
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))) * 100
    result = dx.rolling(period, min_periods=period).mean()
    return result.fillna(0.0)


def session_vwap(df: pd.DataFrame, session_timezone: str = "UTC") -> dict[str, Any]:
    if df.empty or "volume" not in df:
        return {"value": None, "status": "missing_volume", "session_timezone": session_timezone, "session_start_utc": None}
    last_dt = df["dt"].iloc[-1]
    session_start = last_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    session_rows = df[df["dt"] >= session_start]
    volume_sum = float(session_rows["volume"].sum()) if not session_rows.empty else 0.0
    if volume_sum <= 0:
        return {"value": None, "status": "missing_volume", "session_timezone": session_timezone, "session_start_utc": session_start.isoformat(timespec="seconds")}
    typical = (session_rows["high"] + session_rows["low"] + session_rows["close"]) / 3
    value = float((typical * session_rows["volume"]).sum() / volume_sum)
    return {"value": rounded(value), "status": "ok", "session_timezone": session_timezone, "session_start_utc": session_start.isoformat(timespec="seconds")}


def volume_ratio(df: pd.DataFrame, missing_volume_count: int, period: int = 20) -> dict[str, Any]:
    if df.empty or missing_volume_count >= len(df) or float(df["volume"].sum()) <= 0:
        return {"value": None, "status": "missing_volume", "period": period}
    lookback = df["volume"].iloc[-period - 1 : -1] if len(df) > 1 else pd.Series(dtype=float)
    baseline = float(lookback.mean()) if not lookback.empty else 0.0
    last = float(df["volume"].iloc[-1])
    if baseline <= 0:
        return {"value": None, "status": "insufficient_volume_history", "period": period, "last": rounded(last)}
    return {"value": rounded(last / baseline, 6), "status": "ok", "period": period, "last": rounded(last), "baseline": rounded(baseline)}


def series_tail(series: pd.Series, limit: int) -> list[float | None]:
    return [rounded(value) for value in series.tail(max(0, int(limit))).tolist()]


def compute_indicator_bundle(candle_batch: dict[str, Any], *, series_limit: int = 120, session_timezone: str = "UTC") -> dict[str, Any]:
    df, meta = candles_to_dataframe(candle_batch)
    errors: list[str] = []
    warnings: list[str] = []
    if df.empty:
        errors.append("no_valid_candles")
    candle_count = len(df)
    close = df["close"] if not df.empty else pd.Series(dtype=float)
    sma = {str(period): last_value(close.rolling(period, min_periods=period).mean()) for period in SMA_PERIODS}
    ema = {str(period): last_value(ci.ema(close, period)) if not close.empty else None for period in EMA_PERIODS}
    rsi_series = rsi14(close) if not close.empty else pd.Series(dtype=float)
    macd_values = ci.macd(close) if not close.empty else {"macd": pd.Series(dtype=float), "signal": pd.Series(dtype=float), "hist": pd.Series(dtype=float)}
    bb = ci.bollinger(close) if not close.empty else {"upper": pd.Series(dtype=float), "mid": pd.Series(dtype=float), "lower": pd.Series(dtype=float)}
    atr_series = ci.atr(df, 14) if not df.empty else pd.Series(dtype=float)
    adx_series = adx(df, 14) if not df.empty else pd.Series(dtype=float)
    warmup_complete = candle_count >= FULL_WARMUP_CANDLES
    if not warmup_complete:
        warnings.append("warmup_incomplete")
    vwap = session_vwap(df, session_timezone=session_timezone)
    vol_ratio = volume_ratio(df, int(meta["missing_volume_count"]))
    if vwap["status"] != "ok":
        warnings.append("vwap_disabled:" + vwap["status"])
    if vol_ratio["status"] != "ok":
        warnings.append("volume_ratio_disabled:" + vol_ratio["status"])
    indicators = {
        "sma": sma,
        "ema": ema,
        "rsi14": last_value(rsi_series),
        "macd": {
            "line": last_value(macd_values["macd"]),
            "signal": last_value(macd_values["signal"]),
            "hist": last_value(macd_values["hist"]),
        },
        "adx14": last_value(adx_series),
        "atr14": last_value(atr_series),
        "bollinger20_2": {
            "upper": last_value(bb["upper"]),
            "mid": last_value(bb["mid"]),
            "lower": last_value(bb["lower"]),
        },
        "vwap": vwap,
        "volume_ratio": vol_ratio,
    }
    series = {
        "close": series_tail(close, series_limit),
        "ema20": series_tail(ci.ema(close, 20), series_limit) if not close.empty else [],
        "ema50": series_tail(ci.ema(close, 50), series_limit) if not close.empty else [],
        "rsi14": series_tail(rsi_series, series_limit),
        "macd_hist": series_tail(macd_values["hist"], series_limit),
        "atr14": series_tail(atr_series, series_limit),
    }
    source_ids = list(candle_batch.get("source_ids") or ["chart_candle_batch"])
    input_event_ids = list(candle_batch.get("input_event_ids") or [])
    if candle_batch.get("batch_id"):
        input_event_ids.append(str(candle_batch["batch_id"]))
    material = {
        "symbol": candle_batch.get("symbol"),
        "timeframe": candle_batch.get("timeframe"),
        "batch_id": candle_batch.get("batch_id"),
        "indicators": indicators,
        "series": series,
        "session_timezone": session_timezone,
    }
    indicator_id = stable_digest("chart_indicators", material)
    degradation_state = "quarantined" if errors or candle_batch.get("degradation_state") == "quarantined" else "partial" if warnings or not warmup_complete else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartIndicatorBundle.v1",
        "indicator_id": indicator_id,
        "symbol": str(candle_batch.get("symbol") or "").upper(),
        "timeframe": candle_batch.get("timeframe"),
        "price_basis": candle_batch.get("price_basis"),
        "native_timeframe": bool(candle_batch.get("native_timeframe", True)),
        "source_ids": source_ids,
        "input_event_ids": sorted(set(input_event_ids)),
        "decision_cutoff": candle_batch.get("decision_cutoff") or utc_now(),
        "cutoff_proof": candle_batch.get("cutoff_proof") or {"ok": False, "errors": ["missing_cutoff_proof"]},
        "degradation_state": degradation_state,
        "candle_count": candle_count,
        "min_candle_count": FULL_WARMUP_CANDLES,
        "warmup_complete": warmup_complete,
        "indicator_status": {
            "sma200": "ok" if candle_count >= 200 else "warmup_incomplete",
            "ema200": "ok" if candle_count >= 200 else "warmup_incomplete",
            "rsi14": "ok" if candle_count >= 14 else "warmup_incomplete",
            "macd": "ok" if candle_count >= 26 else "warmup_incomplete",
            "adx14": "ok" if candle_count >= 28 else "warmup_incomplete",
            "atr14": "ok" if candle_count >= 14 else "warmup_incomplete",
            "vwap": vwap["status"],
            "volume_ratio": vol_ratio["status"],
        },
        "capability_mask": {
            "action": "normal" if degradation_state == "ok" else "size_cap" if degradation_state == "partial" else "skip",
            "value_errors": sorted(set(errors)),
            "warnings": sorted(set(warnings)),
            "source_confidence": 1.0 if degradation_state == "ok" else 0.5 if degradation_state == "partial" else 0.0,
        },
        "session": {"timezone": session_timezone, "storage_timezone": "UTC"},
        "indicators": indicators,
        "series": series,
        "created_at": utc_now(),
        "can_place_live_orders": False,
        "live_permission": False,
    }
    validation = validate_chart_contract("ChartIndicatorBundle.v1", payload)
    if not validation.ok:
        payload["degradation_state"] = "quarantined"
        payload["capability_mask"]["action"] = "skip"
        payload["capability_mask"]["value_errors"] = sorted(set(payload["capability_mask"]["value_errors"] + validation.errors))
    return payload


def indicator_path(symbol: str, timeframe: str) -> Path:
    return INDICATOR_DIR / symbol.upper() / f"{timeframe}.jsonl"


def indicator_latest_path(symbol: str, timeframe: str) -> Path:
    return INDICATOR_DIR / symbol.upper() / f"{timeframe}.latest.json"


def store_indicator_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    append_jsonl(indicator_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    write_json_atomic(indicator_latest_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    return bundle


def compute_multi_timeframe_indicators(candle_batches: dict[str, dict[str, Any]], *, series_limit: int = 120, session_timezone: str = "UTC") -> dict[str, Any]:
    bundles = {
        timeframe: compute_indicator_bundle(batch, series_limit=series_limit, session_timezone=session_timezone)
        for timeframe, batch in sorted(candle_batches.items())
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartIndicatorMultiTimeframeBundle.v1",
        "bundle_id": stable_digest("chart_mtf_indicators", {tf: item.get("indicator_id") for tf, item in bundles.items()}),
        "timeframes": bundles,
        "created_at": utc_now(),
        "can_place_live_orders": False,
        "live_permission": False,
    }
