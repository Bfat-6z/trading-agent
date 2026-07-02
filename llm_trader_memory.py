"""Persistent-learning memory distillation for the LLM PAPER trader (pure).

WHY this module exists: llm_trader.py currently feeds the LLM only the last-8
raw memory rows for the same coin/regime. That discards most of the history
and lets single outliers dominate the prompt. This module aggregates ALL
closed trades into grouped statistics (symbol / regime / hour-bucket / side /
leverage / symbol+side), distills them into short DATA-phrased lines
("SOLUSDT SHORT: 1W/4L, mean -0.52R"), and packs a compact context dict that
llm_trader.decide() injects into its prompt as the "memory" block via
llm_trader.memory_context() (plan 260702, checklist #10/#11; wiring pinned by
the regression tests in tests/test_llm_trader_memory.py).

Design rules (owner + plan — enforced here, not left to callers):
- Lessons are COUNTS, never prescriptions. No "never do X" / "always avoid":
  markets are non-stationary, so the LLM must weigh evidence contextually
  instead of obeying blanket bans baked in by a string formatter.
- Pure functions over plain lists/dicts: no file/network I/O, deterministic,
  so llm_trader.decide and the tests share exactly one code path.
- Robust to malformed rows: closed.jsonl is append-only and occasionally
  hand-edited; a bad row must be SKIPPED, never crash the trading loop.
  A row needs numeric r+net to count in stats; per-grouping keys that are
  missing/invalid only drop the row from THAT grouping.

Closed-trade record keys (written by llm_trader.resolve): symbol, side,
regime, hour_utc, entry, exit, reason, net, r, leverage, rationale, closed_ts.
"""
from __future__ import annotations

from typing import Any

# Four 6h UTC buckets — coarse enough that groups reach n>=2 quickly on a
# 300s-loop paper account, fine enough to catch session effects (Asia/EU/US).
HOUR_BUCKETS = ("0-5", "6-11", "12-17", "18-23")

_GROUPINGS = ("by_symbol", "by_regime", "by_hour_bucket", "by_side",
              "by_leverage", "by_symbol_side")

# recent_trades caps the stored rationale at 120 chars (plan contract);
# build_memory_context trims further because 10 rationales dominate the
# prompt budget (~2k chars target for the whole context at 100 trades).
_RATIONALE_MAX = 120
_CONTEXT_RATIONALE_MAX = 40

# Which groupings survive into build_memory_context's trimmed stats, and how
# many top-n groups each keeps. WHY these three: by_symbol_side subsumes
# by_symbol/by_side at higher specificity, and the lessons lines (sorted by
# evidence weight across ALL six groupings) already surface any strong
# leverage/side/symbol group, so the prompt stays compact without losing
# signal.
_CONTEXT_GROUP_LIMITS = {"by_symbol_side": 4, "by_regime": 3,
                         "by_hour_bucket": 4}

# Lesson label per grouping. Bare keys are self-describing for symbol views;
# the rest get a suffix so "SHORT" (a side) can't be misread as a symbol.
_LESSON_LABELS = {
    "by_symbol": "{k}",
    "by_symbol_side": "{k}",
    "by_regime": "{k} regime",
    "by_hour_bucket": "{k}h UTC",
    "by_side": "{k} side",
    "by_leverage": "{k} leverage",
}


def _num(value: Any) -> float | None:
    """Coerce to finite float; None on anything malformed (incl. NaN)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _text(value: Any) -> str | None:
    """Non-empty stripped string or None (missing/blank keys are 'invalid')."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def hour_bucket(hour_utc: Any) -> str | None:
    """Map a UTC hour (0-23) onto the plan's four 6h buckets; None if invalid.

    Edges matter (tested): 5 -> '0-5', 6 -> '6-11', 23 -> '18-23'.
    """
    h = _num(hour_utc)
    if h is None:
        return None
    h = int(h)
    if 0 <= h <= 23:
        return HOUR_BUCKETS[h // 6]
    return None


def _row_group_keys(row: dict[str, Any]) -> dict[str, str]:
    """Grouping-name -> group-key for one closed row; invalid keys omitted."""
    keys: dict[str, str] = {}
    sym = _text(row.get("symbol"))
    side = _text(row.get("side"))
    side = side.upper() if side else None
    if sym:
        keys["by_symbol"] = sym
    if side:
        keys["by_side"] = side
    if sym and side:
        keys["by_symbol_side"] = f"{sym} {side}"
    regime = _text(row.get("regime"))
    if regime:
        keys["by_regime"] = regime
    bucket = hour_bucket(row.get("hour_utc"))
    if bucket:
        keys["by_hour_bucket"] = bucket
    lev = _num(row.get("leverage"))
    if lev is not None and lev > 0:
        keys["by_leverage"] = f"x{int(lev)}"
    return keys


def aggregate_stats(closed: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate ALL closed trades into the six plan groupings.

    Returns {grouping: {key: {n, wins, win_rate, mean_r, total_net}}} with all
    six grouping keys always present. Only groups with n>=2 are kept — a
    single trade is an anecdote, not evidence (plan contract). A win is
    net > 0, matching llm_trader.py's account accounting. Rows without a
    numeric r AND net are skipped entirely; never raises on malformed input.
    """
    acc: dict[str, dict[str, list[float]]] = {g: {} for g in _GROUPINGS}
    for row in closed or []:
        if not isinstance(row, dict):
            continue
        r = _num(row.get("r"))
        net = _num(row.get("net"))
        if r is None or net is None:
            continue  # unusable for stats — skip, never raise
        for grouping, key in _row_group_keys(row).items():
            slot = acc[grouping].setdefault(key, [0, 0, 0.0, 0.0])
            slot[0] += 1
            slot[1] += 1 if net > 0 else 0
            slot[2] += r
            slot[3] += net
    out: dict[str, Any] = {}
    for grouping in _GROUPINGS:
        kept: dict[str, dict[str, Any]] = {}
        for key in sorted(acc[grouping]):
            n, wins, sum_r, sum_net = acc[grouping][key]
            n, wins = int(n), int(wins)
            if n < 2:
                continue  # n>=2 filter: anecdotes don't make stats
            kept[key] = {"n": n, "wins": wins,
                         "win_rate": round(wins / n, 3),
                         "mean_r": round(sum_r / n, 3),
                         "total_net": round(sum_net, 2)}
        out[grouping] = kept
    return out


def distill_lessons(stats: dict, min_n: int = 3, max_lines: int = 12) -> list[str]:
    """Distill aggregate stats into short DATA-phrased lesson lines.

    Format: "SOLUSDT SHORT: 1W/4L, mean -0.52R" — pure counts the LLM weighs
    contextually. Deliberately NO prescriptive wording ("never", "always
    avoid"): a blanket ban baked into a formatter would defeat the owner's
    non-stationarity requirement (checklist #12). Sorted by evidence weight
    |mean_r| * n descending (strong AND repeated beats loud-but-once), capped
    at max_lines; groups with n < min_n are noise and dropped.
    """
    scored: list[tuple[float, int, str, str]] = []
    if not isinstance(stats, dict):
        return []
    for grouping, label_fmt in _LESSON_LABELS.items():
        groups = stats.get(grouping)
        if not isinstance(groups, dict):
            continue
        for key, g in groups.items():
            if not isinstance(g, dict):
                continue
            n = _num(g.get("n"))
            wins = _num(g.get("wins"))
            mean_r = _num(g.get("mean_r"))
            if n is None or wins is None or mean_r is None or int(n) < min_n:
                continue
            n_i, w_i = int(n), int(wins)
            label = label_fmt.format(k=key)
            line = f"{label}: {w_i}W/{n_i - w_i}L, mean {mean_r:+.2f}R"
            # (-weight, -n, label) => deterministic order, evidence first.
            scored.append((abs(mean_r) * n_i, n_i, label, line))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    cap = max(0, int(max_lines))
    return [line for _, _, _, line in scored[:cap]]


def recent_trades(closed: list[dict[str, Any]], k: int = 10) -> list[dict[str, Any]]:
    """Last k closed trades for rationale-vs-outcome review, oldest first.

    WHY rationale is kept (<=120 chars): the LLM sees what it CLAIMED would
    happen next to what DID happen — the checklist-#11 feedback loop that raw
    stats can't provide. Rows without symbol+side are skipped (they can't be
    reviewed) and do not consume one of the k slots. List order is trusted as
    chronological because closed.jsonl is append-only.
    """
    rows: list[dict[str, Any]] = []
    for row in closed or []:
        if not isinstance(row, dict):
            continue
        sym = _text(row.get("symbol"))
        side = _text(row.get("side"))
        if not sym or not side:
            continue  # unreviewable — skip, never raise
        r = _num(row.get("r"))
        hour = _num(row.get("hour_utc"))
        rows.append({
            "symbol": sym,
            "side": side.upper(),
            "regime": _text(row.get("regime")),
            "hour": int(hour) if hour is not None else None,
            "r": round(r, 3) if r is not None else None,
            "reason": _text(row.get("reason")),
            "rationale": str(row.get("rationale") or "")[:_RATIONALE_MAX],
        })
    k = int(k)
    if k <= 0:
        return []
    return rows[-k:]


def _trim_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Prompt-budget view of aggregate_stats: top groups by n, {n, mean_r}.

    wins/win_rate/total_net are dropped here because the lessons lines
    already carry exact W/L counts (richer than a rate); repeating them would
    double-spend the char budget without adding information.
    """
    trimmed: dict[str, Any] = {}
    for grouping, limit in _CONTEXT_GROUP_LIMITS.items():
        groups = stats.get(grouping) or {}
        top = sorted(groups.items(), key=lambda kv: (-kv[1]["n"], kv[0]))[:limit]
        trimmed[grouping] = {
            key: {"n": g["n"], "mean_r": round(g["mean_r"], 2)}
            for key, g in top
        }
    return trimmed


def build_memory_context(closed: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact {"stats", "lessons", "recent"} dict for the decide() prompt.

    Target ~2000 chars of JSON at 100 closed trades (plan contract): stats are
    trimmed to the top groups, lessons are the capped distilled lines, and
    recent rationales are re-truncated harder than recent_trades' 120-char cap
    because ten of them dominate the budget otherwise.
    """
    stats = aggregate_stats(closed)
    lessons = distill_lessons(stats)
    recent = []
    for row in recent_trades(closed, k=10):
        row = dict(row)
        row["rationale"] = (row.get("rationale") or "")[:_CONTEXT_RATIONALE_MAX]
        recent.append(row)
    return {"stats": _trim_stats(stats), "lessons": lessons, "recent": recent}
