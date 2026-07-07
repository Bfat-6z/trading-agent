"""METHOD MATRIX — decision-time comparison of EVERY method on the CURRENT setup.

Owner goal (/goal): "thay vì kill những phương pháp tệ thì biến thành notes, xong
trước khi trade dùng nhiều pp đó áp thử lên xem cái nào ra winrate cao nhất kiểu so
sánh". The arena leaderboard compares only the 2-4 ARMED methods' career stats; this
is the richer thing the owner asked for — it applies ALL ~168 known method
definitions (seeds + full pool + armed, dead ideas included) to the live bar and
answers, per setup RIGHT NOW: which methods fire, and which of them has the best
historical track record.

Two layers, both rebuilt each cycle:
  1. LEADERBOARD — every method backtested on ONE shared recent window across the
     liquid universe -> winrate, avgR, expectancy, n. The honest ranking is by
     expectancy (a 6:1 method wins 30% and is still +EV), winrate shown alongside.
  2. LIVE MATRIX — for each coin whose latest bar fires >=1 method, the firing
     methods ranked by their leaderboard stats: "on ZEC now, {A 46%, B 33%} agree,
     A has the better history." Pure DECISION SUPPORT — places no orders, touches
     no account. Paper/offline like everything else; live stays LOCKED.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault("INGEST_DECISION_CANDLES", "0")

import method_lab as ml
from method_seeds import SEED_METHODS

ROOT = Path(__file__).resolve().parent
OUT_JSON = ROOT / "state" / "memory" / "method_matrix.json"
STATS_CACHE = ROOT / "state" / "memory" / "method_matrix_stats.json"
VAULT = ROOT / "vault" / "auto" / "method-matrix.md"
HB = ROOT / "state" / "method_matrix_heartbeat.json"

TF = "15m"
MIN_QVOL = 50_000_000.0
UNIV_N = 30
STATS_TTL = 3 * 3600           # rebuild the (expensive) backtest leaderboard at most every 3h
STATS_MONTHS = 1.0             # history window for the winrate estimate
LIVE_MONTHS = 0.12             # short pull is enough to fire the latest bar

# Gate constants SHARED with llm_trader via the SAME env vars + defaults (Codex #5:
# don't hardcode a second copy that can drift from the executor's real gate). If the
# owner overrides MECH_LEV/MECH_GAP_LIQ_ATR_MULT, both this matrix's advisory flag and
# the executor's actual veto move together.
MECH_LEV = int(os.environ.get("MECH_LEV", "10"))
GAP_LIQ_ATR_MULT = float(os.environ.get("MECH_GAP_LIQ_ATR_MULT", "3.0"))
# Core features every method reads; if any is non-finite on the latest bar the frame
# is degenerate (kline gap / warmup) and we must NOT eval method_fires on it (Codex #4:
# the live latest row has none of backtest_method's i>=200 warmup protection).
_CORE_FEATS = ("close", "rsi14", "atr_pct", "vol_ratio", "ret20")


def _row_ok(row: dict) -> bool:
    import math
    for f in _CORE_FEATS:
        v = row.get(f)
        if v is None or not math.isfinite(float(v)):
            return False
    return True


def load_all_methods() -> list[dict]:
    """Every evaluable method definition, deduped by id: seeds + full pool + armed.
    The pool holds the tested-and-shelved ideas (the 'notes' the owner wanted kept
    alive to compare against), so a dead method still competes here."""
    by_id: dict[str, dict] = {m["id"]: m for m in SEED_METHODS}
    pool = ROOT / "state" / "method_lab" / "methods_pool.jsonl"
    if pool.exists():
        for line in pool.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    m = json.loads(line)
                    by_id[m["id"]] = m
                except Exception:
                    pass
    armed = ROOT / "state" / "method_lab" / "armed_methods.json"
    if armed.exists():
        try:
            d = json.loads(armed.read_text(encoding="utf-8"))
            for m in (d if isinstance(d, list) else d.get("methods", [])):
                by_id[m["id"]] = {**by_id.get(m["id"], {}), **m}
        except Exception:
            pass
    return list(by_id.values())


def _frames(client, months: float) -> dict[str, tuple]:
    import orderflow_data as of
    now = int(time.time() * 1000)
    ticks = client.futures_ticker()
    syms = [t["symbol"] for t in sorted(
        [x for x in ticks if x.get("symbol", "").endswith("USDT") and "_" not in x["symbol"]
         and float(x.get("quoteVolume", 0) or 0) >= MIN_QVOL],
        key=lambda x: -float(x.get("quoteVolume", 0) or 0))[:UNIV_N]]
    frames = {}
    for s in syms:
        try:
            bars = [b for b in of.fetch_klines_with_flow(s, TF, months=months, end_ms=now,
                                                         client=client, sleep_between=0.02)
                    if b.get("is_final", True)]
            if len(bars) >= 220:
                frames[s] = (bars, ml.feature_frame(bars))
        except Exception:
            continue
    return frames


def build_stats(client) -> dict:
    """Backtest every method across the shared universe -> career stats. Cached."""
    methods = load_all_methods()
    frames = _frames(client, STATS_MONTHS)
    stats = {}
    for m in methods:
        mid = m.get("id")
        if not mid:
            continue
        wins = n = 0
        net_sum = r_sum = 0.0
        for sym, (_bars, rows) in frames.items():
            for t in ml.backtest_method(rows, m, sym):
                n += 1
                net_sum += t["net"]
                r_sum += t["r"]
                if t["net"] > 0:
                    wins += 1
        if n:
            stats[mid] = {"n": n, "win": round(wins / n, 3), "avg_r": round(r_sum / n, 3),
                          "exp_net": round(net_sum / n * 100, 4), "side": m.get("side", "LONG"),
                          "family": m.get("family"), "sl": m.get("sl_pct"), "tp": m.get("tp_pct")}
    payload = {"ts": int(time.time() * 1000), "universe": len(frames), "months": STATS_MONTHS,
               "methods": stats}
    STATS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    STATS_CACHE.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def load_stats(client) -> dict:
    try:
        p = json.loads(STATS_CACHE.read_text(encoding="utf-8"))
        if (time.time() * 1000 - p.get("ts", 0)) / 1000 < STATS_TTL and p.get("methods"):
            return p
    except Exception:
        pass
    return build_stats(client)


def live_matrix(client, stats: dict) -> dict:
    """For each coin's LATEST bar: which methods fire, ranked by their career stats."""
    methods = load_all_methods()
    frames = _frames(client, LIVE_MONTHS)
    smeth = stats.get("methods", {})
    signals = []                                     # one row per (coin, firing method)
    per_coin: dict[str, list] = {}
    for sym, (_bars, rows) in frames.items():
        row = rows[-1]
        if not _row_ok(row):                         # degenerate latest bar -> skip coin (Codex #4)
            continue
        for m in methods:
            mid = m.get("id")
            try:
                if not ml.method_fires(row, m):
                    continue
            except Exception:
                continue
            st = smeth.get(mid) or {}
            # gate awareness (not a block — this is analysis): flag if the live gap
            # gate would refuse this fire (atr too high for the stop) so the matrix
            # doesn't recommend a setup the executor will veto. Same constants as the
            # executor (Codex #5) so the advisory flag can't drift from the real veto.
            atr = float(row.get("atr_pct") or 0.0)
            # fail-closed parity with the executor: gate_ok only when atr is present AND
            # within the liquidation band (missing atr => executor now refuses => not ok).
            gate_ok = bool(atr > 0 and atr * GAP_LIQ_ATR_MULT <= 100.0 / max(1, MECH_LEV))
            sig = {"coin": sym, "method": mid, "side": m.get("side", "LONG"),
                   "win": st.get("win"), "avg_r": st.get("avg_r"), "exp_net": st.get("exp_net"),
                   "n": st.get("n"), "atr_pct": round(atr, 2), "gate_ok": gate_ok,
                   "rsi": row.get("rsi14"), "vol_ratio": row.get("vol_ratio")}
            signals.append(sig)
            per_coin.setdefault(sym, []).append(sig)
    # rank each coin's firing methods by expectancy (winrate shown alongside)
    for sym in per_coin:
        per_coin[sym].sort(key=lambda s: (s["exp_net"] if s["exp_net"] is not None else -9), reverse=True)
    signals.sort(key=lambda s: (s["exp_net"] if s["exp_net"] is not None else -9), reverse=True)
    return {"ts": int(time.time() * 1000), "signals": signals, "per_coin": per_coin,
            "coins_live": len(per_coin)}


def render(stats: dict, live: dict) -> None:
    sm = stats.get("methods", {})
    # leaderboard: rank by expectancy, need a minimum sample
    board = sorted([{"id": k, **v} for k, v in sm.items() if v.get("n", 0) >= 30],
                   key=lambda x: x["exp_net"], reverse=True)
    b = ("> ⚙️ AUTO-RENDERED — do not edit (method_matrix.render). Decision-support only.\n\n"
         "# 🔢 METHOD MATRIX — áp mọi phương pháp lên setup hiện tại\n\n"
         f"Backtest {len(sm)} method trên {stats.get('universe')} coin liquid "
         f"({stats.get('months')} tháng 15m). Xếp theo **expectancy** (winrate cao mà "
         "payoff bé vẫn có thể âm EV); winrate hiển thị kèm.\n\n")
    # LIVE signals first — that's the decision-time answer
    b += "## ⚡ Tín hiệu LIVE ngay bây giờ (method nào fire trên setup nào)\n\n"
    if live.get("per_coin"):
        b += "| coin | method | side | winrate | avgR | exp_net% | n | gate | rsi | vol |\n|---|---|---|---|---|---|---|---|---|---|\n"
        for sym, sigs in sorted(live["per_coin"].items(),
                                key=lambda kv: (kv[1][0]["exp_net"] if kv[1] and kv[1][0]["exp_net"] is not None else -9),
                                reverse=True):
            for s in sigs[:6]:
                g = "✓" if s["gate_ok"] else "⛔atr"
                b += (f"| {sym} | [[auto/methods/{s['method']}\\|{s['method']}]] | {s['side']} | "
                      f"{s['win']} | {s['avg_r']} | {s['exp_net']} | {s['n']} | {g} | {s['rsi']} | {s['vol_ratio']} |\n")
    else:
        b += "_Không coin nào fire method nào ở bar hiện tại (chợ chưa vào vùng setup)._\n"
    # LEADERBOARD
    b += "\n## 🏆 Leaderboard toàn bộ method (career backtest)\n\n"
    b += "| # | method | side | winrate | avgR | exp_net% | n | fam |\n|---|---|---|---|---|---|---|---|\n"
    for i, m in enumerate(board[:40], 1):
        b += (f"| {i} | [[auto/methods/{m['id']}\\|{m['id']}]] | {m['side']} | **{m['win']}** | "
              f"{m['avg_r']} | {m['exp_net']} | {m['n']} | {m.get('family') or '?'} |\n")
    b += (f"\n_{len(board)} method có n≥30. Luật: expectancy>0 mới đáng cân nhắc; "
          "winrate cao + payoff cao + n lớn = tin cậy. Đây chỉ là GỢI Ý, executor vẫn "
          "chỉ bắn method ARMED đã qua lockbox._\n")
    VAULT.parent.mkdir(parents=True, exist_ok=True)
    VAULT.write_text(b, encoding="utf-8")
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({"stats_ts": stats.get("ts"), "live": live,
                                    "leaderboard": board[:40]}), encoding="utf-8")


def run_once(client) -> dict:
    stats = load_stats(client)
    live = live_matrix(client, stats)
    render(stats, live)
    HB.write_text(json.dumps({"agent": "method_matrix", "pid": os.getpid(),
                              "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "status": "running", "coins_live": live["coins_live"],
                              "methods": len(stats.get("methods", {}))}), encoding="utf-8")
    return {"coins_live": live["coins_live"], "methods": len(stats.get("methods", {})),
            "top_live": live["signals"][0] if live["signals"] else None}


def main():
    ap = argparse.ArgumentParser(description="method matrix (decision support, paper-only)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=600.0)
    ap.add_argument("--rebuild-stats", action="store_true")
    args = ap.parse_args()
    pid_f = ROOT / "state" / "method_matrix.pid"
    try:
        from forward_test import _pid_alive
        old = int(pid_f.read_text(encoding="utf-8").strip())
        if old and old != os.getpid() and _pid_alive(old):
            print(json.dumps({"exit": "another method_matrix alive", "pid": old})); return
    except Exception:
        pass
    pid_f.write_text(str(os.getpid()), encoding="utf-8")
    from tradingagents.binance.client import spot_client
    client = spot_client()
    if args.rebuild_stats:
        build_stats(client)
    while True:
        try:
            print(json.dumps(run_once(client)))
        except Exception as e:
            print(json.dumps({"error": repr(e)[:160]}))
        if args.once:
            break
        time.sleep(max(120.0, args.interval))


if __name__ == "__main__":
    main()
