"""Backup brain.db + events.jsonl to C: (gap #7, 2026-07-14).

The ground-truth (648+ trials, the append-only hash-chain, trade autopsies) lives ONLY on
E: = a USB HDD. A cable pop / drive sleep mid-write = the evidence is gone and NOT replayable.
This copies it to C:/keo-brain-backups on a rolling schedule via Windows Task Scheduler (NO
resident daemon — the machine is RAM-starved). Uses sqlite's online .backup() so a concurrent
writer can't produce a torn copy. Keeps the last N snapshots.

Run: python backup_brain.py   (wired to Task Scheduler every ~12h + at logon).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_DB = ROOT / "state" / "memory" / "brain.db"
SRC_EVENTS = ROOT / "state" / "memory" / "events.jsonl"
DEST = Path("C:/keo-brain-backups")          # C: (internal SSD) — survives an E: USB drop
KEEP = 8                                      # rolling snapshots (~4 days at 12h cadence)


def _stamp() -> str:
    # avoid Date.now-style nondeterminism concerns — plain wall clock is fine for a filename
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def backup_db(dest_db: Path) -> bool:
    """Online sqlite backup (torn-write safe even while agents write)."""
    try:
        src = sqlite3.connect(f"file:{SRC_DB.as_posix()}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(str(dest_db))
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        return True
    except Exception as e:
        # fallback: plain file copy (better a possibly-torn copy than none on a USB-drop)
        try:
            shutil.copy2(SRC_DB, dest_db)
            return True
        except Exception:
            print(json.dumps({"backup_db_error": repr(e)[:200]}))
            return False


def prune(pattern: str) -> None:
    snaps = sorted(DEST.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in snaps[KEEP:]:
        try:
            old.unlink()
        except Exception:
            pass


def main() -> dict:
    DEST.mkdir(parents=True, exist_ok=True)
    ts = _stamp()
    res = {"ts": ts, "dest": str(DEST)}
    if SRC_DB.exists():
        db_dest = DEST / f"brain_{ts}.db"
        res["db_ok"] = backup_db(db_dest)
        res["db_bytes"] = db_dest.stat().st_size if db_dest.exists() else 0
        prune("brain_*.db")
    else:
        res["db_ok"] = False; res["db_missing"] = True
    if SRC_EVENTS.exists():
        try:
            ev_dest = DEST / f"events_{ts}.jsonl"
            shutil.copy2(SRC_EVENTS, ev_dest)
            res["events_ok"] = True
            prune("events_*.jsonl")
        except Exception as e:
            res["events_ok"] = False; res["events_error"] = repr(e)[:120]
    # heartbeat so a stale backup is detectable
    try:
        (DEST / "last_backup.json").write_text(json.dumps(res), encoding="utf-8")
    except Exception:
        pass
    return res


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=True))
