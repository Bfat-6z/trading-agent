import json
from decimal import Decimal
from pathlib import Path

import paper_execution_lifecycle_loop as lifecycle
import paper_portfolio_manager as ppm
import market_data_lake as mdl


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


def test_lifecycle_opens_latest_paper_candidate(monkeypatch, tmp_path: Path):
    account_path, memory = patch_paths(monkeypatch, tmp_path)
    decision = {
        "decision": {
            "action": "paper_open_candidate",
            "decided_at": "d1",
            "candidate": {"candidate_id": "c1", "symbol": "ABCUSDT", "side": "LONG", "setup_id": "exhaustion_fade", "market_snapshot_ts": "t1"},
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
    assert Decimal(close["exit_fee"]) == Decimal("0.02625")
    assert Decimal(close["fee"]) == Decimal("0.05125")
    assert Decimal(close["funding_payment"]) == Decimal("-0.05")
    assert Decimal(close["net"]) == Decimal("2.39875")
    assert Decimal(account_after["fees_paid"]) == Decimal("0.05125")
    assert Decimal(account_after["equity"]) == Decimal("102.39875")
    assert Decimal(account_after["realized_pnl"]) == Decimal("2.39875")

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
