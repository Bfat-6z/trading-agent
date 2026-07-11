"""BACKFILL flush_oi_dn / flush_no_oi over history (owner: live measurement too slow — flushes are
rare, so waiting for n>=25 takes days). The flush trigger is deterministic, so we replay it over ~20d
of past bars and grab every flush that already happened -> dozens of samples in one batch.

Matches the LIVE trigger exactly (llm_trader_triggers): flush = ret5 <= -3.0% AND vol_ratio >= 2.0,
where vol_ratio = volume / SMA(volume,20) (backtest_chart_signal VOL_MA=20) and ret5 = 5-bar % change.
Split by OI slope over the prior 8h: <= -1.0% -> flush_oi_dn, else flush_no_oi. Exit + dedup + costs
are shadow_trigger_eval's (same simulate, same episode key) so backfill samples are directly comparable
to live ones and land in the SAME ledger. No-lookahead: trigger/OI use bars <= i, outcome uses bars > i.

Rate-limited (recent Binance IP ban): sleeps between coins, modest universe/window. src='backfill' tag.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import orderflow_data as of
import shadow_trigger_eval as ste

# curated liquid crypto majors/alts (excludes tokenized stocks; enough breadth for many flushes)
UNIVERSE = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
            "LINKUSDT", "SUIUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "TIAUSDT",
            "SEIUSDT", "LTCUSDT", "AAVEUSDT", "UNIUSDT", "FILUSDT", "DOTUSDT", "ATOMUSDT", "WLDUSDT"]

TF = "15m"
TF_MS = of._TF_MS[TF]
DAYS = 20
T_FLUSH_RET5 = -3.0
T_FLUSH_VOL = 2.0
T_OI_DECL = -1.0
VOL_MA = 20
SLEEP_COIN = 2.0            # gap between coins -> stay under Binance weight limit (post-ban caution)


def _oi_slope_at(deriv: dict, ts_ms: int) -> float | None:
    """% change of OI over the ~8h ending at ts_ms, from the point-in-time deriv series (no lookahead)."""
    pts = sorted((int(t), float(v.get("oi"))) for t, v in deriv.items()
                 if isinstance(v, dict) and v.get("oi") is not None and int(t) <= ts_ms)
    if len(pts) < 3:
        return None
    window = [oi for t, oi in pts if t >= ts_ms - 8 * 3_600_000]
    if len(window) < 3 or window[0] <= 0:
        return None
    return (window[-1] - window[0]) / window[0] * 100


def backfill_once(client, now_ms: int) -> dict:
    done = ste._load_done_keys()
    added = {"flush_oi_dn": 0, "flush_no_oi": 0}
    n_coins = 0
    for sym in UNIVERSE:
        if ste._base(sym) in ste.NON_CRYPTO:
            continue
        try:
            end = now_ms
            # with_deriv=True so each bar carries point-in-time OI; also fetch the raw deriv series
            months = DAYS / 30.0 + 0.05
            bars = of.fetch_klines_with_flow(sym, TF, months=months, end_ms=end,
                                             client=client, sleep_between=0.03, with_deriv=False)
            if len(bars) < VOL_MA + 30:
                continue
            deriv = of.fetch_deriv_series(sym, "1h", start_ms=now_ms - (DAYS + 1) * 86_400_000, end_ms=now_ms)
            closes = [float(b["close"]) for b in bars]
            vols = [float(b.get("volume", 0.0) or 0.0) for b in bars]
            n_coins += 1
            # scan for flushes; stop early enough that a full 24-bar outcome exists after entry
            for i in range(VOL_MA, len(bars) - (ste.MAX_HOLD + 2)):
                ret5 = (closes[i] / closes[i - 5] - 1) * 100 if closes[i - 5] > 0 else 0.0
                vma = sum(vols[i - VOL_MA + 1:i + 1]) / VOL_MA
                vr = vols[i] / vma if vma > 0 else 0.0
                if not (ret5 <= T_FLUSH_RET5 and vr >= T_FLUSH_VOL):
                    continue
                ts = int(bars[i]["ts_ms"])                  # close time of the flush bar
                slope = _oi_slope_at(deriv, ts)
                path = "flush_oi_dn" if (slope is not None and slope <= T_OI_DECL) else "flush_no_oi"
                key = f"{sym}|{path}|{ts // ste.EPISODE_MS}"
                if key in done:
                    continue
                sim = ste._simulate(bars, i, "LONG")        # flush hypothesis = LONG the bounce
                if sim is None:
                    continue
                rec = {"key": key, "sym": sym, "path": path, "side": "LONG", "trigger_ts": ts,
                       "eval_ts": now_ms, "src": "backfill", "oi_slope_pct": slope, **sim}
                with ste.SHADOW.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=True) + "\n")
                done.add(key)
                added[path] += 1
            time.sleep(SLEEP_COIN)
        except Exception as e:
            # a ban surfaces here as an exception -> stop early, keep what we have (don't hammer)
            if "-1003" in str(e) or "banned" in str(e).lower():
                return {"coins_done": n_coins, "added": added, "STOPPED": "IP ban — dung lai"}
            continue
    return {"coins_done": n_coins, "added": added}


if __name__ == "__main__":
    from tradingagents.binance.client import spot_client
    res = backfill_once(spot_client(), int(time.time() * 1000))
    print(json.dumps(res, ensure_ascii=True))
    print(json.dumps(ste.report().get("by_path", {}).get("flush_oi_dn", {}), ensure_ascii=True))
    print(json.dumps(ste.report().get("by_path", {}).get("flush_no_oi", {}), ensure_ascii=True))
