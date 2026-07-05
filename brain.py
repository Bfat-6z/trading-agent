"""Second brain — deterministic trials/DSR registry (P2).

Ground-truth memory for the research pipeline. Design (15-agent blueprint in
plans/second_brain_design.md, adapted to repo conventions in
plans/second_brain_readiness.md):

  Layer 0  state/memory/events.jsonl — append-only, hash-chained event log.
           Source of truth; every write is one event line.
  Layer 1  state/memory/brain.db (SQLite WAL) — a PROJECTION of the log
           (rebuildable via rebuild_from_events). Append-only triggers on the
           trials table: a dead trial can never be edited away, because
           deleting/updating it would silently un-deflate every future
           Sharpe/p-value (the registry doubles as the Deflated-Sharpe trial
           count — Bailey & López de Prado).

  NO LLM anywhere in this module: writers are called only from deterministic
  pipeline code (deep_validation, forward_test, method_lab_runner, backfill).
  The LLM's only interface is READ (novelty gate feedback quotes rows
  verbatim) plus the quarantined `proposals` table, which no gate consults.

Paper/offline only. No exchange calls, no secrets.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from atomic_state import canonical_json
from method_canonical import method_hash, bucketed_hash

ROOT = Path(__file__).resolve().parent
MEM_DIR = ROOT / "state" / "memory"
DB = MEM_DIR / "brain.db"
EVENTS = MEM_DIR / "events.jsonl"

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS trials (
  trial_id      TEXT PRIMARY KEY,
  novelty_hash  TEXT,                 -- RAW-def identity (gate key — matches what a proposer can emit). NULL only for label-only backfill rows.
  as_traded_hash TEXT,                -- identity WITH the grid-optimal sl/tp/timeout actually tested (joins shadow autopsy; Codex file-review #2)
  bucket_hash   TEXT,
  method_id     TEXT,
  dsl_canonical TEXT,
  side          TEXT,
  universe      TEXT,
  timeframe     TEXT,
  months        REAL,
  oos_n INTEGER, oos_mean_r REAL, oos_win REAL, oos_net_pct REAL,
  pvalue REAL, pvalue_method TEXT,
  lockbox_n INTEGER, lockbox_mean_r REAL, lockbox_net_pct REAL, lockbox_pvalue REAL,
  lockbox_held INTEGER,
  opt_sl REAL, opt_tp REAL, opt_timeout INTEGER,
  verdict TEXT CHECK(verdict IN ('DEAD','LOCKBOX_PASS','PENDING')),
  failure_mode TEXT,
  source TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS trials_hash ON trials(novelty_hash);
CREATE INDEX IF NOT EXISTS trials_mid  ON trials(method_id);
CREATE TRIGGER IF NOT EXISTS trials_no_upd BEFORE UPDATE ON trials
  BEGIN SELECT RAISE(ABORT,'trials is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trials_no_del BEFORE DELETE ON trials
  BEGIN SELECT RAISE(ABORT,'trials is append-only'); END;

CREATE TABLE IF NOT EXISTS method_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  novelty_hash TEXT, method_id TEXT,
  state TEXT CHECK(state IN ('armed','disarmed','shadow','retired')),
  reason TEXT,
  valid_at TEXT NOT NULL,
  invalid_at TEXT
);

CREATE TABLE IF NOT EXISTS trade_autopsy (
  trade_id TEXT PRIMARY KEY,
  src TEXT,                 -- 'shadow' | 'mission'
  method_id TEXT, novelty_hash TEXT,
  symbol TEXT, side TEXT,
  entry REAL, exit_px REAL,
  entry_ts_ms INTEGER, closed_ts_ms INTEGER,
  net REAL, r REAL,
  mae_pct REAL, mfe_pct REAL,
  bars_held INTEGER, exit_reason TEXT,
  entry_feats TEXT,         -- JSON snapshot of the fire-bar feature row (lesson mining input)
  created_at TEXT
);

-- LESSONS: deterministic aggregates over trade_autopsy, recomputed (never
-- hand-written, never LLM-written). A lesson is an EXECUTABLE predicate in the
-- method-DSL condition form; 'active' rows become HARD entry vetoes. Promotion
-- is mechanical (n >= 5, consistently negative) — Reflexion-style causal
-- confabulation from a single trade can never mint a rule.
CREATE TABLE IF NOT EXISTS lessons (
  lesson_id TEXT PRIMARY KEY,     -- stable template key (pre-registered)
  block_side TEXT,                -- 'LONG' | 'SHORT' | 'ANY'
  method_scope TEXT,              -- substring the method_id must contain ('' = all methods)
  conds TEXT NOT NULL,            -- JSON [{feat,op,val},...] evaluated on the fire row
  label TEXT,
  n INTEGER, wins INTEGER, avg_r REAL, avg_net REAL, worst_r REAL,
  eff_n INTEGER,                  -- distinct (symbol, UTC-day) clusters — dedups one market move
  mission_n INTEGER, mission_avg_r REAL,
  shadow_n INTEGER, shadow_avg_r REAL,
  status TEXT CHECK(status IN ('candidate','advisory','active')),
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS proposals (   -- LLM quarantine: zero evidential weight
  id TEXT PRIMARY KEY,
  method_id TEXT, dsl TEXT,
  novelty_hash TEXT, bucket_hash TEXT,
  gate_result TEXT,
  created_at TEXT
);
"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _ulid() -> str:
    return f"{int(time.time() * 1000):x}-{os.urandom(4).hex()}"


def connect(readonly: bool = False) -> sqlite3.Connection:
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    if readonly and DB.exists():
        con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)
    else:
        con = sqlite3.connect(DB, timeout=10)
        con.executescript(_SCHEMA)
        try:                                       # migration: pre-lessons DBs lack this column
            con.execute("ALTER TABLE trade_autopsy ADD COLUMN entry_feats TEXT")
        except sqlite3.OperationalError:
            pass
        # migration: lessons schema v2 (3-tier status + cohort columns). The table is
        # a DERIVED projection (recomputed by mine_lessons) so drop-and-recreate is lossless.
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(lessons)")}
            if cols and "mission_n" not in cols:
                con.execute("DROP TABLE lessons")
                con.executescript(_SCHEMA)
        except sqlite3.OperationalError:
            pass
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# Layer 0: hash-chained event log (source of truth)
#
# Codex review fix: read-head-then-append was racy across the three writer
# processes (forward_test 5-min loop, lab 3h loop, manual deep runs) — two
# writers could share `prev` and fork the chain, making normal operation
# indistinguishable from tampering. Serialized with an exclusive sidecar file
# lock (msvcrt on Windows) around read-head -> append -> write-head; the O(1)
# head file replaces the fixed-4096-byte tail scan (which broke on long lines).
# ---------------------------------------------------------------------------
_LOCK = MEM_DIR / "events.lock"
_HEAD = MEM_DIR / "chain_head.txt"


class _chain_lock:
    def __enter__(self):
        MEM_DIR.mkdir(parents=True, exist_ok=True)
        self.fh = open(_LOCK, "a+b")
        try:
            import msvcrt
            acquired = False
            for _ in range(200):                       # ~10s worst-case wait
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    time.sleep(0.05)
            if not acquired:                           # Codex file-review #1: proceeding
                self.fh.close()                        # unlocked would fork the chain —
                raise TimeoutError("brain events.lock not acquired in 10s")  # fail CLOSED
        except ImportError:
            pass                                        # non-Windows: single-writer assumption
        return self

    def __exit__(self, *exc):
        try:
            import msvcrt
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        self.fh.close()
        return False


def _last_event_hash() -> str:
    try:
        h = _HEAD.read_text(encoding="utf-8").strip()
        if h:
            return h
    except Exception:
        pass
    if not EVENTS.exists():
        return "genesis"
    try:                                                # fallback: full last line
        lines = EVENTS.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if line.strip():
                return json.loads(line).get("h", "genesis")
    except Exception:
        pass
    return "genesis"


def _append_event(kind: str, payload: dict[str, Any]) -> str:
    with _chain_lock():
        prev = _last_event_hash()
        body = {"ts": _now_iso(), "kind": kind, "payload": payload}
        h = hashlib.sha256((prev + canonical_json(body)).encode()).hexdigest()[:32]
        with EVENTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**body, "prev": prev, "h": h}, ensure_ascii=False) + "\n")
        _HEAD.write_text(h, encoding="utf-8")
    return h


# ---------------------------------------------------------------------------
# deterministic verdict mapping (documented, no judgment calls at write time)
# ---------------------------------------------------------------------------
def _fin(x: Any) -> float | None:
    """Finite float or None — NaN/inf in a metric must never drive a verdict
    (Codex review: NaN comparisons are silently False and misclassify)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def verdict_for(row: dict[str, Any]) -> tuple[str, str | None]:
    pv = _fin(row.get("pvalue"))
    if pv is None:
        # distinguish "never produced stats" from "tested but thin sample"
        return "PENDING", ("low_n" if row.get("oos_n") else "unknown_stats")
    # LOCKBOX_PASS needs the flag AND sane supporting stats (defense in depth —
    # a truthy flag on a malformed/legacy row must not mint a winner)
    if (row.get("lockbox_held") and (row.get("lockbox_n") or 0) >= 30
            and (_fin(row.get("lockbox_mean_r")) or 0) > 0):
        return "LOCKBOX_PASS", None
    if (_fin(row.get("oos_mean_r")) or 0) <= 0 or (_fin(row.get("oos_net_pct")) or 0) <= 0:
        return "DEAD", "died_oos"
    if (row.get("lockbox_n") or 0) >= 30:
        return "DEAD", "died_lockbox"
    return "PENDING", "low_n"


# ---------------------------------------------------------------------------
# writers (deterministic pipeline code only)
# ---------------------------------------------------------------------------
def record_trials(rows: list[dict[str, Any]], defs: dict[str, dict[str, Any]],
                  source: str, universe: str, timeframe: str, months: float) -> int:
    """One trials row per validation result row. defs maps method_id -> DSL def
    (for canonical hash); missing def -> label-only row (hash NULL, still counts
    toward the DSR trial total)."""
    con = connect()
    n = 0
    try:
        with con:
            for r in rows:
                d = defs.get(r.get("id"))
                nh = method_hash(d) if d else None
                bh = bucketed_hash(d) if d else None
                # as-traded identity: what the grid ACTUALLY tested (opt sl/tp/timeout
                # override the def) — distinct from the raw-def gate key (Codex #2).
                ath = None
                if d:
                    eff = {**d}
                    if r.get("opt_sl") is not None:
                        eff["sl_pct"] = r["opt_sl"]
                    if r.get("opt_tp") is not None:
                        eff["tp_pct"] = r["opt_tp"]
                    if r.get("opt_timeout") is not None:
                        eff["timeout"] = r["opt_timeout"]
                    ath = method_hash(eff)
                verdict, fmode = verdict_for(r)
                rec = {
                    "trial_id": _ulid(), "novelty_hash": nh, "as_traded_hash": ath, "bucket_hash": bh,
                    "method_id": r.get("id"),
                    "dsl_canonical": canonical_json(d) if d else None,
                    "side": r.get("side") or (d or {}).get("side"),
                    "universe": universe, "timeframe": timeframe, "months": months,
                    "oos_n": r.get("oos_n"), "oos_mean_r": r.get("oos_mean_r"),
                    "oos_win": r.get("oos_win"), "oos_net_pct": r.get("oos_net_pct"),
                    "pvalue": r.get("pvalue"), "pvalue_method": "block_bootstrap",
                    "lockbox_n": r.get("lockbox_n"), "lockbox_mean_r": r.get("lockbox_mean_r"),
                    "lockbox_net_pct": r.get("lockbox_net_pct"), "lockbox_pvalue": r.get("lockbox_pvalue"),
                    "lockbox_held": 1 if r.get("lockbox_held") else 0,
                    "opt_sl": r.get("opt_sl"), "opt_tp": r.get("opt_tp"),
                    "opt_timeout": r.get("opt_timeout"),
                    "verdict": verdict, "failure_mode": fmode,
                    "source": source, "created_at": _now_iso(),
                }
                _append_event("trial", rec)
                con.execute(
                    f"INSERT INTO trials ({','.join(rec)}) VALUES ({','.join('?' * len(rec))})",
                    list(rec.values()))
                n += 1
    finally:
        con.close()
    return n


def _autopsy_row(rec: dict[str, Any], src: str, novelty_hash: str | None) -> dict[str, Any]:
    feats = rec.get("entry_feats")
    return {
        "trade_id": _ulid(), "src": src,
        "method_id": rec.get("method"), "novelty_hash": novelty_hash,
        "symbol": rec.get("symbol"), "side": rec.get("side"),
        "entry": rec.get("entry"), "exit_px": rec.get("exit"),
        "entry_ts_ms": rec.get("entry_ts_ms"), "closed_ts_ms": rec.get("closed_ts_ms"),
        "net": rec.get("net"), "r": rec.get("r"),
        "mae_pct": rec.get("mae_pct"), "mfe_pct": rec.get("mfe_pct"),
        "bars_held": rec.get("bars_held"), "exit_reason": rec.get("reason"),
        "entry_feats": canonical_json(feats) if isinstance(feats, dict) else None,
        "created_at": _now_iso(),
    }


def _insert_autopsy(row: dict[str, Any], kind: str) -> None:
    _append_event(kind, row)
    con = connect()
    try:
        with con:
            con.execute(
                f"INSERT OR IGNORE INTO trade_autopsy ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                list(row.values()))
    finally:
        con.close()


def record_shadow_close(rec: dict[str, Any], novelty_hash: str | None) -> None:
    """One trade_autopsy row per forward-test shadow close (numbers only)."""
    _insert_autopsy(_autopsy_row(rec, "shadow", novelty_hash), "shadow_close")


def record_mission_close(rec: dict[str, Any], novelty_hash: str | None = None) -> None:
    """One trade_autopsy row per MISSION close — the bot's own losses are the
    highest-value lesson-mining input (numbers only, no rationale text)."""
    r2 = {**rec, "method": rec.get("method") or rec.get("mech_method"),
          "entry_ts_ms": rec.get("entry_ts_ms") or rec.get("entry_ts"),
          "closed_ts_ms": rec.get("closed_ts_ms") or rec.get("closed_ts")}
    _insert_autopsy(_autopsy_row(r2, "mission", novelty_hash), "mission_close")


# ---------------------------------------------------------------------------
# LESSONS: pre-registered templates -> mechanical mining -> tiered gates.
#
# Templates are FIXED AND PRE-DECLARED (bounded multiple-testing surface). The
# Codex round-2 review set the ship gate for a HARD veto, implemented here:
#   candidate -> stats only.
#   advisory  -> pooled evidence is negative (n>=ADVISORY_MIN_N, avg_r<0,
#                win<=MAX_WIN) but NOT strong enough to block: logged only.
#   active    -> HARD veto. Requires ALL of:
#                  eff_n >= ACTIVE_MIN_EFF_N   (distinct (symbol, UTC-day)
#                    clusters — one market-wide move can't fake a sample),
#                  pooled avg_r < 0 and win <= MAX_WIN,
#                  mission_n >= ACTIVE_MIN_MISSION_N with mission_avg_r < 0
#                    (shadow-only evidence may never hard-veto the mission —
#                    different costs/universe/selection),
#                computed over a rolling LESSON_WINDOW_DAYS window (regimes
#                change; all rows kept for audit, only recent ones govern).
#
# Anti-lock (Codex critical #1): forward_test deliberately does NOT apply the
# lesson gate — the shadow ledger keeps trading through vetoed conditions and
# is the counterfactual probe stream. Demotion runs on POOLED rolling stats, so
# shadow evidence alone can demote an active lesson even while mission entries
# are being vetoed. Promotion needs mission evidence; demotion doesn't.
# ---------------------------------------------------------------------------
ADVISORY_MIN_N = 5
ACTIVE_MIN_EFF_N = 12
ACTIVE_MIN_MISSION_N = 3
LESSON_MAX_WIN = 0.45
LESSON_WINDOW_DAYS = 90
LESSON_FEATS = ("rsi14", "ret20", "ret5", "vol_ratio", "funding_z", "funding_rate_bps",
                "dd96_pct", "px_vs_ema200", "atr_pct", "close_pos", "ema_stack")

LESSON_TEMPLATES: list[dict[str, Any]] = [
    {"lesson_id": "chase_pump_long", "block_side": "LONG", "method_scope": "",
     "label": "LONG after a vertical run (chasing the pump)",
     "conds": [{"feat": "ret20", "op": ">", "val": 8.0}]},
    {"lesson_id": "chase_dump_short", "block_side": "SHORT", "method_scope": "",
     "label": "SHORT after a waterfall (chasing the dump)",
     "conds": [{"feat": "ret20", "op": "<", "val": -8.0}]},
    # funding: z-score alone is noisy when base funding is tiny (Codex) — require magnitude too
    {"lesson_id": "crowded_long", "block_side": "LONG", "method_scope": "",
     "label": "LONG into crowded positive funding",
     "conds": [{"feat": "funding_z", "op": ">", "val": 1.5},
               {"feat": "funding_rate_bps", "op": ">", "val": 1.0}]},
    {"lesson_id": "crowded_short", "block_side": "SHORT", "method_scope": "",
     "label": "SHORT into crowded negative funding",
     "conds": [{"feat": "funding_z", "op": "<", "val": -1.5},
               {"feat": "funding_rate_bps", "op": "<", "val": -1.0}]},
    {"lesson_id": "dead_tape", "block_side": "ANY", "method_scope": "",
     "label": "entries on dead volume (may also be pre-breakout compression — watch stats)",
     "conds": [{"feat": "vol_ratio", "op": "<", "val": 0.7}]},
    # renamed from fake_dip_long (Codex: dd96<2 means NEAR THE HIGH, not a dip) and
    # scoped to capitulation-style methods only — a breakout method WANTS this state.
    {"lesson_id": "near_high_long", "block_side": "LONG", "method_scope": "cap",
     "label": "dip-buy method LONG while price is near the 96-bar high (no real drawdown)",
     "conds": [{"feat": "dd96_pct", "op": "<", "val": 2.0}]},
]


def _lesson_matches(feats: dict[str, Any], side: str, tpl: dict[str, Any],
                    method_id: str | None = None) -> bool:
    if tpl.get("block_side") not in ("ANY", side):
        return False
    scope = tpl.get("method_scope") or ""
    if scope and (method_id is None or scope not in str(method_id)):
        return False
    try:
        import method_lab as ml
        return ml.method_fires(feats, {"when": tpl["conds"]})
    except Exception:
        return False


def mine_lessons() -> dict[str, Any]:
    """Recompute every template's tiered status over the rolling window of
    autopsy rows that carry an entry-feature snapshot. Deterministic; safe to
    run every cycle. Returns {lesson_id: {status, ...}} for logging."""
    if not DB.exists():
        return {}
    con = connect()
    out: dict[str, Any] = {}
    try:
        cutoff_ms = int((time.time() - LESSON_WINDOW_DAYS * 86400) * 1000)
        rows = [dict(r) for r in con.execute(
            "SELECT src, symbol, side, method_id, net, r, entry_feats, closed_ts_ms "
            "FROM trade_autopsy WHERE entry_feats IS NOT NULL")]
        for t in rows:
            try:
                t["feats"] = json.loads(t["entry_feats"])
            except Exception:
                t["feats"] = None
        rows = [t for t in rows if isinstance(t.get("feats"), dict)
                and int(t.get("closed_ts_ms") or 0) >= cutoff_ms]
        now = _now_iso()
        with con:
            for tpl in LESSON_TEMPLATES:
                hit = [t for t in rows
                       if _lesson_matches(t["feats"], t.get("side") or "", tpl, t.get("method_id"))]
                n = len(hit)
                wins = sum(1 for t in hit if (t.get("net") or 0) > 0)
                avg_r = (sum(float(t.get("r") or 0) for t in hit) / n) if n else None
                avg_net = (sum(float(t.get("net") or 0) for t in hit) / n) if n else None
                worst = min((float(t.get("r") or 0) for t in hit), default=None)
                # effective sample: distinct (symbol, UTC-day) — a single synchronized
                # market move that stops 7 clustered trades is ONE observation, not 7
                eff = len({(t.get("symbol"), int(t.get("closed_ts_ms") or 0) // 86400000) for t in hit})
                mis = [t for t in hit if t.get("src") == "mission"]
                sha = [t for t in hit if t.get("src") == "shadow"]
                m_n, s_n = len(mis), len(sha)
                m_avg = (sum(float(t.get("r") or 0) for t in mis) / m_n) if m_n else None
                s_avg = (sum(float(t.get("r") or 0) for t in sha) / s_n) if s_n else None
                neg = bool(n and (avg_r or 0) < 0 and (wins / n) <= LESSON_MAX_WIN)
                if (neg and eff >= ACTIVE_MIN_EFF_N
                        and m_n >= ACTIVE_MIN_MISSION_N and (m_avg or 0) < 0):
                    status = "active"
                elif neg and n >= ADVISORY_MIN_N:
                    status = "advisory"
                else:
                    status = "candidate"
                con.execute(
                    "INSERT INTO lessons (lesson_id, block_side, method_scope, conds, label, n, wins, "
                    "avg_r, avg_net, worst_r, eff_n, mission_n, mission_avg_r, shadow_n, shadow_avg_r, "
                    "status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(lesson_id) DO UPDATE SET block_side=excluded.block_side, "
                    "method_scope=excluded.method_scope, conds=excluded.conds, label=excluded.label, "
                    "n=excluded.n, wins=excluded.wins, avg_r=excluded.avg_r, avg_net=excluded.avg_net, "
                    "worst_r=excluded.worst_r, eff_n=excluded.eff_n, mission_n=excluded.mission_n, "
                    "mission_avg_r=excluded.mission_avg_r, shadow_n=excluded.shadow_n, "
                    "shadow_avg_r=excluded.shadow_avg_r, status=excluded.status, updated_at=excluded.updated_at",
                    (tpl["lesson_id"], tpl["block_side"], tpl.get("method_scope") or "",
                     canonical_json(tpl["conds"]), tpl["label"], n, wins, avg_r, avg_net, worst,
                     eff, m_n, m_avg, s_n, s_avg, status, now))
                out[tpl["lesson_id"]] = {"n": n, "eff_n": eff, "mission_n": m_n,
                                         "status": status, "avg_r": avg_r}
    finally:
        con.close()
    try:
        render_views()
    except Exception:
        pass
    return out


def _lessons_by_status(statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    if not DB.exists():
        return []
    con = connect(readonly=True)
    try:
        out = []
        q = f"SELECT * FROM lessons WHERE status IN ({','.join('?' * len(statuses))})"
        for r in con.execute(q, statuses):
            d = dict(r)
            try:
                d["conds"] = json.loads(d["conds"])
            except Exception:
                continue
            out.append(d)
        return out
    finally:
        con.close()


def lesson_hits(feats: dict[str, Any], side: str, method_id: str | None = None) -> list[dict[str, Any]]:
    """All advisory+active lessons matching this (fire-row, side, method).
    'active' = hard veto; 'advisory' = log-only. Rows returned verbatim."""
    hits = []
    for les in _lessons_by_status(("active", "advisory")):
        tpl = {"block_side": les["block_side"], "conds": les["conds"],
               "method_scope": les.get("method_scope") or ""}
        if _lesson_matches(feats, side, tpl, method_id):
            hits.append(les)
    return hits


def record_proposal(method: dict[str, Any], gate_result: str) -> None:
    """LLM proposal + the gate's verdict — quarantine table, audit only."""
    row = {"id": _ulid(), "method_id": method.get("id"),
           "dsl": canonical_json(method),
           "novelty_hash": method_hash(method), "bucket_hash": bucketed_hash(method),
           "gate_result": gate_result, "created_at": _now_iso()}
    _append_event("proposal", row)
    con = connect()
    try:
        with con:
            con.execute(
                f"INSERT INTO proposals ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                list(row.values()))
    finally:
        con.close()


def sync_armed_state(defs: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """Mirror state/method_lab/armed_methods.json into bi-temporal method_state.
    Arming is manual curation (no code writer exists) — this records transitions
    whenever the file's contents change. Returns transition descriptions."""
    armed_path = ROOT / "state" / "method_lab" / "armed_methods.json"
    try:
        armed = json.loads(armed_path.read_text(encoding="utf-8"))
    except Exception:
        armed = []
    now = _now_iso()
    want = {}
    for a in armed:
        d = (defs or {}).get(a.get("id"))
        want[a.get("id")] = method_hash(d) if d else None
    changes: list[str] = []
    con = connect()
    try:
        with con:
            cur = {r["method_id"]: r for r in con.execute(
                "SELECT * FROM method_state WHERE state='armed' AND invalid_at IS NULL")}
            for mid, r in cur.items():
                if mid not in want:
                    con.execute("UPDATE method_state SET invalid_at=? WHERE id=?", (now, r["id"]))
                    con.execute(
                        "INSERT INTO method_state (novelty_hash, method_id, state, reason, valid_at) "
                        "VALUES (?,?,?,?,?)", (r["novelty_hash"], mid, "disarmed", "removed from armed_methods.json", now))
                    changes.append(f"disarmed:{mid}")
                    _append_event("method_state", {"method_id": mid, "state": "disarmed", "at": now})
            for mid, nh in want.items():
                if mid not in cur:
                    con.execute(
                        "INSERT INTO method_state (novelty_hash, method_id, state, reason, valid_at) "
                        "VALUES (?,?,?,?,?)", (nh, mid, "armed", "armed_methods.json", now))
                    changes.append(f"armed:{mid}")
                    _append_event("method_state", {"method_id": mid, "state": "armed", "at": now})
    finally:
        con.close()
    return changes


# ---------------------------------------------------------------------------
# readers (novelty gate + DSR)
# ---------------------------------------------------------------------------
def known_hashes() -> set[str]:
    if not DB.exists():
        return set()
    con = connect(readonly=True)
    try:
        return {r[0] for r in con.execute(
            "SELECT DISTINCT novelty_hash FROM trials WHERE novelty_hash IS NOT NULL")}
    finally:
        con.close()


def rows_for_hash(nh: str, limit: int = 3) -> list[dict[str, Any]]:
    """Verbatim rows for gate feedback — the raw record, never a paraphrase
    (an LLM-rephrased rejection is itself a laundering channel)."""
    if not DB.exists():
        return []
    con = connect(readonly=True)
    try:
        return [dict(r) for r in con.execute(
            "SELECT method_id, verdict, failure_mode, oos_n, oos_mean_r, oos_net_pct, pvalue, "
            "lockbox_net_pct, lockbox_pvalue, source, created_at FROM trials "
            "WHERE novelty_hash=? ORDER BY created_at DESC LIMIT ?", (nh, limit))]
    finally:
        con.close()


def trial_counts() -> dict[str, int]:
    """DSR inputs: every validation event ever run (label-only rows included —
    they were real trials and must deflate future Sharpe like any other)."""
    if not DB.exists():
        return {"total": 0, "distinct_methods": 0}
    con = connect(readonly=True)
    try:
        tot = con.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
        dm = con.execute(
            "SELECT COUNT(DISTINCT COALESCE(novelty_hash, method_id)) FROM trials").fetchone()[0]
        return {"total": int(tot), "distinct_methods": int(dm)}
    finally:
        con.close()


def render_views() -> None:
    """Deterministic markdown views regenerated FROM SQL (never hand-edited,
    never LLM-written; if they drift, delete — they'll regrow identical)."""
    if not DB.exists():
        return
    con = connect(readonly=True)
    try:
        c = trial_counts()
        lines = [f"# BRAIN SUMMARY (auto-rendered {_now_iso()} — do not edit)",
                 f"\ntrials={c['total']} distinct_methods={c['distinct_methods']}", ""]
        lines.append("## Armed (current)")
        for r in con.execute("SELECT method_id, state, valid_at FROM method_state WHERE invalid_at IS NULL"):
            lines.append(f"- {r['method_id']}: {r['state']} since {r['valid_at']}")
        lines.append("\n## Lessons")
        for r in con.execute("SELECT lesson_id, status, n, wins, avg_r, label FROM lessons ORDER BY status"):
            lines.append(f"- [{r['status']}] {r['lesson_id']}: n={r['n']} wins={r['wins']} "
                         f"avg_r={r['avg_r']} — {r['label']}")
        lines.append("\n## Graveyard (top 30 DEAD by sample size)")
        for r in con.execute("SELECT method_id, failure_mode, oos_n, oos_mean_r, pvalue, source FROM trials "
                             "WHERE verdict='DEAD' ORDER BY COALESCE(oos_n,0) DESC LIMIT 30"):
            lines.append(f"- {r['method_id']} [{r['failure_mode']}] n={r['oos_n']} "
                         f"meanR={r['oos_mean_r']} p={r['pvalue']} ({r['source']})")
        (MEM_DIR / "BRAIN_SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    finally:
        con.close()


def rebuild_from_events() -> dict[str, int]:
    """Disaster recovery: brain.db is a projection — rebuild it from the log."""
    if DB.exists():
        DB.unlink()
    con = connect()
    counts = {"trial": 0, "shadow_close": 0, "proposal": 0, "method_state": 0}
    try:
        with con:
            for line in (EVENTS.read_text(encoding="utf-8").splitlines() if EVENTS.exists() else []):
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                kind, p = ev.get("kind"), ev.get("payload") or {}
                if kind == "trial":
                    con.execute(f"INSERT OR IGNORE INTO trials ({','.join(p)}) VALUES ({','.join('?' * len(p))})",
                                list(p.values()))
                elif kind == "shadow_close":
                    con.execute(f"INSERT OR IGNORE INTO trade_autopsy ({','.join(p)}) VALUES ({','.join('?' * len(p))})",
                                list(p.values()))
                elif kind == "proposal":
                    con.execute(f"INSERT OR IGNORE INTO proposals ({','.join(p)}) VALUES ({','.join('?' * len(p))})",
                                list(p.values()))
                elif kind == "method_state":
                    con.execute("INSERT INTO method_state (novelty_hash, method_id, state, reason, valid_at) "
                                "VALUES (?,?,?,?,?)",
                                (p.get("novelty_hash"), p.get("method_id"), p.get("state"),
                                 "rebuilt", p.get("at") or _now_iso()))
                if kind in counts:
                    counts[kind] += 1
    finally:
        con.close()
    return counts


# ---------------------------------------------------------------------------
# the novelty gate (deterministic, pre-compute)
# ---------------------------------------------------------------------------
def novelty_gate(method: dict[str, Any], extra_hashes: set[str] | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Returns (gate_result, verbatim_rows).
    REJECT_EXACT        — this exact idea already has a trial (or sits in
                          seeds/pool via extra_hashes) and is not a winner.
    REJECT_KNOWN_WINNER — exact match whose latest trial is LOCKBOX_PASS: no
                          re-test needed, but callers must surface it (a winner
                          that fell out of the pool should be RESURRECTED, not
                          silently skipped — Codex review).
    FLAG_NEAR           — bucketed twin of a known trial. The LLM proposer must
                          treat this as a REJECT (threshold-nudging a dead idea
                          is the main laundering escape route); the hand-curated
                          ingest path may accept a deliberate A/B.
    PASS                — genuinely new idea.
    """
    nh = method_hash(method)
    known = known_hashes()
    if nh in known:
        rows = rows_for_hash(nh)
        if rows and rows[0].get("verdict") == "LOCKBOX_PASS":
            return "REJECT_KNOWN_WINNER", rows
        return "REJECT_EXACT", rows
    if extra_hashes and nh in extra_hashes:
        return "REJECT_EXACT", rows_for_hash(nh)
    bh = bucketed_hash(method)
    if DB.exists():
        con = connect(readonly=True)
        try:
            near = [dict(r) for r in con.execute(
                "SELECT method_id, verdict, failure_mode, oos_mean_r, pvalue FROM trials "
                "WHERE bucket_hash=? ORDER BY created_at DESC LIMIT 3", (bh,))]
        finally:
            con.close()
        if near:
            return "FLAG_NEAR", near
    return "PASS", []
