"""HARNESS-B tests: forward-test channel is no-lookahead + honest about sample."""
import forward_test_harness as ft


class MockClient:
    def __init__(self, mark=100.0, fwd=105.0):
        self._mark = mark
        self._fwd = fwd
    def futures_order_book(self, symbol, limit=20):
        return {"bids": [["100", "10"], ["99", "5"]], "asks": [["101", "3"], ["102", "2"]]}
    def futures_mark_price(self, symbol):
        return {"markPrice": str(self._mark), "lastFundingRate": "0.0001"}
    def futures_klines(self, symbol, interval, startTime, limit):
        return [[startTime, "100", "106", "104", str(self._fwd), "1000", startTime + 60000,
                 "100000", 50, "600", "60000", "0"]]


def test_snapshot_records_real_features(tmp_path):
    c = MockClient()
    snap = ft.fetch_orderflow_snapshot(c, "BTCUSDT", "2026-07-01T00:00:00Z", 1000)
    # bid depth 15 vs ask depth 5 -> positive imbalance
    assert snap["ob_imbalance"] > 0
    assert snap["mark_price"] == 100.0
    assert snap["can_place_live_orders"] is False


def test_tag_only_matured_horizons(tmp_path):
    sp = tmp_path / "snap.jsonl"; lp = tmp_path / "lab.jsonl"
    c = MockClient(mark=100.0, fwd=105.0)
    # record a snapshot at ts=0
    ft.record_snapshots(c, ["BTCUSDT"], "2026-07-01T00:00:00Z", 0, snapshots_path=sp)
    # now = +20min -> only the 15m horizon is matured, not 60m/240m
    n = ft.tag_matured_returns(c, 20 * 60_000, snapshots_path=sp, labeled_path=lp)
    assert n == 1
    rows = ft._load_jsonl(lp)
    assert rows[0]["horizon_min"] == 15
    assert abs(rows[0]["return"] - 0.05) < 1e-6   # (105-100)/100

    # advancing to +5h matures the remaining horizons (no double-label of 15m)
    n2 = ft.tag_matured_returns(c, 5 * 3600_000, snapshots_path=sp, labeled_path=lp)
    assert n2 == 2  # 60m + 240m now
    horizons = sorted(r["horizon_min"] for r in ft._load_jsonl(lp))
    assert horizons == [15, 60, 240]


def test_summary_reports_insufficient_sample(tmp_path):
    lp = tmp_path / "lab.jsonl"
    with open(lp, "w", encoding="utf-8") as fh:
        import json
        for i in range(10):
            fh.write(json.dumps({"ts_ms": i, "symbol": "BTCUSDT", "horizon_min": 15,
                                 "ob_imbalance": 0.2, "return": 0.01}) + "\n")
    s = ft.summarize(labeled_path=lp)
    assert s["total_labeled"] == 10
    assert s["verdict"] == "insufficient_data_forward_test_still_accruing"
    assert "insufficient_sample" in s["buckets"]["h15_bid_heavy"]["status"]
