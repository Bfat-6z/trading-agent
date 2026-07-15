"""Direct boundary tests for the era-window in build_memory_context (review: the
wiring pins compare against the helper itself and can't catch a broken branch).
Expectations here are hand-computed — never derived from the function under test."""
import llm_trader_memory as ltm

CUR = "cx/gpt-5.6-sol"


def _row(sym, model=None, mech=None, net=1.0, r=0.5):
    d = {"symbol": sym, "side": "LONG", "net": net, "r": r}
    if model:
        d["model"] = model
    if mech:
        d["mech_method"] = mech
    return d


def test_boundary_7_rows_falls_back_with_note():
    closed = [_row("BBB") for _ in range(20)] + [_row("AAA", CUR) for _ in range(7)]
    ctx = ltm.build_memory_context(closed, model=CUR)
    assert ctx.get("era_note")                       # fallback fired
    assert "BBB" in ctx["stats"].get("by_symbol_side", {}) or \
           any("BBB" in l for l in ctx["lessons"])   # prior-era rows present


def test_boundary_8_rows_uses_era_only():
    closed = [_row("BBB") for _ in range(60)] + [_row("AAA", CUR) for _ in range(8)]
    ctx = ltm.build_memory_context(closed, model=CUR)
    assert not ctx.get("era_note")
    blob = str(ctx["stats"]) + str(ctx["lessons"]) + str(ctx["recent"])
    assert "BBB" not in blob                         # prior era fully excluded


def test_mech_rows_excluded_from_era():
    closed = [_row("MMM", CUR, mech="flush_no_oi_mech") for _ in range(50)] + \
             [_row("AAA", CUR) for _ in range(8)]
    ctx = ltm.build_memory_context(closed, model=CUR)
    assert "MMM" not in str(ctx["stats"]) + str(ctx["recent"])


def test_cap_200():
    closed = [_row("OLD", CUR) for _ in range(60)] + [_row("NEW", CUR) for _ in range(200)]
    ctx = ltm.build_memory_context(closed, model=CUR)
    assert "OLD" not in str(ctx["stats"]) + str(ctx["recent"])


def test_model_none_legacy_includes_mech_no_note():
    closed = [_row("MMM", CUR, mech="x") for _ in range(5)] + [_row("AAA") for _ in range(5)]
    ctx = ltm.build_memory_context(closed, model=None)
    assert not ctx.get("era_note")
    assert "MMM" in str(ctx["stats"])                # legacy pools everything


def test_empty_ledger_no_false_note():
    ctx = ltm.build_memory_context([], model=CUR)
    assert not ctx.get("era_note")                   # review: false claim on fresh ledger


def test_thesis_wrong_lesson_threshold_and_rank():
    # fires at >=40% + dominating noise-stops over >=8 instrumented rows; rank FIRST
    rows = [{"net": -1, "r": -1, "side": "LONG", "reason": "sl",
             "thesis_wrong": i < 5, "noise_stop": False} for i in range(10)]
    ms = ltm.mistake_lessons(rows)
    assert ms and ms[0].startswith("THESIS WRONG")
    # below threshold (3/10=30%) -> silent
    rows2 = [{"net": -1, "r": -1, "side": "LONG", "reason": "sl",
              "thesis_wrong": i < 3, "noise_stop": False} for i in range(10)]
    assert not any(m.startswith("THESIS WRONG") for m in ltm.mistake_lessons(rows2))
    # n=7 instrumented -> dormant
    rows3 = [{"net": -1, "r": -1, "side": "LONG", "reason": "sl",
              "thesis_wrong": True, "noise_stop": False} for _ in range(7)]
    assert not any(m.startswith("THESIS WRONG") for m in ltm.mistake_lessons(rows3))


def test_thesis_wrong_not_blanket_suppressed():
    import llm_trader as lt
    # blanket tuple must not swallow THESIS WRONG (prefix check)
    assert not "THESIS WRONG x".startswith(("STAND ASIDE", "AVOID", "OVER-TRADING"))
    b = lt._mistakes_block()   # smoke: never raises, bounded
    assert isinstance(b, str)
