from decimal import Decimal
from datetime import datetime, timedelta, timezone
from argparse import Namespace
from pathlib import Path

from scalp_autotrader import OrderPlan, ScalpAutoTrader, Signal, ema, floor_to_step, parse_args, rsi, score_signal


def test_floor_to_step_rounds_down():
    assert floor_to_step(Decimal("12.349"), Decimal("0.01")) == Decimal("12.34")
    assert floor_to_step(Decimal("88.9"), Decimal("0.1")) == Decimal("88.9")


def test_ema_tracks_trend_direction():
    up = [float(i) for i in range(1, 40)]
    down = list(reversed(up))
    assert ema(up, 9) > ema(up, 21)
    assert ema(down, 9) < ema(down, 21)


def test_rsi_bounds():
    vals = [1, 2, 3, 2, 4, 5, 4, 6, 7, 7, 8, 9, 8, 9, 10, 11]
    value = rsi([float(v) for v in vals])
    assert 0 <= value <= 100


def test_score_signal_long_requires_clear_edge():
    signal = score_signal(
        symbol="TESTUSDT",
        price=101.0,
        quote_volume_m=100.0,
        spread_pct=0.01,
        change_3m_pct=0.12,
        change_5m_pct=0.20,
        change_10m_pct=0.30,
        volume_ratio_1m=1.1,
        rsi_1m=55.0,
        taker_flow_last=1.2,
        taker_flow_avg=1.1,
        ema9=100.5,
        ema21=100.0,
    )
    assert signal is not None
    assert signal.side == "LONG"
    assert signal.score >= 6


def test_score_signal_rejects_chop():
    signal = score_signal(
        symbol="TESTUSDT",
        price=100.0,
        quote_volume_m=100.0,
        spread_pct=0.01,
        change_3m_pct=0.01,
        change_5m_pct=-0.01,
        change_10m_pct=0.0,
        volume_ratio_1m=0.4,
        rsi_1m=50.0,
        taker_flow_last=1.0,
        taker_flow_avg=1.0,
        ema9=100.0,
        ema21=100.0,
    )
    assert signal is None

def test_parse_args_defaults_include_live_gate_and_adaptive_caps():
    args = parse_args([])

    assert args.live is False
    assert args.paper_equity == 100.0
    assert args.reset_paper_account is False
    assert args.reset_paper_account_only is False
    assert args.live_required_win_rate == 0.80
    assert args.live_min_paper_trades == 30
    assert args.min_margin_usdt <= args.margin_usdt
    assert args.min_leverage <= args.leverage
    assert args.shadow_log_interval_seconds == 60.0


def test_memory_sleep_active_detects_future_sleep_until():
    bot = ScalpAutoTrader.__new__(ScalpAutoTrader)
    sleep_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")

    active, parsed_until = bot.memory_sleep_active({"sleep_until": sleep_until})

    assert active is True
    assert parsed_until is not None

def test_paper_can_bypass_memory_sleep_for_sample_collection():
    bot = ScalpAutoTrader.__new__(ScalpAutoTrader)
    sleep_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    events = []
    bot.args = Namespace(
        live=False,
        paper_trade_through_memory_sleep=True,
        max_consecutive_losses=2,
        loss_sleep_seconds=0,
        daily_loss_limit_usdt=10.0,
        cooldown_seconds=0.0,
    )
    bot.memory_bias = lambda: {"sleep_until": sleep_until, "updated_at": "now"}
    bot.log = lambda event, payload: events.append((event, payload))
    bot.log_risk_block = lambda reason, payload: events.append(("risk_block", {"reason": reason, **payload}))
    bot.consecutive_losses = 0
    bot.realized_today = Decimal("0")
    bot.last_trade_closed_at = 0.0
    bot.trades_by_bias_update = {}
    bot.last_risk_block_reason = None
    bot.bias_key = lambda bias: "now"

    assert bot.risk_allows_new_trade() is True
    assert events[0][0] == "paper_memory_sleep_bypass"

def test_live_never_bypasses_memory_sleep():
    bot = ScalpAutoTrader.__new__(ScalpAutoTrader)
    sleep_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    events = []
    bot.args = Namespace(live=True, paper_trade_through_memory_sleep=True)
    bot.memory_bias = lambda: {"sleep_until": sleep_until, "updated_at": "now"}
    bot.log_risk_block = lambda reason, payload: events.append((reason, payload))
    bot.live_performance_gate_allows = lambda: True
    bot.refresh_live_realized = lambda: None
    bot.last_risk_block_reason = None

    assert bot.risk_allows_new_trade() is False
    assert events[0][0] == "memory_sleep"


def test_live_protection_state_detects_reduce_only_stop_and_tp():
    class FakeClient:
        def futures_get_open_orders(self, symbol):
            assert symbol == "TESTUSDT"
            return [
                {"type": "STOP_MARKET", "reduceOnly": True},
                {"type": "TAKE_PROFIT_MARKET", "reduceOnly": "true"},
            ]

    bot = ScalpAutoTrader.__new__(ScalpAutoTrader)
    bot.client = FakeClient()

    has_stop, has_tp, orders = bot.live_protection_state("TESTUSDT")

    assert has_stop is True
    assert has_tp is True
    assert len(orders) == 2

def _planner_bot(tmp_path: Path) -> ScalpAutoTrader:
    bot = ScalpAutoTrader.__new__(ScalpAutoTrader)
    bot.args = Namespace(
        margin_usdt=2.0,
        min_margin_usdt=0.25,
        leverage=20,
        min_leverage=3,
        min_quote_volume_m=20.0,
        paper_performance_window=10,
        live_min_paper_trades=5,
        live_required_win_rate=0.80,
        live_required_net_usdt=0.0,
        allow_live_without_paper_edge=False,
        risk_log_interval_seconds=0,
        shadow_log_interval_seconds=0,
        take_profit_pct=0.4,
        stop_loss_pct=0.25,
    )
    bot.log_path = tmp_path / "scalp.jsonl"
    bot.paper_position = None
    bot.trades_by_bias_update = {}
    bot.last_risk_block_reason = None
    bot.last_shadow_log_at = {}
    bot.memory_bias = lambda: {}
    bot.log = lambda event, payload: None
    bot.last_risk_log_at = {}
    return bot

def _strong_signal() -> Signal:
    return Signal(
        symbol="TESTUSDT",
        side="SHORT",
        score=8,
        long_score=1,
        short_score=8,
        price=100.0,
        quote_volume_m=600.0,
        spread_pct=0.01,
        change_3m_pct=-0.2,
        change_5m_pct=-0.6,
        change_10m_pct=-0.8,
        volume_ratio_1m=1.3,
        rsi_1m=45.0,
        taker_flow_last=0.8,
        taker_flow_avg=0.9,
        reasons=["test"],
    )

def test_order_plan_is_dynamic_and_within_caps(tmp_path: Path):
    bot = _planner_bot(tmp_path)

    plan = bot.order_plan(_strong_signal())

    assert Decimal("0.25") <= plan.margin_usdt <= Decimal("2.0")
    assert 3 <= plan.leverage <= 20
    assert plan.notional == plan.margin_usdt * Decimal(str(plan.leverage))
    assert plan.confidence > 0

def test_paper_account_initializes_and_resets_to_100(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("scalp_autotrader.spot_client", lambda: object())
    args = parse_args(["--state-dir", str(tmp_path), "--paper-equity", "100"])

    bot = ScalpAutoTrader(args)

    assert bot.paper_equity == Decimal("100.0")
    assert (tmp_path / "paper_account.json").exists()
    (tmp_path / "paper_account.json").write_text('{"equity":"87.5"}\n', encoding="utf-8")

    bot2 = ScalpAutoTrader(args)

    assert bot2.paper_equity == Decimal("87.5")
    reset_args = parse_args(["--state-dir", str(tmp_path), "--paper-equity", "100", "--reset-paper-account"])
    bot3 = ScalpAutoTrader(reset_args)

    assert bot3.paper_equity == Decimal("100.0")

def test_write_paper_account_persists_equity(tmp_path: Path):
    bot = ScalpAutoTrader.__new__(ScalpAutoTrader)
    bot.args = Namespace(live=False, paper_equity=100.0)
    bot.paper_account_path = tmp_path / "paper_account.json"

    bot.write_paper_account(Decimal("101.25"), "paper_close")

    payload = bot.paper_account_path.read_text(encoding="utf-8")
    assert '"equity": "101.25"' in payload
    assert '"starting_equity": "100.0"' in payload

def test_reset_paper_account_only_writes_100_and_logs(tmp_path: Path):
    args = parse_args(["--state-dir", str(tmp_path), "--paper-equity", "100", "--reset-paper-account-only"])

    path = __import__("scalp_autotrader").reset_paper_account_only(args)

    payload = path.read_text(encoding="utf-8")
    assert '"equity": "100.0"' in payload
    assert '"reason": "manual_reset"' in payload
    log_text = (tmp_path / "scalp_autotrader.jsonl").read_text(encoding="utf-8")
    assert "paper_account_reset" in log_text

def test_open_paper_respects_inner_critic_block(tmp_path: Path, monkeypatch):
    bot = _planner_bot(tmp_path)
    events = []
    bot.log = lambda event, payload: events.append((event, payload))
    monkeypatch.setattr(
        "scalp_autotrader.evaluate_signal",
        lambda signal, bias=None: {"verdict": "block", "reasons": ["test_block"], "can_loosen": False},
    )

    bot.open_paper(_strong_signal())

    assert bot.paper_position is None
    assert events[0][0] == "inner_critic_block"
    assert events[0][1]["critic"]["reasons"] == ["test_block"]

def test_open_paper_bypasses_inner_critic_memory_sleep_for_sample_collection(tmp_path: Path, monkeypatch):
    bot = _planner_bot(tmp_path)
    sleep_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    events = []
    captured = {}
    bot.args.live = False
    bot.args.paper_trade_through_memory_sleep = True
    bot.memory_bias = lambda: {"sleep_until": sleep_until, "updated_at": "now", "min_signal_score": 7}
    bot.log = lambda event, payload: events.append((event, payload))
    bot.symbol_filters = lambda symbol: (Decimal("0.01"), Decimal("0.001"))

    def fake_evaluate_signal(signal, bias=None):
        captured.update(bias or {})
        return {"verdict": "allow_paper", "reasons": ["critic_passed"], "can_loosen": False}

    monkeypatch.setattr("scalp_autotrader.evaluate_signal", fake_evaluate_signal)

    bot.open_paper(_strong_signal())

    assert bot.paper_position is not None
    assert "sleep_until" not in captured
    assert captured["min_signal_score"] == 7
    assert any(event == "paper_inner_critic_memory_sleep_bypass" for event, _ in events)

def test_live_critic_bias_never_bypasses_memory_sleep(tmp_path: Path):
    bot = _planner_bot(tmp_path)
    sleep_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    bias = {"sleep_until": sleep_until, "updated_at": "now"}
    bot.args.live = True
    bot.args.paper_trade_through_memory_sleep = True

    critic_bias = bot.critic_bias_for_paper_sample(bias)

    assert critic_bias == bias
    assert critic_bias["sleep_until"] == sleep_until

def test_log_shadow_trade_records_would_trade_without_position(tmp_path: Path, monkeypatch):
    bot = _planner_bot(tmp_path)
    events = []
    bot.log = lambda event, payload: events.append((event, payload))
    bot.symbol_filters = lambda symbol: (Decimal("0.01"), Decimal("0.001"))
    monkeypatch.setattr("scalp_autotrader.append_shadow", lambda path, shadow: events.append(("shadow_file", shadow)))

    shadow = bot.log_shadow_trade(_strong_signal(), "memory_sleep")

    assert bot.paper_position is None
    assert shadow is not None
    assert shadow["no_execution"] is True
    assert shadow["block_reason"] == "memory_sleep"
    assert any(event == "shadow_open" for event, _ in events)

def test_tick_shadow_logs_raw_signal_when_risk_blocks(tmp_path: Path):
    bot = _planner_bot(tmp_path)
    captured = []
    bot.args.live = False
    bot.paper_position = None
    bot.manage_paper = lambda: None
    bot.risk_allows_new_trade = lambda: (setattr(bot, "last_risk_block_reason", "memory_sleep") or False)
    bot.scan_once = lambda apply_memory_bias=True: [_strong_signal()]
    bot.log_shadow_trade = lambda signal, reason: captured.append((signal.symbol, reason))

    bot.tick()

    assert captured == [("TESTUSDT", "memory_sleep")]

def test_open_paper_logs_inner_critic_allow_payload(tmp_path: Path, monkeypatch):
    bot = _planner_bot(tmp_path)
    events = []
    bot.log = lambda event, payload: events.append((event, payload))
    bot.symbol_filters = lambda symbol: (Decimal("0.01"), Decimal("0.001"))
    critic = {"verdict": "allow_paper", "reasons": ["critic_passed"], "can_loosen": False}
    monkeypatch.setattr("scalp_autotrader.evaluate_signal", lambda signal, bias=None: critic)

    bot.open_paper(_strong_signal())

    paper_open = next(payload for event, payload in events if event == "paper_open")

    assert bot.paper_position is not None
    assert paper_open["critic"] == critic
    assert paper_open["signal"]["symbol"] == "TESTUSDT"

def test_open_paper_bumps_margin_to_exchange_min_qty(tmp_path: Path, monkeypatch):
    bot = _planner_bot(tmp_path)
    events = []
    bot.paper_equity = Decimal("100")
    bot.log = lambda event, payload: events.append((event, payload))
    bot.symbol_filters = lambda symbol: (Decimal("0.01"), Decimal("0.1"))
    bot.order_plan = lambda signal: OrderPlan(
        margin_usdt=Decimal("0.3000"),
        leverage=5,
        notional=Decimal("1.5000"),
        confidence=0.5,
        entry_type="MARKET_SMALL",
        reasons=["test"],
    )
    monkeypatch.setattr(
        "scalp_autotrader.evaluate_signal",
        lambda signal, bias=None: {"verdict": "allow_paper", "reasons": ["critic_passed"], "can_loosen": False},
    )

    bot.open_paper(_strong_signal())

    assert bot.paper_position is not None
    assert bot.paper_position.qty == Decimal("0.1")
    assert bot.paper_position.margin_usdt == Decimal("2.0000")
    assert any(event == "paper_min_qty_margin_bump" for event, _ in events)
    paper_open = next(payload for event, payload in events if event == "paper_open")
    assert "min_qty_margin_bump" in paper_open["order_plan"]["reasons"]

def test_open_paper_blocks_when_equity_cannot_reach_exchange_min_qty(tmp_path: Path, monkeypatch):
    bot = _planner_bot(tmp_path)
    events = []
    bot.paper_equity = Decimal("1")
    bot.log = lambda event, payload: events.append((event, payload))
    bot.symbol_filters = lambda symbol: (Decimal("0.01"), Decimal("0.1"))
    bot.order_plan = lambda signal: OrderPlan(
        margin_usdt=Decimal("0.3000"),
        leverage=5,
        notional=Decimal("1.5000"),
        confidence=0.5,
        entry_type="MARKET_SMALL",
        reasons=["test"],
    )
    monkeypatch.setattr(
        "scalp_autotrader.evaluate_signal",
        lambda signal, bias=None: {"verdict": "allow_paper", "reasons": ["critic_passed"], "can_loosen": False},
    )

    bot.open_paper(_strong_signal())

    assert bot.paper_position is None
    paper_block = next(payload for event, payload in events if event == "paper_open_block")
    assert paper_block["reason"] == "paper_equity_below_min_qty_margin"

def test_live_performance_gate_blocks_until_enough_paper_samples(tmp_path: Path):
    bot = _planner_bot(tmp_path)
    bot.log_risk_block = lambda reason, payload: setattr(bot, "last_block", reason)
    bot.log_path.write_text(
        '{"event":"paper_close","net":"0.1"}\n' * 4,
        encoding="utf-8",
    )

    assert bot.live_performance_gate_allows() is False
    assert bot.last_block == "live_paper_sample_too_small"

def test_live_performance_gate_requires_80_percent_win_rate(tmp_path: Path):
    bot = _planner_bot(tmp_path)
    bot.log_risk_block = lambda reason, payload: setattr(bot, "last_block", reason)
    rows = ['{"event":"paper_close","net":"0.1"}'] * 3 + ['{"event":"paper_close","net":"-0.1"}'] * 2
    bot.log_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    assert bot.live_performance_gate_allows() is False
    assert bot.last_block == "live_win_rate_below_gate"

def test_live_performance_gate_allows_when_paper_edge_is_strong(tmp_path: Path):
    bot = _planner_bot(tmp_path)
    rows = ['{"event":"paper_close","net":"0.1"}'] * 4 + ['{"event":"paper_close","net":"-0.01"}']
    bot.log_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    assert bot.live_performance_gate_allows() is True
