def test_klines_cache_and_backoff():
    import sys, time, shutil
    sys.path.insert(0, r"E:\keo-moi-mail\trading-agent")
    import orderflow_data as of
    
    # clean cache dir for a deterministic test
    if of._CACHE_DIR.exists():
        shutil.rmtree(of._CACHE_DIR, ignore_errors=True)
    
    TF_MS = of._TF_MS["15m"]
    NOW = 1784_000_000_000
    NOW = (NOW // TF_MS) * TF_MS + 5000   # 5s into a bar -> +100ms stays in the SAME bar
    
    class MockClient:
        def __init__(self):
            self.calls = 0
        def futures_klines(self, symbol, interval, startTime, limit):
            self.calls += 1
            tf = of._TF_MS[interval]
            rows = []
            # emit ~200 closed bars ending before NOW
            t = (startTime // tf) * tf
            while t + tf - 1 < NOW and len(rows) < limit:
                ct = t + tf - 1
                rows.append([t, "100", "101", "99", "100.5", "10", ct, "1000", 5, "6", "600", "0"])
                t += tf
            return rows
    
    # --- 1. round-trip contract: cache stores + returns exact bar dicts, downstream enrich OK ---
    c = MockClient()
    b1 = of.fetch_klines_with_flow("BTCUSDT", "15m", months=0.12, end_ms=NOW, client=c, with_deriv=False)
    assert b1, "empty fetch"
    assert isinstance(b1[0]["ts_ms"], int), "ts_ms not int"
    for k in ("open_time", "close_time", "open", "high", "low", "close", "volume",
              "quote_volume", "taker_buy_base", "is_final"):
        assert k in b1[0], "missing key " + k
    print("1 contract keys OK, n=%d, client_calls=%d" % (len(b1), c.calls))
    
    # downstream: compute_indicators -> enrich_indicator_df (the fail-closed ts_ms join) must not raise
    import backtest_chart_signal as cs
    ind = cs.compute_indicators(b1)
    enr = of.enrich_indicator_df(ind, b1, [])
    print("2 downstream enrich OK, rows=%d" % len(enr))
    
    # --- 3. cache collapse: 2nd fetch same bar = 0 new client calls; 0.02 shares 0.12's cache ---
    b2 = of.fetch_klines_with_flow("BTCUSDT", "15m", months=0.12, end_ms=NOW + 100, client=c, with_deriv=False)
    assert c.calls == 1, "cache miss on same bar! calls=%d" % c.calls
    b3 = of.fetch_klines_with_flow("BTCUSDT", "15m", months=0.02, end_ms=NOW, client=c, with_deriv=False)
    assert c.calls == 1, "0.02 did not share 0.12 cache! calls=%d" % c.calls
    assert len(b3) <= len(b1), "strict slice failed: 0.02 window should be <= 0.12"
    print("3 cache-collapse OK: 3 fetches, 1 client call; 0.02 sliced to n=%d (0.12 n=%d)" % (len(b3), len(b1)))
    
    # --- 4. next bar -> exactly one refetch ---
    b4 = of.fetch_klines_with_flow("BTCUSDT", "15m", months=0.12, end_ms=NOW + TF_MS, client=c, with_deriv=False)
    assert c.calls == 2, "next bar did not refetch! calls=%d" % c.calls
    print("4 bar-roll refetch OK: calls=%d" % c.calls)
    
    # --- 5. backtest bypass: months=5 never touches cache ---
    c2 = MockClient()
    _ = of.fetch_klines_with_flow("ETHUSDT", "15m", months=5.0, end_ms=NOW, client=c2, with_deriv=False)
    assert not (of._CACHE_DIR / ("ETHUSDT_15m_5" )).exists()
    assert c2.calls >= 1, "backtest should fetch direct"
    print("5 backtest bypass OK (no cache file), calls=%d" % c2.calls)
    
    # --- 6. backoff fail-CLOSED: record a ban -> wrapper returns [] WITHOUT calling client ---
    class Ban(Exception):
        status_code = 418
        code = -1003
        retry_after = None
    of._record_klines_ban(Ban("banned"))
    assert of._klines_backoff_active(), "backoff not active after record"
    c3 = MockClient()
    r = of.fetch_klines_with_flow("SOLUSDT", "15m", months=0.12, end_ms=NOW, client=c3, with_deriv=False)
    assert r == [] and c3.calls == 0, "ban gate leaked! r=%d calls=%d" % (len(r), c3.calls)
    # also applies to backtest windows
    r2 = of.fetch_klines_with_flow("SOLUSDT", "15m", months=5.0, end_ms=NOW, client=c3, with_deriv=False)
    assert r2 == [] and c3.calls == 0, "ban gate leaked on backtest"
    print("6 backoff fail-CLOSED OK: banned -> [] + 0 client calls (hot AND backtest)")
    
    # clear backoff, confirm resumes
    import json
    of._BACKOFF_FILE.write_text(json.dumps({"backoff_until_epoch": 0}), encoding="utf-8")
    assert not of._klines_backoff_active()
    r3 = of.fetch_klines_with_flow("SOLUSDT", "15m", months=0.12, end_ms=NOW, client=c3, with_deriv=False)
    assert r3 and c3.calls == 1, "did not resume after backoff expired"
    print("7 resume-after-backoff OK")
    
    # --- 8. fail-open: corrupt cache file -> falls back to direct fetch ---
    key = of._bar_cache_key("XRPUSDT", "15m", 0.12, NOW, False)
    of._CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (of._CACHE_DIR / (key + ".json")).write_text("{corrupt", encoding="utf-8")
    c4 = MockClient()
    r4 = of.fetch_klines_with_flow("XRPUSDT", "15m", months=0.12, end_ms=NOW, client=c4, with_deriv=False)
    assert r4 and c4.calls == 1, "corrupt cache did not fail-open! calls=%d" % c4.calls
    print("8 fail-open on corrupt cache OK")
    
    # --- 9. Codex #4: a ban raised on the BACKTEST (months=5) path is RECORDED to _backoff.json ---
    of._BACKOFF_FILE.unlink(missing_ok=True) if of._BACKOFF_FILE.exists() else None
    class BX(of._BinanceAPIException):
        def __init__(self):
            self.status_code = 418
            self.code = -1003
            self.retry_after = None
    class BanClient:
        def futures_klines(self, **kw):
            raise BX()
    try:
        of.fetch_klines_with_flow("ADAUSDT", "15m", months=5.0, end_ms=NOW, client=BanClient(), with_deriv=False)
    except Exception:
        pass
    assert of._klines_backoff_active(), "backtest ban did NOT record backoff (Codex #4)"
    print("9 backtest-path ban records backoff OK")
    of._BACKOFF_FILE.write_text('{"backoff_until_epoch":0}', encoding="utf-8")
    
    # --- 10. Codex #5: a 15m sweep must NOT delete a valid CURRENT 1h cache file ---
    of._CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cur15 = NOW // of._TF_MS["15m"]
    cur1h = NOW // of._TF_MS["1h"]
    f_stale15 = of._CACHE_DIR / ("AAAUSDT_15m_0.12_%d_0.json" % (cur15 - 5))     # stale 15m -> should die
    f_cur1h = of._CACHE_DIR / ("AAAUSDT_1h_0.5_%d_0.json" % cur1h)               # current 1h -> must live
    f_stale15.write_text("[]", encoding="utf-8"); f_cur1h.write_text("[]", encoding="utf-8")
    of._sweep_stale_klines("15m", NOW)
    assert not f_stale15.exists(), "stale 15m not swept"
    assert f_cur1h.exists(), "sweep wrongly deleted current 1h file (Codex #5)"
    print("10 sweep respects timeframe OK")
    
    shutil.rmtree(of._CACHE_DIR, ignore_errors=True)
    print("ALL PASS")
