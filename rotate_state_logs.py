"""ROTATE STATE LOGS — one-shot tail-trim for ROLLING streams/caches (gap #14).

whale_flow_history hit 552MB + candle caches ~100MB/coin on a 16GB machine whose
full-file readers (shadow_trigger_eval) already died of MemoryError once. This trims
ONLY rebuildable rolling data, tail-kept on line boundaries, atomic (tmp+replace).

HARD RULES:
- NEVER touch ground truth: brain.db, state/memory/*, any closed.jsonl, positions,
  pending, governance, ledgers, heartbeats. Whitelist-only — nothing is discovered
  dynamically except the candle-cache glob.
- Writers append via open-append-close; a row landing between tail-read and replace
  is lost from a MARKET STREAM (acceptable). If the file is momentarily locked
  (Windows sharing violation) -> skip, next run catches it.
Run: venv python rotate_state_logs.py  (fleet_watchdog wires it with a 12h throttle).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AM = ROOT / "state" / "agent_memory"
LOG = ROOT / "state" / "rotation_log.jsonl"

# (path, cap_bytes, keep_tail_bytes)
TARGETS: list[tuple[Path, int, int]] = [
    (AM / "whale_flow_history.jsonl",           80 * 2**20, 40 * 2**20),
    (AM / "microstructure_flow_history.jsonl",  80 * 2**20, 40 * 2**20),
    (AM / "whale_flow_events.jsonl",           120 * 2**20, 60 * 2**20),
    (AM / "paper_candidate_feeder_history.jsonl", 60 * 2**20, 30 * 2**20),
]
CANDLE_CAP, CANDLE_KEEP = 20 * 2**20, 10 * 2**20   # per-coin candle caches (rebuildable)


def _trim(p: Path, cap: int, keep: int) -> dict | None:
    try:
        size = p.stat().st_size
    except OSError:
        return None
    if size <= cap:
        return None
    try:
        with p.open("rb") as f:
            f.seek(max(0, size - keep))
            tail = f.read()
        nl = tail.find(b"\n")                      # drop the partial first line
        tail = tail[nl + 1:] if nl >= 0 else tail
        tmp = p.with_suffix(p.suffix + ".rot")
        tmp.write_bytes(tail)
        os.replace(tmp, p)
        return {"file": str(p.relative_to(ROOT)), "was_mb": round(size / 2**20, 1),
                "now_mb": round(len(tail) / 2**20, 1)}
    except OSError as e:                           # locked by a writer -> next run
        return {"file": str(p.relative_to(ROOT)), "skip": repr(e)[:80]}


def main() -> None:
    done = []
    for p, cap, keep in TARGETS:
        r = _trim(p, cap, keep)
        if r:
            done.append(r)
    candles = ROOT / "state" / "chart" / "candles"
    if candles.exists():
        for coin in candles.iterdir():
            if coin.is_dir():
                for f in coin.glob("*.jsonl"):
                    r = _trim(f, CANDLE_CAP, CANDLE_KEEP)
                    if r:
                        done.append(r)
    rec = {"ts": int(time.time() * 1000), "trimmed": len(done), "detail": done[:40]}
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(json.dumps({"trimmed": len(done),
                      "freed_mb": round(sum((d.get("was_mb", 0) - d.get("now_mb", 0))
                                            for d in done if "now_mb" in d), 1)}))


if __name__ == "__main__":
    main()
