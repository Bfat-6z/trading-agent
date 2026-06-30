"""Versioned data contracts for learning artifacts and NeuroCore events."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1
ENVELOPE_SCHEMA_VERSION = "neurocore.v1"


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]

    def payload(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


REQUIRED_FIELDS: dict[str, set[str]] = {
    "paper_trade_event": {"trade_id", "mode", "symbol", "side", "setup_id", "open_ts", "entry", "qty", "margin", "leverage", "sl", "tp", "risk_decision_id", "status"},
    "paper_close_event": {"trade_id", "mode", "symbol", "side", "setup_id", "open_ts", "close_ts", "entry", "exit", "qty", "margin", "leverage", "fee", "slippage", "risk_decision_id", "status"},
    "episode": {"episode_id", "trigger", "goal", "decision", "actions", "outcome", "quality"},
    "risk_decision": {"risk_decision_id", "can_open_paper", "reason"},
    "instrument": {"symbol", "status", "tick_size", "step_size", "min_notional", "max_leverage"},
}

EVENT_SCHEMA_REGISTRY: dict[str, dict[str, Any]] = {
    "paper.order": {"version": "v1", "compatibility": "additive", "required_payload": {"order_id", "symbol", "side", "qty"}, "producers": {"paper_execution_lifecycle_loop", "paper_executor"}},
    "paper.fill": {"version": "v1", "compatibility": "additive", "required_payload": {"order_id", "fill_id", "symbol", "side", "qty", "price"}, "producers": {"paper_execution_lifecycle_loop", "paper_executor"}, "ledger_transaction": True},
    "paper.position_update": {"version": "v1", "compatibility": "additive", "required_payload": {"position_id", "symbol", "side"}, "producers": {"paper_execution_lifecycle_loop", "paper_portfolio_manager"}},
    "paper.close": {"version": "v1", "compatibility": "additive", "required_payload": {"trade_id", "symbol", "side", "entry", "exit"}, "producers": {"paper_execution_lifecycle_loop", "paper_executor"}},
    "paper.liquidation": {"version": "v1", "compatibility": "additive", "required_payload": {"position_id", "symbol", "side", "mark_price"}, "producers": {"paper_execution_lifecycle_loop", "paper_executor"}},
    "funding.settlement": {"version": "v1", "compatibility": "additive", "required_payload": {"position_id", "symbol", "funding_rate", "funding_payment"}, "producers": {"paper_execution_lifecycle_loop", "paper_execution_simulator"}},
    "scan_universe.snapshot": {"version": "v1", "compatibility": "additive", "required_payload": {"universe_id", "symbols"}, "producers": {"paper_candidate_feeder", "market_observer"}},
    "scan_universe.gap": {"version": "v1", "compatibility": "additive", "required_payload": {"source_id", "reason"}, "producers": {"paper_candidate_feeder", "market_observer"}},
    "scan_universe.rate_limited": {"version": "v1", "compatibility": "additive", "required_payload": {"source_id"}, "producers": {"paper_candidate_feeder", "market_observer"}},
    "candidate.generated": {"version": "v1", "compatibility": "additive", "required_payload": {"candidate_id", "symbol", "side"}, "producers": {"paper_candidate_feeder"}},
    "candidate.prefiltered_out": {"version": "v1", "compatibility": "additive", "required_payload": {"candidate_id", "reason"}, "producers": {"paper_candidate_feeder", "inner_critic"}},
    "candidate.not_evaluated": {"version": "v1", "compatibility": "additive", "required_payload": {"candidate_id", "reason"}, "producers": {"paper_candidate_feeder"}},
    "candidate.ranked": {"version": "v1", "compatibility": "additive", "required_payload": {"candidate_id", "rank"}, "producers": {"paper_candidate_feeder", "autonomous_paper_trading_brain"}},
    "candidate.skipped": {"version": "v1", "compatibility": "additive", "required_payload": {"candidate_id", "reason"}, "producers": {"autonomous_paper_trading_brain"}},
    "candidate.expired": {"version": "v1", "compatibility": "additive", "required_payload": {"candidate_id"}, "producers": {"paper_candidate_feeder", "autonomous_paper_trading_brain"}},
    "candidate.selected": {"version": "v1", "compatibility": "additive", "required_payload": {"candidate_id", "symbol", "side"}, "producers": {"autonomous_paper_trading_brain"}},
    "candidate.missed": {"version": "v1", "compatibility": "additive", "required_payload": {"candidate_id", "reason"}, "producers": {"counterfactual_replay_agent", "paper_candidate_feeder"}},
    "social.post.created": {"version": "v1", "compatibility": "additive", "required_payload": {"post_id", "source", "text_hash"}, "producers": {"whale_flow_observer", "news_observer"}, "requires_provenance": True},
    "social.post.edited": {"version": "v1", "compatibility": "additive", "required_payload": {"post_id", "version"}, "producers": {"whale_flow_observer", "news_observer"}, "requires_provenance": True},
    "social.post.deleted": {"version": "v1", "compatibility": "additive", "required_payload": {"post_id"}, "producers": {"whale_flow_observer", "news_observer"}, "requires_provenance": True},
    "social.claim.retracted": {"version": "v1", "compatibility": "additive", "required_payload": {"claim_id", "source"}, "producers": {"whale_flow_observer", "news_observer"}, "requires_provenance": True},
    "news.snapshot.captured": {"version": "v1", "compatibility": "additive", "required_payload": {"snapshot_id", "source_id"}, "producers": {"news_observer"}, "requires_provenance": True},
    "feature.row.created": {"version": "v1", "compatibility": "additive", "required_payload": {"feature_id", "manifest_id", "symbol", "timeframe", "window_start", "window_end", "candle_close_time", "artifact_digest", "decision_cutoff", "cutoff_proof"}, "producers": {"market_feature_store", "feature_factory"}, "requires_provenance": True},
    "chart_intelligence.generated": {"version": "v1", "compatibility": "additive", "required_payload": {"report_id", "symbol", "timeframes", "decision_cutoff", "cutoff_proof"}, "producers": {"chart_intelligence", "chart_setup_scorer"}, "requires_provenance": True},
    "chart.candles.cached": {"version": "v1", "compatibility": "additive", "required_payload": {"batch_id", "symbol", "timeframe", "price_basis", "degradation_state"}, "producers": {"chart_candle_service"}},
    "feature.invalidated": {"version": "v1", "compatibility": "additive", "required_payload": {"feature_id", "reason"}, "producers": {"feature_factory", "news_observer", "whale_flow_observer"}},
    "feature.quarantined": {"version": "v1", "compatibility": "additive", "required_payload": {"feature_id", "reason"}, "producers": {"feature_factory", "market_feature_store"}},
    "event.contract_rejected": {"version": "v1", "compatibility": "additive", "required_payload": {"rejection_id", "event_type", "reason"}, "producers": {"event_store"}, "audit_chain": True},
    "source.degraded": {"version": "v1", "compatibility": "additive", "required_payload": {"source_id", "status", "reason"}, "producers": {"data_source_registry"}},
    "source.restored": {"version": "v1", "compatibility": "additive", "required_payload": {"source_id", "status"}, "producers": {"data_source_registry"}},
    "source.quota_exhausted": {"version": "v1", "compatibility": "additive", "required_payload": {"source_id", "used", "limit"}, "producers": {"data_source_registry", "quota_monitor"}},
    "operator_command.applied": {"version": "v1", "compatibility": "additive", "required_payload": {"command_id", "operator_id", "role"}, "producers": {"operator_console", "legacy_live_blocker"}, "audit_chain": True},
    "operator_command.denied": {"version": "v1", "compatibility": "additive", "required_payload": {"command_id", "operator_id", "reason"}, "producers": {"operator_console", "legacy_live_blocker"}, "audit_chain": True},
    "legacy_script_blocked": {"version": "v1", "compatibility": "additive", "required_payload": {"denial_id", "path", "reason"}, "producers": {"legacy_live_blocker"}, "audit_chain": True},
    "operator_intervention.applied": {"version": "v1", "compatibility": "additive", "required_payload": {"intervention_id", "operator_id", "reason"}, "producers": {"operator_console"}, "audit_chain": True},
    "human_feedback.imported": {"version": "v1", "compatibility": "additive", "required_payload": {"feedback_id", "source_id"}, "producers": {"human_feedback_ledger"}},
    "human_feedback.reviewed": {"version": "v1", "compatibility": "additive", "required_payload": {"feedback_id", "review_id"}, "producers": {"annotation_reviewer"}},
    "human_feedback.rejected": {"version": "v1", "compatibility": "additive", "required_payload": {"feedback_id", "reason", "text_hash"}, "producers": {"human_feedback_ledger"}, "audit_chain": True},
    "vault.generated_conflict": {"version": "v1", "compatibility": "additive", "required_payload": {"conflict_id", "path", "current_sha256"}, "producers": {"obsidian_vault_writer"}, "audit_chain": True},
    "vault.memory_quarantined": {"version": "v1", "compatibility": "additive", "required_payload": {"quarantine_id", "source_digest", "reason"}, "producers": {"obsidian_vault_writer"}, "audit_chain": True},
    "vault.generated_orphan_deleted": {"version": "v1", "compatibility": "additive", "required_payload": {"tombstone_id", "path", "old_sha256"}, "producers": {"obsidian_vault_writer"}, "audit_chain": True},
    "config.changed": {"version": "v1", "compatibility": "additive", "required_payload": {"config_id", "changed_by", "reason"}, "producers": {"runtime_config", "operator_console"}, "audit_chain": True},
    "risk.threshold_changed": {"version": "v1", "compatibility": "additive", "required_payload": {"threshold_id", "changed_by", "old_value", "new_value"}, "producers": {"operator_console", "capital_allocation_policy"}, "audit_chain": True},
    "promotion.decision": {"version": "v1", "compatibility": "additive", "required_payload": {"decision_id", "state", "passed"}, "producers": {"promotion_evaluator_loop"}, "audit_chain": True},
    "daily.root_checkpoint": {"version": "v1", "compatibility": "additive", "required_payload": {"checkpoint_id", "previous_root", "event_seq_start", "event_seq_end"}, "producers": {"agent_runtime_contract", "event_store"}, "audit_chain": True},
    "job.lifecycle": {"version": "v1", "compatibility": "additive", "required_payload": {"job_id", "job_type", "status"}, "producers": {"agent_work_queue"}},
    "legacy.runtime_event": {"version": "v1", "compatibility": "deprecated", "required_payload": {"event"}, "producers": {"event_store", "agent_runtime_contract"}},
}

ENVELOPE_REQUIRED_FIELDS = {
    "event_id",
    "event_type",
    "schema_version",
    "schema_digest",
    "producer_id",
    "producer_version",
    "idempotency_key",
    "payload_hash",
    "occurred_at",
    "available_at",
    "known_at",
    "ingested_at",
    "source_id",
    "correlation_id",
    "priority",
    "payload",
}


CHART_MODEL_VERSION = "chart_intelligence_v1"
ALLOWED_CHART_TIMEFRAMES = {"1D", "4h", "1h", "15m", "5m", "1m"}
CHART_DEGRADATION_STATES = {"ok", "stale", "partial", "diagnostic_only", "quarantined"}
CHART_REASON_CODES = {
    "trend_aligned",
    "ema_ribbon_bull",
    "ema_ribbon_bear",
    "bos_up",
    "bos_down",
    "choch_up",
    "choch_down",
    "liquidity_sweep_up",
    "liquidity_sweep_down",
    "at_support",
    "at_resistance",
    "breakout_retest",
    "overextended",
    "stale_candles",
    "mixed_timeframes",
    "no_sl_level",
    "inside_messy_zone",
    "volume_confirmed",
    "volume_missing",
}
CHART_CONTRACT_REQUIRED_FIELDS: dict[str, set[str]] = {
    "ChartCandleBatch.v1": {"schema_version", "chart_model_version", "contract", "symbol", "timeframe", "closed_only", "source_ids", "input_event_ids", "decision_cutoff", "cutoff_proof", "degradation_state", "bars"},
    "ChartIndicatorBundle.v1": {"schema_version", "chart_model_version", "contract", "symbol", "timeframe", "source_ids", "input_event_ids", "decision_cutoff", "cutoff_proof", "degradation_state", "indicators"},
    "ChartStructureBundle.v1": {"schema_version", "chart_model_version", "contract", "symbol", "timeframe", "source_ids", "input_event_ids", "decision_cutoff", "cutoff_proof", "degradation_state", "structures"},
    "ChartLiquidityBundle.v1": {"schema_version", "chart_model_version", "contract", "symbol", "timeframe", "source_ids", "input_event_ids", "decision_cutoff", "cutoff_proof", "degradation_state", "liquidity"},
    "ChartSetupScore.v1": {"schema_version", "chart_model_version", "contract", "symbol", "side", "setup_family", "score", "confidence", "reason_codes", "blockers", "evidence_ids", "source_ids", "input_event_ids", "decision_cutoff", "cutoff_proof", "degradation_state"},
    "ChartRiskPlan.v1": {"schema_version", "chart_model_version", "contract", "symbol", "side", "entry_reference", "invalidation", "sl", "tp_ladder", "rr", "source_ids", "input_event_ids", "decision_cutoff", "cutoff_proof", "degradation_state"},
    "ChartSnapshot.v1": {"schema_version", "chart_model_version", "contract", "snapshot_id", "symbol", "timeframe", "image_path", "data_hash", "point_ids", "source_ids", "input_event_ids", "decision_cutoff", "cutoff_proof", "degradation_state"},
    "ChartPostTradeReview.v1": {"schema_version", "chart_model_version", "contract", "review_id", "trade_id", "classification", "learning_eligible", "evidence_ids", "source_ids", "input_event_ids", "decision_cutoff", "cutoff_proof", "degradation_state"},
    "ChartIntelligenceReport.v1": {"schema_version", "chart_model_version", "contract", "report_id", "symbol", "timeframes", "capability_mask", "source_ids", "input_event_ids", "decision_cutoff", "cutoff_proof", "degradation_state"},
}

def validate_chart_contract(kind: str, payload: dict[str, Any]) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    required = CHART_CONTRACT_REQUIRED_FIELDS.get(kind)
    if required is None:
        return ValidationResult(False, ["unknown_chart_contract"], [])
    allow_empty_list = {"blockers"} if kind == "ChartSetupScore.v1" else set()
    missing = sorted(field for field in required if payload.get(field) in (None, "") or (payload.get(field) == [] and field not in allow_empty_list))
    if missing:
        errors.append("missing:" + ",".join(missing))
    if payload.get("contract") not in (None, kind):
        errors.append("contract_mismatch")
    version = payload.get("schema_version")
    if version is not None and int(version) > SCHEMA_VERSION:
        errors.append("future_schema_version")
    if payload.get("chart_model_version") not in (None, CHART_MODEL_VERSION):
        errors.append("invalid_chart_model_version")
    timeframe = payload.get("timeframe")
    if timeframe is not None and timeframe not in ALLOWED_CHART_TIMEFRAMES:
        errors.append("invalid_timeframe")
    degradation_state = payload.get("degradation_state")
    if degradation_state is not None and degradation_state not in CHART_DEGRADATION_STATES:
        errors.append("invalid_degradation_state")
    cutoff_proof = payload.get("cutoff_proof")
    if not isinstance(cutoff_proof, dict):
        errors.append("missing_cutoff_proof")
    elif cutoff_proof.get("ok") is not True:
        errors.append("cutoff_proof_not_ok")
    if not isinstance(payload.get("source_ids"), list) or not payload.get("source_ids"):
        errors.append("missing_source_ids")
    if not isinstance(payload.get("input_event_ids"), list):
        errors.append("missing_input_event_ids")
    if kind == "ChartCandleBatch.v1":
        bars = payload.get("bars")
        if not isinstance(bars, list) or not bars:
            errors.append("missing_bars")
        else:
            for idx, bar in enumerate(bars):
                if not isinstance(bar, dict):
                    errors.append(f"invalid_bar:{idx}")
                    continue
                for field in ("open_time", "close_time", "open", "high", "low", "close", "is_final", "available_at", "known_at", "ingested_at", "finalized_at"):
                    if bar.get(field) in (None, ""):
                        errors.append(f"missing_bar_{field}:{idx}")
                if bar.get("is_final") is not True and payload.get("degradation_state") != "diagnostic_only":
                    errors.append(f"forming_candle_requires_diagnostic_only:{idx}")
    if kind == "ChartSetupScore.v1":
        unknown_codes = sorted({str(code) for code in payload.get("reason_codes", []) if str(code) not in CHART_REASON_CODES})
        if unknown_codes:
            errors.append("unknown_reason_codes:" + ",".join(unknown_codes))
        side = str(payload.get("side") or "").upper()
        if side not in {"LONG", "SHORT", "NONE"}:
            errors.append("invalid_side")
    if kind == "ChartStructureBundle.v1":
        structures = payload.get("structures") if isinstance(payload.get("structures"), dict) else {}
        pivots = structures.get("pivots")
        if pivots is not None:
            if not isinstance(pivots, list):
                errors.append("invalid_pivots")
            else:
                for idx, pivot in enumerate(pivots):
                    if not isinstance(pivot, dict):
                        errors.append(f"invalid_pivot:{idx}")
                        continue
                    for field in ("pivot_id", "kind", "price", "confirmed_known_at"):
                        if pivot.get(field) in (None, ""):
                            errors.append(f"missing_pivot_{field}:{idx}")
                    if pivot.get("kind") not in ("high", "low"):
                        errors.append(f"invalid_pivot_kind:{idx}")
                    guard = pivot.get("lookahead_guard") if isinstance(pivot.get("lookahead_guard"), dict) else {}
                    if guard.get("known_at_lte_decision_cutoff") is not True:
                        errors.append(f"pivot_cutoff_guard_not_true:{idx}")
        zones = structures.get("zones")
        if zones is not None:
            if not isinstance(zones, list):
                errors.append("invalid_zones")
            else:
                for idx, zone in enumerate(zones):
                    if not isinstance(zone, dict):
                        errors.append(f"invalid_zone:{idx}")
                        continue
                    for field in ("zone_id", "zone_type", "lower", "upper", "constituent_pivot_ids", "strength"):
                        if zone.get(field) in (None, "", []):
                            errors.append(f"missing_zone_{field}:{idx}")
                    if zone.get("zone_type") not in ("support", "resistance"):
                        errors.append(f"invalid_zone_type:{idx}")
                    try:
                        lower = float(zone.get("lower"))
                        upper = float(zone.get("upper"))
                        strength = float(zone.get("strength"))
                        if lower > upper:
                            errors.append(f"zone_lower_gt_upper:{idx}")
                        if not (0.0 <= strength <= 1.0):
                            errors.append(f"zone_strength_out_of_range:{idx}")
                    except Exception:
                        errors.append(f"invalid_zone_numeric:{idx}")
                relation = structures.get("current_price_relation")
                if relation is not None and not isinstance(relation, dict):
                    errors.append("invalid_current_price_relation")
        trendlines = structures.get("trendlines")
        if trendlines is not None:
            if not isinstance(trendlines, list):
                errors.append("invalid_trendlines")
            else:
                for idx, line in enumerate(trendlines):
                    if not isinstance(line, dict):
                        errors.append(f"invalid_trendline:{idx}")
                        continue
                    for field in ("line_id", "line_type", "pivot_ids", "slope", "intercept", "current_relation", "strength"):
                        if line.get(field) in (None, "", []):
                            errors.append(f"missing_trendline_{field}:{idx}")
                    if line.get("line_type") not in ("support", "resistance"):
                        errors.append(f"invalid_trendline_type:{idx}")
                    try:
                        strength = float(line.get("strength"))
                        if not (0.0 <= strength <= 1.0):
                            errors.append(f"trendline_strength_out_of_range:{idx}")
                    except Exception:
                        errors.append(f"invalid_trendline_numeric:{idx}")
        channels = structures.get("channels")
        if channels is not None:
            if not isinstance(channels, list):
                errors.append("invalid_channels")
            else:
                for idx, channel in enumerate(channels):
                    if not isinstance(channel, dict):
                        errors.append(f"invalid_channel:{idx}")
                        continue
                    for field in ("channel_id", "support_line_id", "resistance_line_id", "width", "current_relation", "strength"):
                        if channel.get(field) in (None, "", []):
                            errors.append(f"missing_channel_{field}:{idx}")
                    try:
                        width = float(channel.get("width"))
                        strength = float(channel.get("strength"))
                        if width <= 0:
                            errors.append(f"channel_width_nonpositive:{idx}")
                        if not (0.0 <= strength <= 1.0):
                            errors.append(f"channel_strength_out_of_range:{idx}")
                    except Exception:
                        errors.append(f"invalid_channel_numeric:{idx}")
    if kind == "ChartLiquidityBundle.v1":
        liquidity = payload.get("liquidity") if isinstance(payload.get("liquidity"), dict) else {}
        events = liquidity.get("events")
        if events is not None and not isinstance(events, list):
            errors.append("invalid_liquidity_events")
        volume = liquidity.get("volume")
        if volume is not None and not isinstance(volume, dict):
            errors.append("invalid_liquidity_volume")
        policy = liquidity.get("liquidity_policy")
        if isinstance(policy, dict) and policy.get("divergence_standalone_entry_allowed") is not False:
            errors.append("divergence_standalone_entry_must_be_false")
    if kind == "ChartRiskPlan.v1":
        try:
            rr = float(payload.get("rr"))
            if rr < 0:
                errors.append("negative_rr")
        except Exception:
            errors.append("invalid_rr")
        if payload.get("can_place_live_orders") is True or payload.get("live_permission") is True:
            errors.append("chart_risk_live_permission_forbidden")
    return ValidationResult(not errors, sorted(set(errors)), warnings)

def require_chart_contract(kind: str, payload: dict[str, Any]) -> None:
    result = validate_chart_contract(kind, payload)
    if not result.ok:
        raise ValueError(f"chart contract {kind} failed: {result.errors}")

def validate_contract(kind: str, payload: dict[str, Any]) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    required = REQUIRED_FIELDS.get(kind)
    if required is None:
        return ValidationResult(False, ["unknown_contract"], [])
    missing = sorted(field for field in required if payload.get(field) in (None, ""))
    if missing:
        errors.append("missing:" + ",".join(missing))
    version = payload.get("schema_version")
    if version is not None and int(version) > SCHEMA_VERSION:
        errors.append("future_schema_version")
    if version is None:
        warnings.append("missing_schema_version")
    return ValidationResult(not errors, errors, warnings)


def canonical_schema_value(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(canonical_schema_value(item) for item in value)
    if isinstance(value, dict):
        return {str(key): canonical_schema_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [canonical_schema_value(item) for item in value]
    return value

def schema_digest(event_type: str) -> str:
    schema = EVENT_SCHEMA_REGISTRY.get(event_type)
    if not schema:
        return "sha256:unknown"
    raw = json.dumps(canonical_schema_value({"event_type": event_type, **schema}), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_event_payload(event_type: str, producer_id: str, payload: dict[str, Any], provenance_id: str | None = None, source_id: str | None = None) -> ValidationResult:
    schema = EVENT_SCHEMA_REGISTRY.get(event_type)
    if not schema:
        return ValidationResult(False, ["unknown_event_type"], [])
    errors: list[str] = []
    warnings: list[str] = []
    allowed = schema.get("producers") or set()
    if allowed and producer_id not in allowed:
        errors.append("unauthorized_producer")
    missing = sorted(field for field in schema.get("required_payload", set()) if payload.get(field) in (None, ""))
    if missing:
        errors.append("missing_payload:" + ",".join(missing))
    if schema.get("requires_provenance") and not provenance_id:
        errors.append("missing_provenance_id")
    if not source_id:
        errors.append("missing_source_id")
    return ValidationResult(not errors, errors, warnings)


def validate_event_envelope(envelope: dict[str, Any]) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    missing = sorted(field for field in ENVELOPE_REQUIRED_FIELDS if envelope.get(field) in (None, ""))
    if missing:
        errors.append("missing_envelope:" + ",".join(missing))
    if envelope.get("schema_version") != ENVELOPE_SCHEMA_VERSION:
        errors.append("invalid_envelope_schema_version")
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    payload_result = validate_event_payload(
        str(envelope.get("event_type") or ""),
        str(envelope.get("producer_id") or ""),
        payload,
        provenance_id=envelope.get("provenance_id"),
        source_id=envelope.get("source_id"),
    )
    errors.extend(payload_result.errors)
    warnings.extend(payload_result.warnings)
    return ValidationResult(not errors, sorted(set(errors)), sorted(set(warnings)))


def require_contract(kind: str, payload: dict[str, Any]) -> None:
    result = validate_contract(kind, payload)
    if not result.ok:
        raise ValueError(f"contract {kind} failed: {result.errors}")
