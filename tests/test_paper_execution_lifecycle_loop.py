import json
from decimal import Decimal
from pathlib import Path

import paper_execution_lifecycle_loop as lifecycle
import paper_portfolio_manager as ppm
import market_data_lake as mdl
import chart_paper_snapshot_backfill as cpsb


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def patch_paths(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    account_path = tmp_path / "paper_account.json"
    monkeypatch.setattr(lifecycle, "DECISION_LATEST", memory / "autonomous_paper_trading_loop_latest.json")
    monkeypatch.setattr(lifecycle, "MARKET_LATEST", tmp_path / "market_updates_latest.json")
    monkeypatch.setattr(lifecycle, "LATEST_PATH", memory / "paper_execution_lifecycle_latest.json")
    monkeypatch.setattr(lifecycle, "HISTORY_PATH", memory / "paper_execution_lifecycle_history.jsonl")
    monkeypatch.setattr(lifecycle, "HEARTBEAT_PATH", tmp_path / "paper_execution_lifecycle_loop_heartbeat.json")
    monkeypatch.setattr(lifecycle, "SEEN_PATH", memory / "paper_execution_lifecycle_seen.json")
    monkeypatch.setattr(lifecycle, "PAPER_TRADES_PATH", memory / "paper_trades.jsonl")
    monkeypatch.setattr(mdl, "MARKET_CACHE_DIR", tmp_path / "market_cache")
    monkeypatch.setattr(lifecycle, "evaluate_live_permission", lambda request: {"allowed": True})
    monkeypatch.setattr(lifecycle, "write_latest_report", lambda *args, **kwargs: {"learning_allowed": True, "trade_lifecycle_completeness": 1.0})
    monkeypatch.setattr(lifecycle, "load_account", lambda: ppm.load_account(account_path))
    monkeypatch.setattr(lifecycle, "save_account", lambda account: ppm.save_account(account, account_path))
    monkeypatch.setattr(lifecycle, "open_paper_position", lambda risk, account=None, entry_fee="0": ppm.open_paper_position(risk, account=account, path=account_path, entry_fee=entry_fee))
    monkeypatch.setattr(lifecycle, "close_paper_position", lambda position_id, exit_price, fee="0", reason="manual_sim_close", funding_payment="0": ppm.close_paper_position(position_id, exit_price, fee=fee, reason=reason, path=account_path, funding_payment=funding_payment))
    monkeypatch.setattr(lifecycle, "review_closed_trade", lambda trade, candles, setup_score=None, append=True: {"review_id": "r1", "classification": "good_win"})
    write_json(account_path, ppm.default_account())
    return account_path, memory

def chart_score(cutoff: str = "2026-06-21T00:00:00+00:00") -> dict:
    return {
        "score_id": "chart_score_1",
        "chart_intelligence_id": "chart_intel_1",
        "score": 8.4,
        "tier": "A+",
        "decision_cutoff": cutoff,
        "cutoff_proof": {"ok": True, "errors": []},
        "degradation_state": "ok",
        "capability_mask": {"action": "normal", "value_errors": [], "warnings": [], "source_confidence": 1.0},
        "source_ids": ["chart_setup_scorer"],
        "input_event_ids": ["chart_event_1"],
    }

def chart_risk_plan(cutoff: str = "2026-06-21T00:00:00+00:00") -> dict:
    return {
        "risk_plan_id": "chart_risk_1",
        "decision_cutoff": cutoff,
        "cutoff_proof": {"ok": True, "errors": []},
        "degradation_state": "ok",
        "capability_mask": {"action": "normal", "value_errors": [], "warnings": [], "source_confidence": 1.0},
        "sl": 9,
        "tp_ladder": [{"price": 11, "rr": 1.0}],
    }


def test_lifecycle_opens_latest_paper_candidate(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    decision = {
        "decision": {
            "action": "paper_open_candidate",
            "decided_at": "d1",
            "candidate": {"candidate_id": "c1", "symbol": "ABCUSDT", "side": "LONG", "setup_id": "exhaustion_fade", "market_snapshot_ts": lifecycle.utc_now()},
            "risk_decision": {
                "can_open_paper": True,
                "risk_decision_id": "risk_1",
                "symbol": "ABCUSDT",
                "side": "LONG",
                "setup_id": "exhaustion_fade",
                "entry": "10",
                "sl": "9",
                "tp": "12",
                "qty": "0.02",
                "margin": "0.1",
                "leverage": "2",
                "notional": "0.2",
            },
        }
    }
    write_json(lifecycle.DECISION_LATEST, decision)

    result = lifecycle.run_once()

    account = ppm.load_account(account_path)
    assert result["open_result"]["action"] == "opened"
    assert len(account["open_positions"]) == 1
    assert "paper_open" in lifecycle.PAPER_TRADES_PATH.read_text(encoding="utf-8")
    assert result["can_place_live_orders"] is False

def test_lifecycle_open_and_close_carries_chart_snapshot_lineage(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: "2026-06-21T00:00:20+00:00")
    decision = {
        "decision": {
            "action": "paper_open_candidate",
            "decided_at": "2026-06-21T00:00:20+00:00",
            "chart_risk_plan": chart_risk_plan(),
            "candidate": {
                "candidate_id": "c_chart_1",
                "symbol": "ABCUSDT",
                "side": "LONG",
                "setup_id": "breakout_retest",
                "market_snapshot_ts": "2026-06-21T00:00:10+00:00",
                "feature_id": "feature_1",
                "feature_manifest_id": "manifest_1",
                "feature_artifact_digest": "sha256:feature",
                "chart_score": chart_score(),
                "chart_snapshot_ids": {"candidate": "chart_snapshot_candidate_1"},
                "chart_data_capability_mask": {"action": "normal", "value_errors": [], "warnings": [], "source_confidence": 1.0},
                "chart_data_status": "ok",
            },
            "risk_decision": {
                "can_open_paper": True,
                "risk_decision_id": "risk_chart_1",
                "symbol": "ABCUSDT",
                "side": "LONG",
                "setup_id": "breakout_retest",
                "entry": "10",
                "sl": "9",
                "tp": "11",
                "qty": "0.02",
                "margin": "0.1",
                "leverage": "2",
                "notional": "0.2",
            },
        }
    }
    write_json(lifecycle.DECISION_LATEST, decision)

    opened = lifecycle.try_open_latest_decision(ppm.load_account(account_path), market={})

    assert opened["action"] == "opened"
    open_event = opened["event"]
    assert open_event["chart_score_id"] == "chart_score_1"
    assert open_event["chart_risk_plan_id"] == "chart_risk_1"
    assert open_event["chart_snapshot_ids"]["candidate"] == "chart_snapshot_candidate_1"
    assert open_event["chart_snapshot_ids"]["open"].startswith("paper_open_chart_snapshot_")
    assert open_event["paper_position_snapshot_v2"]["contract"] == "paper_position_snapshot_v2"
    assert open_event["paper_position_snapshot_v2"]["source_digests"]["chart_score"].startswith("sha256:")

    account = ppm.load_account(account_path)
    results = lifecycle.monitor_open_positions(account, {"ts": "2026-06-21T00:01:00+00:00", "hot": [{"symbol": "ABCUSDT", "price": 11.2, "quote_volume": 1000}]})

    close_event = results[-1]["event"]
    assert close_event["event"] == "paper_close"
    assert close_event["chart_score_id"] == "chart_score_1"
    assert close_event["chart_risk_plan_id"] == "chart_risk_1"
    assert close_event["chart_snapshot_ids"]["candidate"] == "chart_snapshot_candidate_1"
    assert close_event["chart_snapshot_ids"]["open"].startswith("paper_open_chart_snapshot_")
    assert close_event["chart_snapshot_ids"]["close"].startswith("paper_close_chart_snapshot_")
    assert close_event["paper_position_snapshot_v2"]["snapshot_id"] == open_event["paper_position_snapshot_v2"]["snapshot_id"]
    assert close_event["position"]["chart_evidence"]["source_hashes"]["chart_score"].startswith("sha256:")

def test_lifecycle_rejects_stale_chart_candidate(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: "2026-06-21T00:30:00+00:00")
    stale_score = chart_score("2026-06-21T00:00:00+00:00")
    decision = {
        "decision": {
            "action": "paper_open_candidate",
            "decided_at": "2026-06-21T00:30:00+00:00",
            "candidate": {
                "candidate_id": "c_stale_chart",
                "symbol": "ABCUSDT",
                "side": "LONG",
                "setup_id": "breakout_retest",
                "market_snapshot_ts": "2026-06-21T00:29:50+00:00",
                "chart_score": stale_score,
                "chart_data_capability_mask": stale_score["capability_mask"],
                "chart_data_status": "ok",
            },
            "risk_decision": {
                "can_open_paper": True,
                "risk_decision_id": "risk_stale_chart",
                "symbol": "ABCUSDT",
                "side": "LONG",
                "setup_id": "breakout_retest",
                "entry": "10",
                "sl": "9",
                "tp": "11",
                "qty": "0.02",
                "margin": "0.1",
                "leverage": "2",
                "notional": "0.2",
            },
        }
    }
    write_json(lifecycle.DECISION_LATEST, decision)

    result = lifecycle.try_open_latest_decision(ppm.load_account(account_path), market={})

    assert result["action"] == "open_skipped"
    assert result["reason"] == "stale_chart_evidence"
    assert result["chart_preflight"]["reject_open"] is True

def test_chart_snapshot_failure_keeps_trade_open_but_marks_degraded(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: "2026-06-21T00:00:20+00:00")
    score = chart_score()
    decision = {
        "decision": {
            "action": "paper_open_candidate",
            "decided_at": "2026-06-21T00:00:20+00:00",
            "candidate": {
                "candidate_id": "c_missing_render_source",
                "symbol": "ABCUSDT",
                "side": "LONG",
                "setup_id": "breakout_retest",
                "market_snapshot_ts": "2026-06-21T00:00:10+00:00",
                "chart_score": score,
                "chart_data_capability_mask": score["capability_mask"],
                "chart_data_status": "ok",
            },
            "risk_decision": {
                "can_open_paper": True,
                "risk_decision_id": "risk_missing_render_source",
                "symbol": "ABCUSDT",
                "side": "LONG",
                "setup_id": "breakout_retest",
                "entry": "10",
                "sl": "9",
                "tp": "11",
                "qty": "0.02",
                "margin": "0.1",
                "leverage": "2",
                "notional": "0.2",
            },
        }
    }
    write_json(lifecycle.DECISION_LATEST, decision)

    opened = lifecycle.try_open_latest_decision(ppm.load_account(account_path), market={})

    assert opened["action"] == "opened"
    assert opened["event"]["chart_evidence_status"] == "degraded"
    assert opened["event"]["chart_learning_eligible"] is False
    assert "missing_chart_candle_batch_for_render" in opened["event"]["position"]["chart_evidence"]["warnings"]

def test_chart_backfill_is_diagnostic_only_and_reconciler_flags_required_missing(tmp_path: Path):
    trades_path = tmp_path / "paper_trades.jsonl"
    output_path = tmp_path / "backfill.jsonl"
    latest_path = tmp_path / "latest.json"
    lifecycle.append_jsonl(trades_path, {"event": "paper_close", "trade_id": "old_1", "symbol": "ABCUSDT", "close_ts": "2026-06-21T00:01:00+00:00", "chart_score_id": "score_old"})

    report = cpsb.backfill_paper_trade_snapshots(trades_path=trades_path, output_path=output_path, latest_path=latest_path)

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert report["backfilled_count"] == 1
    assert rows[0]["diagnostic_only"] is True
    assert rows[0]["readiness_eligible"] is False
    assert cpsb.reconcile_chart_snapshots([{"event": "paper_close", "trade_id": "new_1", "chart_score_id": "score_new"}])["missing_required_count"] == 1
    assert cpsb.reconcile_chart_snapshots(rows)["missing_required_count"] == 0

def test_lifecycle_skips_open_when_candidate_market_snapshot_is_stale(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: "2026-06-21T00:20:01+00:00")
    decision = {
        "decision": {
            "action": "paper_open_candidate",
            "decided_at": "d1",
            "candidate": {"candidate_id": "c1", "symbol": "ABCUSDT", "side": "LONG", "setup_id": "exhaustion_fade", "market_snapshot_ts": "2026-06-21T00:00:00+00:00"},
            "risk_decision": {
                "can_open_paper": True,
                "risk_decision_id": "risk_1",
                "symbol": "ABCUSDT",
                "side": "LONG",
                "setup_id": "exhaustion_fade",
                "entry": "10",
                "sl": "9",
                "tp": "12",
                "qty": "0.02",
                "margin": "0.1",
                "leverage": "2",
                "notional": "0.2",
            },
        }
    }
    write_json(lifecycle.DECISION_LATEST, decision)

    result = lifecycle.try_open_latest_decision(ppm.load_account(account_path), market={})

    account = ppm.load_account(account_path)
    assert result["action"] == "open_skipped"
    assert result["reason"] == "stale_market_snapshot"
    assert account["open_positions"] == []
    assert not lifecycle.PAPER_TRADES_PATH.exists()

def test_lifecycle_allows_fourth_position_when_portfolio_caps_are_clean(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    account = ppm.load_account(account_path)
    for idx in range(3):
        risk = ppm.evaluate_paper_order(
            f"OLD{idx}USDT",
            "LONG",
            "10",
            "9.9",
            "11",
            requested_margin="1",
            requested_leverage="2",
            account=account,
            config={"mode": "paper_learning", "feature_flags": {"paper_trading": True, "live_orders": False}},
        )
        risk["risk_decision_id"] = f"old_risk_{idx}"
        opened = ppm.open_paper_position(risk, account=account, path=account_path)
        account = opened["account"]
    new_risk = ppm.evaluate_paper_order(
        "NEWUSDT",
        "LONG",
        "10",
        "9.9",
        "11",
        requested_margin="1",
        requested_leverage="2",
        account=account,
        config={"mode": "paper_learning", "feature_flags": {"paper_trading": True, "live_orders": False}},
    )
    decision = {
        "decision": {
            "action": "paper_open_candidate",
            "decided_at": "d1",
            "candidate": {"candidate_id": "new_c1", "symbol": "NEWUSDT", "side": "LONG", "setup_id": "funding_squeeze", "market_snapshot_ts": lifecycle.utc_now()},
            "risk_decision": new_risk,
        }
    }
    write_json(lifecycle.DECISION_LATEST, decision)

    result = lifecycle.try_open_latest_decision(ppm.load_account(account_path), market={})

    account_after = ppm.load_account(account_path)
    assert result["action"] == "opened"
    assert len(account_after["open_positions"]) == 4

def test_lifecycle_blocks_portfolio_margin_cap():
    account = {"equity": "100", "open_positions": [{"margin": "84", "side": "LONG", "entry": "10", "sl": "9.9", "qty": "1"}]}
    risk = {"margin": "2", "estimated_loss": "0.1"}

    result = lifecycle.portfolio_open_reject_reason(account, risk)

    assert result["reason"] == "portfolio_margin_cap_reached"

def test_lifecycle_blocks_portfolio_risk_cap():
    account = {"equity": "100", "open_positions": [{"margin": "1", "side": "LONG", "entry": "100", "sl": "85", "qty": "1"}]}
    risk = {"margin": "1", "estimated_loss": "1"}

    result = lifecycle.portfolio_open_reject_reason(account, risk)

    assert result["reason"] == "portfolio_risk_cap_reached"


def test_lifecycle_closes_position_on_mark_tp(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    account = ppm.load_account(account_path)
    risk = ppm.evaluate_paper_order("ABCUSDT", "LONG", "10", "9", "11", requested_margin="0.1", requested_leverage="2", account=account, config={"mode": "paper_learning", "feature_flags": {"paper_trading": True, "live_orders": False}})
    opened = ppm.open_paper_position(risk, account=account, path=account_path)
    open_event = lifecycle.build_open_event(opened["position"], {"candidate_id": "c1"}, {"decided_at": "d1"})
    lifecycle.append_jsonl(lifecycle.PAPER_TRADES_PATH, open_event)
    write_json(lifecycle.DECISION_LATEST, {"decision": {"action": "skip"}})
    write_json(lifecycle.MARKET_LATEST, {"ts": "2026-06-21T00:01:00+00:00", "hot": [{"symbol": "ABCUSDT", "price": 11.2, "quote_volume": 1000}]})

    result = lifecycle.run_once()

    account = ppm.load_account(account_path)
    rows = lifecycle.PAPER_TRADES_PATH.read_text(encoding="utf-8")
    assert "closed" in result["actions"]
    assert account["open_positions"] == []
    assert account["closed_trades"] == 1
    assert "paper_close" in rows
    assert "tp" in rows

def test_lifecycle_closes_missing_mark_position_after_timeout(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(lifecycle, "utc_now", lambda: "2026-06-21T00:31:00+00:00")
    account = ppm.load_account(account_path)
    risk = ppm.evaluate_paper_order("ABCUSDT", "LONG", "10", "9", "11", requested_margin="1", requested_leverage="2", account=account, config={"mode": "paper_learning", "feature_flags": {"paper_trading": True, "live_orders": False}})
    opened = ppm.open_paper_position(risk, account=account, path=account_path)
    position = {**opened["position"], "opened_at": "2026-06-21T00:00:00+00:00"}
    account_payload = opened["account"]
    account_payload["open_positions"] = [position]
    write_json(account_path, account_payload)
    lifecycle.append_jsonl(lifecycle.PAPER_TRADES_PATH, lifecycle.build_open_event(position, {"candidate_id": "c1"}, {"decided_at": "d1"}))
    write_json(lifecycle.DECISION_LATEST, {"decision": {"action": "skip"}})
    write_json(lifecycle.MARKET_LATEST, {"ts": "2026-06-21T00:31:00+00:00", "hot": [{"symbol": "OTHERUSDT", "price": 1, "quote_volume": 1000}]})

    result = lifecycle.run_once(max_hold_seconds=60)

    account_after = ppm.load_account(account_path)
    close_rows = [json.loads(line) for line in lifecycle.PAPER_TRADES_PATH.read_text(encoding="utf-8").splitlines() if json.loads(line).get("event") == "paper_close"]
    assert "closed" in result["actions"]
    assert account_after["open_positions"] == []
    assert close_rows[-1]["reason"] == "missing_mark_price_timeout"
    assert close_rows[-1]["data_quality"] == "mark_only_snapshot"

def test_lifecycle_accounts_for_entry_fee_exit_fee_and_funding(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    account = ppm.load_account(account_path)
    risk = ppm.evaluate_paper_order("ABCUSDT", "LONG", "10", "9.9", "10.5", requested_margin="5", requested_leverage="10", account=account, config={"mode": "paper_learning", "feature_flags": {"paper_trading": True, "live_orders": False}})
    opened = ppm.open_paper_position(risk, account=account, path=account_path, entry_fee="0.025")
    opened["position"]["opened_at"] = "2026-06-21T07:59:00+00:00"
    write_json(account_path, opened["account"])
    account_payload = ppm.load_account(account_path)
    account_payload["open_positions"][0]["opened_at"] = "2026-06-21T07:59:00+00:00"
    write_json(account_path, account_payload)
    lifecycle.append_jsonl(lifecycle.PAPER_TRADES_PATH, lifecycle.build_open_event(opened["position"], {"candidate_id": "c1"}, {"decided_at": "d1"}))
    write_json(lifecycle.DECISION_LATEST, {"decision": {"action": "skip"}})
    write_json(lifecycle.MARKET_LATEST, {"ts": "2026-06-21T08:01:00+00:00", "hot": [{"symbol": "ABCUSDT", "price": 10.5, "funding_pct": 0.1, "quote_volume": 1000}]})

    result = lifecycle.run_once()

    account_after = ppm.load_account(account_path)
    rows = lifecycle.PAPER_TRADES_PATH.read_text(encoding="utf-8")
    close_rows = [json.loads(line) for line in rows.splitlines() if json.loads(line).get("event") == "paper_close"]
    close = close_rows[-1]
    assert result["actions"]
    assert "closed" in result["actions"]
    assert Decimal(close["entry_fee"]) == Decimal("0.025")
    assert Decimal(close["exit_fee"]) == Decimal("0.02624475")
    assert Decimal(close["fee"]) == Decimal("0.05124475")
    assert Decimal(close["funding_payment"]) == Decimal("-0.05")
    assert Decimal(close["slippage"]) > 0
    assert Decimal(close["net"]) == Decimal("2.38825525")
    assert Decimal(account_after["fees_paid"]) == Decimal("0.05124475")
    assert Decimal(account_after["equity"]) == Decimal("102.38825525")
    assert Decimal(account_after["realized_pnl"]) == Decimal("2.38825525")

def test_open_deducts_entry_fee_immediately(tmp_path: Path):
    account_path = tmp_path / "paper_account.json"
    write_json(account_path, ppm.default_account())
    account = ppm.load_account(account_path)
    risk = ppm.evaluate_paper_order("ABCUSDT", "LONG", "10", "9.9", "10.5", requested_margin="5", requested_leverage="10", account=account, config={"mode": "paper_learning", "feature_flags": {"paper_trading": True, "live_orders": False}})

    opened = ppm.open_paper_position(risk, account=account, path=account_path, entry_fee="0.025")

    assert opened["ok"] is True
    assert Decimal(opened["account"]["cash"]) == Decimal("94.975")
    assert Decimal(opened["account"]["equity"]) == Decimal("99.975")
    assert Decimal(opened["account"]["fees_paid"]) == Decimal("0.025")
    assert opened["position"]["entry_fee_paid_at_open"] is True

def test_lifecycle_liquidates_before_sl_when_mark_breaches_liq(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    account = ppm.load_account(account_path)
    risk = ppm.evaluate_paper_order("ABCUSDT", "LONG", "10", "9.9", "12", requested_margin="5", requested_leverage="50", account=account, config={"mode": "paper_learning", "feature_flags": {"paper_trading": True, "live_orders": False}})
    opened = ppm.open_paper_position(risk, account=account, path=account_path)
    lifecycle.append_jsonl(lifecycle.PAPER_TRADES_PATH, lifecycle.build_open_event(opened["position"], {"candidate_id": "c1"}, {"decided_at": "d1"}))
    write_json(lifecycle.DECISION_LATEST, {"decision": {"action": "skip"}})
    write_json(lifecycle.MARKET_LATEST, {"ts": "2026-06-21T00:01:00+00:00", "hot": [{"symbol": "ABCUSDT", "price": 9.84, "quote_volume": 1000}]})

    lifecycle.run_once()

    close_rows = [json.loads(line) for line in lifecycle.PAPER_TRADES_PATH.read_text(encoding="utf-8").splitlines() if json.loads(line).get("event") == "paper_close"]
    close = close_rows[-1]
    assert close["reason"] == "liquidation"
    assert close["promotion_blocked"] is True
    assert Decimal(close["liquidation_price"]) == Decimal("9.85")
    assert Decimal(close["exit"]) <= Decimal(close["liquidation_price"])

def test_legacy_partial_account_load_uses_equity_as_cash(tmp_path: Path):
    account_path = tmp_path / "paper_account.json"
    write_json(account_path, {"starting_equity": "100", "equity": "82"})

    account = ppm.load_account(account_path)

    assert Decimal(account["cash"]) == Decimal("82")
    assert Decimal(account["equity"]) == Decimal("82")
    assert Decimal(account["realized_pnl"]) == Decimal("-18")

def test_close_after_mark_to_market_does_not_double_count_unrealized(tmp_path: Path):
    account_path = tmp_path / "paper_account.json"
    write_json(account_path, ppm.default_account())
    account = ppm.load_account(account_path)
    risk = ppm.evaluate_paper_order("ABCUSDT", "LONG", "10", "9.9", "11", requested_margin="5", requested_leverage="10", account=account, config={"mode": "paper_learning", "feature_flags": {"paper_trading": True, "live_orders": False}})
    opened = ppm.open_paper_position(risk, account=account, path=account_path)
    marked = opened["account"]
    marked["open_positions"][0]["replay_candles"] = [{"ts": "t1", "close": "10.5"}]
    marked = ppm.save_account(marked, account_path)

    closed = ppm.close_paper_position(opened["position"]["position_id"], "10.5", fee="0", account=marked, path=account_path)

    assert Decimal(marked["equity"]) == Decimal("102.5")
    assert Decimal(closed["position"]["net"]) == Decimal("2.5")
    assert Decimal(closed["account"]["equity"]) == Decimal("102.5")
    assert Decimal(closed["account"]["cash"]) == Decimal("102.5")
    assert Decimal(closed["account"]["unrealized_pnl"]) == Decimal("0")

def test_lifecycle_close_writes_replay_candle_cache_when_mark_sequence_available(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    account = ppm.load_account(account_path)
    risk = ppm.evaluate_paper_order("ABCUSDT", "LONG", "10", "9", "11", requested_margin="0.1", requested_leverage="2", account=account, config={"mode": "paper_learning", "feature_flags": {"paper_trading": True, "live_orders": False}})
    opened = ppm.open_paper_position(risk, account=account, path=account_path)
    account_payload = ppm.load_account(account_path)
    account_payload["open_positions"][0]["replay_candles"] = [
        {"ts": "2026-06-21T00:00:00+00:00", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1000},
        {"ts": "2026-06-21T00:01:00+00:00", "open": 10.5, "high": 10.5, "low": 10.5, "close": 10.5, "volume": 1000},
    ]
    write_json(account_path, account_payload)
    lifecycle.append_jsonl(lifecycle.PAPER_TRADES_PATH, lifecycle.build_open_event(opened["position"], {"candidate_id": "c1"}, {"decided_at": "d1"}))
    write_json(lifecycle.DECISION_LATEST, {"decision": {"action": "skip"}})
    write_json(lifecycle.MARKET_LATEST, {"ts": "2026-06-21T00:02:00+00:00", "hot": [{"symbol": "ABCUSDT", "price": 11.2, "quote_volume": 1000}]})

    lifecycle.run_once()

    close_rows = [json.loads(line) for line in lifecycle.PAPER_TRADES_PATH.read_text(encoding="utf-8").splitlines() if json.loads(line).get("event") == "paper_close"]
    close = close_rows[-1]
    cached = mdl.load_candles(close["candle_cache_id"])
    assert close["data_quality"] == "mark_sequence"
    assert close["replay_candle_count"] == 3
    assert len(cached["candles"]) == 3
