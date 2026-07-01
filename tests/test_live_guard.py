"""Phase 0.4 — fail-closed live-order guard at the futures chokepoint."""
import importlib

import pytest


lg = importlib.import_module("tradingagents.binance.live_guard")


def test_blocked_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    assert lg.live_orders_allowed() is False
    with pytest.raises(lg.LiveOrdersBlocked):
        lg.assert_live_orders_allowed("open_long")


@pytest.mark.parametrize("val", ["0", "false", "no", "", "  ", "off"])
def test_falsy_values_block(monkeypatch, val):
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", val)
    with pytest.raises(lg.LiveOrdersBlocked):
        lg.assert_live_orders_allowed()


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "ALLOW"])
def test_explicit_allow_passes(monkeypatch, val):
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", val)
    assert lg.live_orders_allowed() is True
    lg.assert_live_orders_allowed()  # must not raise


def test_testnet_is_exempt(monkeypatch):
    monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    lg.assert_live_orders_allowed()  # testnet is not real money


def test_futures_open_long_blocks_before_hitting_api(monkeypatch):
    """The wrapper must raise the guard BEFORE any network/order call."""
    monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    fut = importlib.import_module("tradingagents.binance.futures")

    # Stub everything the wrapper touches before the guard so the ONLY thing
    # that can stop it is the guard. If the guard is missing, this would try
    # to build a real client and fail differently.
    class _FakeClient:
        def futures_mark_price(self, symbol):
            return {"markPrice": "100"}
        def futures_create_order(self, **kw):  # pragma: no cover - must never run
            raise AssertionError("LIVE ORDER REACHED API — guard failed!")

    monkeypatch.setattr(fut, "spot_client", lambda: _FakeClient())
    monkeypatch.setattr(fut, "set_isolated_margin", lambda *a, **k: None)
    monkeypatch.setattr(fut, "set_leverage", lambda *a, **k: None)
    monkeypatch.setattr(fut, "_futures_filters", lambda s: {"step_size": 0.001, "min_qty": 0.0})

    with pytest.raises(lg.LiveOrdersBlocked):
        fut.open_long("BTCUSDT", margin_usdt=5, leverage=5)
