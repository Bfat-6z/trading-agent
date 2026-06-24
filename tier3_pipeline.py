"""Tier 3: Full 17-call pipeline on top 5 from deep_scan."""
from dotenv import load_dotenv
load_dotenv()
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import futures_watch as fw

# Load top 5
with open("state/deep_scan_top5.json") as f:
    top5 = json.load(f)

print(f"\n=== TIER 3: Full pipeline on {len(top5)} finalists ===\n")
results = {}

def run_one(c):
    sym = c["symbol"]
    print(f"  [{sym}] starting pipeline...", flush=True)
    t0 = time.time()
    try:
        r = fw.run_pipeline(sym)
        dt = time.time() - t0
        if r is None:
            return sym, None, dt
        return sym, r, dt
    except Exception as e:
        return sym, {"error": f"{type(e).__name__}: {e}"}, time.time() - t0

with ThreadPoolExecutor(max_workers=3) as ex:
    futs = {ex.submit(run_one, c): c for c in top5}
    for fut in as_completed(futs):
        sym, r, dt = fut.result(timeout=600)
        results[sym] = r
        if r is None:
            print(f"  [{sym}] [{dt:.0f}s] FAILED snapshot/pipeline")
        elif "error" in r:
            print(f"  [{sym}] [{dt:.0f}s] ERROR: {r['error']}")
        else:
            d = r["debate"]
            rk = r["risk"]
            print(f"  [{sym}] [{dt:.0f}s] debate={d.consensus} ({d.consensus_strength:.2f})  "
                  f"risk={rk.recommendation} (score {rk.risk_score:.1f})")

print("\n\n=== FINAL VERDICT TABLE ===\n")
for c in top5:
    sym = c["symbol"]
    r = results.get(sym)
    if not r or "error" in r:
        print(f"  {sym:14s} -- FAILED --")
        continue
    d = r["debate"]
    rk = r["risk"]
    # Count analyst verdicts
    n_bull = sum(1 for a in r["analysts"] if a.verdict == "bullish")
    n_bear = sum(1 for a in r["analysts"] if a.verdict == "bearish")
    n_neut = sum(1 for a in r["analysts"] if a.verdict == "neutral")
    tv_a = next((a for a in r["analysts"] if a.agent_name == "tv_technicals_analyst"), None)
    tv_v = tv_a.verdict if tv_a else "n/a"
    tv_c = tv_a.confidence if tv_a else 0
    action, lev = fw.decide_action(d, rk)
    print(f"  {sym:14s} setup={c['setup']:30s}  ch24={c['ch24']:+6.2f}%")
    print(f"    analysts:  bull={n_bull}  bear={n_bear}  neutral={n_neut}  TV={tv_v}({tv_c:.2f})")
    print(f"    debate:    {d.consensus:8s} strength={d.consensus_strength:.2f}")
    print(f"    risk:      {rk.recommendation:12s} score={rk.risk_score:.1f}")
    print(f"    decision:  {action or 'NO_TRADE'} @ {lev}x" if action else f"    decision:  NO_TRADE")
    if hasattr(d, "agent_name"):  # backward compat field name
        pass
    print()

# Save full results
out = {}
for sym, r in results.items():
    if not r or "error" in r:
        out[sym] = {"status": "failed", "error": r.get("error") if r else "no result"}
        continue
    out[sym] = {
        "debate_consensus": r["debate"].consensus,
        "debate_strength": r["debate"].consensus_strength,
        "risk_rec": r["risk"].recommendation,
        "risk_score": r["risk"].risk_score,
        "analysts": [{"name": a.agent_name, "verdict": a.verdict,
                       "conf": a.confidence, "key_points": a.key_points[:3]}
                      for a in r["analysts"]],
    }
with open("state/tier3_results.json", "w") as f:
    json.dump(out, f, indent=2)
print("Full results saved to state/tier3_results.json")
