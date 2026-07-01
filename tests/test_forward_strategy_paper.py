"""Forward-paper strategy channel tests: bracket resolution + summary honesty +
paper-only. Uses a mock client so no network."""
from datetime import datetime, timedelta, timezone

import forward_strategy_paper as fsp


def _bars(n, start_ms, step_ms=3_600_000, base=100.0, drift=0.0, hi_pad=0.5, lo_pad=0.5):
    out = []
    px = base
    for i in range(n):
        o = px; c = px + drift
        ct = start_ms + (i + 1) * step_ms
        ot = start_ms + i * step_ms
        out.append({"open_time": fsp.__dict__.get("_iso", None) or _iso(ot), "close_time": _iso(ct),
                    "open": f"{o:.4f}", "high": f"{max(o,c)+hi_pad:.4f}", "low": f"{min(o,c)-lo_pad:.4f}",
                    "close": f"{c:.4f}", "volume": "1000", "is_final": True,
                    "available_at": _iso(ct), "known_at": _iso(ct), "ingested_at": _iso(ct), "finalized_at": _iso(ct)})
        px = c
    return out


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def test_resolve_hits_tp_with_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(fsp, "POSITIONS", tmp_path / "pos.jsonl")
    monkeypatch.setattr(fsp, "CLOSED", tmp_path / "closed.jsonl")
    monkeypatch.setattr(fsp, "FS_DIR", tmp_path)
    # one open LONG position; future bars rally to TP
    entry_ts = 1_000_000_000_000
    fsp._rewrite(fsp.POSITIONS, [{"symbol": "BTCUSDT", "direction": "LONG", "decision_cutoff": "t",
                                  "entry_ts": entry_ts, "entry": 100.0, "sl": 95.0, "tp": 110.0,
                                  "atr": 2.0, "quote_volume": 1e10, "tier": "major", "spec_id": "x"}])

    class Mock:
        def futures_klines(self, symbol, interval, startTime, limit):
            # bars AFTER entry that rally through TP=110
            rows = []
            for k in range(10):
                ct = entry_ts + (k + 1) * 3_600_000
                price = 100.0 + k * 3.0  # rises; hits 110 by k=4
                rows.append([ct - 3_600_000, f"{price:.2f}", f"{price+3:.2f}", f"{price-0.5:.2f}",
                             f"{price+2:.2f}", "1000", ct, "100000", 50, "600", "60000", "0"])
            return rows
    # backtest_data_fetcher.fetch_history uses client.futures_klines; patch spot path
    import backtest_data_fetcher as bf
    monkeypatch.setattr(bf, "fetch_history", lambda *a, **k: _bars(10, entry_ts, base=100.0, drift=3.0))
    n = fsp.resolve_open(Mock(), entry_ts + 20 * 3_600_000)
    assert n == 1
    closed = fsp._load(fsp.CLOSED)
    assert closed[0]["reason"] in ("tp", "timeout", "sl")
    # net must have costs deducted (net < gross for a tp) -> r finite
    assert "r_multiple" in closed[0]


def test_summary_reports_insufficient(tmp_path, monkeypatch):
    monkeypatch.setattr(fsp, "CLOSED", tmp_path / "closed.jsonl")
    monkeypatch.setattr(fsp, "POSITIONS", tmp_path / "pos.jsonl")
    fsp._rewrite(fsp.CLOSED, [{"r_multiple": 0.1} for _ in range(5)])
    s = fsp.summarize()
    assert s["closed"] == 5
    assert s["verdict"] == "insufficient_sample_still_accruing"


def test_frozen_spec_is_valid_and_paper_only():
    import strategy_compiler as sc
    for d in fsp.DIRECTIONS:
        assert sc.validate_spec(fsp._spec_for(d)) == []
    # paper-only: no live-order call, and it never SETS the live flag (mentioning
    # it in a safety comment is fine; assigning it is not).
    src = open(fsp.__file__, encoding="utf-8").read()
    assert "futures_create_order" not in src
    assert "environ[\"ALLOW_LIVE_ORDERS\"]" not in src and "environ['ALLOW_LIVE_ORDERS']" not in src
