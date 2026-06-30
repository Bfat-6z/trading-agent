"""No-lookahead replay helper for chart intelligence."""
from __future__ import annotations

import hashlib
from typing import Any

import chart_candle_service as ccs
import chart_indicator_engine as cie
import chart_liquidity_detector as cld
import chart_pivot_detector as cpd
import chart_setup_scorer as css
import chart_structure_detector as csd
import chart_trend_regime as ctr
import chart_trendline_detector as ctl
import chart_zone_detector as czd
from atomic_state import canonical_json
from timebase import parse_utc, utc_now


def stable_digest(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]


def bar_known_at(bar: dict[str, Any]) -> str | None:
    return cld.bar_known_at(bar)


def usable_bars_at_cutoff(candle_batch: dict[str, Any], cutoff: str) -> tuple[list[dict[str, Any]], list[str], int]:
    cutoff_dt = parse_utc(cutoff)
    errors: list[str] = []
    ignored = 0
    rows: list[dict[str, Any]] = []
    if not cutoff_dt:
        return [], ["invalid_decision_cutoff"], 0
    for idx, bar in enumerate(candle_batch.get("bars") or []):
        if not isinstance(bar, dict):
            errors.append(f"invalid_bar:{idx}")
            continue
        missing = [field for field in ("available_at", "known_at", "ingested_at", "finalized_at") if bar.get(field) in (None, "")]
        if missing:
            errors.append(f"missing_finality_metadata:{idx}:{','.join(missing)}")
            continue
        known = parse_utc(bar_known_at(bar))
        if not known:
            errors.append(f"invalid_finality_metadata:{idx}")
            continue
        close_dt = parse_utc(bar.get("close_time"))
        if known > cutoff_dt and close_dt and close_dt <= cutoff_dt:
            errors.append(f"known_after_cutoff:{idx}")
            continue
        if known > cutoff_dt:
            ignored += 1
            continue
        if bar.get("is_final") is not True:
            errors.append(f"forming_candle:{idx}")
            continue
        rows.append(bar)
    rows.sort(key=lambda row: str(row.get("open_time") or ""))
    return rows, sorted(set(errors)), ignored


def rebuild_candle_batch_at_cutoff(candle_batch: dict[str, Any], cutoff: str) -> dict[str, Any]:
    rows, errors, ignored = usable_bars_at_cutoff(candle_batch, cutoff)
    replay = ccs.build_chart_candle_batch(
        str(candle_batch.get("symbol") or ""),
        str(candle_batch.get("timeframe") or ""),
        rows,
        decision_cutoff=cutoff,
        source_id="chart_no_lookahead_replay",
        provider=str((candle_batch.get("source_policy") or {}).get("provider") or candle_batch.get("provider") or "replay"),
        exchange=str(candle_batch.get("exchange") or "BINANCE_USDM"),
        price_basis=str(candle_batch.get("price_basis") or "last_trade"),
        server_time=cutoff,
        ingested_at=cutoff,
        input_event_ids=list(candle_batch.get("input_event_ids") or []),
        source_manifest_ids=list(candle_batch.get("source_ids") or []),
        native_timeframe=bool(candle_batch.get("native_timeframe", True)),
        min_candles=1,
    )
    replay["replay"] = {"cutoff": cutoff, "ignored_after_cutoff_count": ignored, "strict_errors": errors, "source_batch_id": candle_batch.get("batch_id")}
    if errors:
        replay["degradation_state"] = "quarantined"
        replay["capability_mask"]["action"] = "skip"
        replay["capability_mask"]["value_errors"] = sorted(set(replay["capability_mask"]["value_errors"] + errors))
    return replay


def rebuild_chart_decision(
    candle_batch: dict[str, Any],
    *,
    cutoff: str,
    side: str,
    setup_family: str = "trend_continuation",
) -> dict[str, Any]:
    replay_batch = rebuild_candle_batch_at_cutoff(candle_batch, cutoff)
    indicators = cie.compute_indicator_bundle(replay_batch)
    trend = ctr.classify_timeframe_trend(indicators)
    pivots = cpd.compute_pivot_bundle(replay_batch)
    zones = czd.compute_zone_bundle(pivots, candle_batch=replay_batch, indicator_bundle=indicators)
    trendlines = ctl.compute_trendline_bundle(pivots, candle_batch=replay_batch, indicator_bundle=indicators)
    structure = csd.compute_market_structure_bundle(pivots, candle_batch=replay_batch, indicator_bundle=indicators)
    liquidity = cld.compute_liquidity_bundle(replay_batch, indicator_bundle=indicators, zone_bundle=zones)
    score = css.score_chart_setup(
        symbol=str(replay_batch.get("symbol") or ""),
        side=side,
        setup_family=setup_family,
        trend_bundle=trend,
        zone_bundle=zones,
        structure_bundle=structure,
        liquidity_bundle=liquidity,
        chart_intelligence_id=None,
    )
    artifacts = {
        "candle_batch": replay_batch,
        "indicators": indicators,
        "trend": trend,
        "pivots": pivots,
        "zones": zones,
        "trendlines": trendlines,
        "structure": structure,
        "liquidity": liquidity,
        "score": score,
    }
    summary = {
        "schema_version": 1,
        "replay_id": stable_digest("chart_replay", {"cutoff": cutoff, "score_id": score.get("score_id"), "batch_id": replay_batch.get("batch_id")}),
        "cutoff": cutoff,
        "symbol": replay_batch.get("symbol"),
        "timeframe": replay_batch.get("timeframe"),
        "price_basis": replay_batch.get("price_basis"),
        "native_timeframe": replay_batch.get("native_timeframe"),
        "score_id": score.get("score_id"),
        "score": score.get("score"),
        "tier": score.get("tier"),
        "artifact_hash": stable_digest("chart_replay_artifacts", {key: value.get("score_id") or value.get("structure_id") or value.get("liquidity_id") or value.get("indicator_id") or value.get("batch_id") for key, value in artifacts.items()}),
        "degradation_state": "quarantined" if any(item.get("degradation_state") == "quarantined" for item in artifacts.values() if isinstance(item, dict)) else "ok",
        "created_at": utc_now(),
        "can_place_live_orders": False,
        "live_permission": False,
    }
    return {"summary": summary, "artifacts": artifacts, "can_place_live_orders": False, "live_permission": False}
