# Phase 03: Dashboard And Learning Integration

## Context Links

- [Plan](./plan.md)
- Depends on [Performance Aggregation](./phase-02-performance-aggregation.md)
- Existing dashboard: `E:\keo-moi-mail\trading-agent\agent_status_dashboard.py`
- Existing learner: `E:\keo-moi-mail\trading-agent\market_learner.py`
- Existing setup library: `E:\keo-moi-mail\trading-agent\setup_skill_library.py`

## Overview

Priority: P1.
Status: Complete.

Expose shadow performance separately from paper performance and make the learning layer aware of shadow evidence without pretending it is real paper/live PnL.

## Requirements

- Dashboard `/api/status` includes `shadow_performance`.
- UI has a Shadow Performance panel.
- Keep paper stats and shadow stats separate.
- Learning integration should use shadow stats as evidence/bias, not as confirmed paper performance.
- Existing `live_monitors` behavior remains unchanged.
- Dashboard must surface data-quality warnings, not only headline WR/net.
- Learning integration must initially be read-only/negative-only unless explicitly approved later.

## Related Code Files

Modify:

- `E:\keo-moi-mail\trading-agent\agent_status_dashboard.py`
- `E:\keo-moi-mail\trading-agent\tests\test_agent_status_dashboard.py`
- Optional: `E:\keo-moi-mail\trading-agent\market_learner.py`
- Optional: `E:\keo-moi-mail\trading-agent\setup_skill_library.py`

Read:

- `E:\keo-moi-mail\trading-agent\state\agent_memory\shadow_performance_latest.json`

## Dashboard Schema

Add compact payload:

```json
{
  "shadow_performance": {
    "updated_at": "...",
    "trades": 0,
    "win_rate": 0.0,
    "net": 0.0,
    "expectancy": 0.0,
    "profit_factor": 0.0,
    "ambiguous_count": 0,
    "top_segments": [],
    "kill_candidates": [],
    "under_sampled": true,
    "data_quality": {
      "unresolved_count": 0,
      "ambiguous_count": 0,
      "skipped_count": 0,
      "confidence": "low|medium|high"
    },
    "assumption_hash": "...",
    "metric_mode": "closed_only"
  }
}
```

## UI Display

Overview KPI:

- Shadow WR
- Shadow net
- Shadow sample count
- Data-quality confidence
- Ambiguous/unresolved warning

Learning tab:

- Top segments
- Worst segments
- Kill candidates
- Under-sampled warning

## Learning Integration

Initial safe behavior:

- If segment has enough shadow closes and negative expectancy, add evidence to memory/bias.
- Do not auto-enable new trade paths from shadow stats alone.
- Promotion still requires closed paper trades per existing live-readiness gate.
- Do not call `record_setup_outcome()` from shadow closes in this phase, because that function currently represents trade outcomes and would mix evidence types.
- If needed, add separate `shadow_evidence` section/file rather than polluting paper setup stats.

## Todo List

- [ ] Load shadow performance in dashboard status.
- [ ] Add UI panel.
- [ ] Add dashboard tests.
- [ ] Decide minimal learner hook after seeing shadow schema.
- [ ] Add explicit dashboard labels: `Shadow / would-trade only`.
- [ ] Add missing-file and mixed-assumption tests.
- [ ] Add learner tests if hook is included.

## Success Criteria

- Dashboard clearly shows shadow != paper.
- Existing dashboard tests still pass.
- If no shadow performance file exists, dashboard shows safe empty state.
- Learning cannot promote live mode from shadow-only evidence.

## Risk Assessment

| Risk | Mitigation |
| --- | --- |
| User confuses shadow PnL with real PnL | Label as Shadow / would-trade only. |
| Learning overreacts to shadow data | Use only negative filters initially. |
| UI clutter | Add compact panel, no nested cards. |
| Shadow stats pollute paper setup skill stats | Keep separate `shadow_performance` payload; no direct `record_setup_outcome()` update. |

## Security Considerations

- Dashboard remains read-only.
- No live order controls added.
- No API-key display.

## Completion Notes

- Added `compact_shadow_performance()` and `shadow_performance` to `/api/status`.
- Added Shadow WR KPI, Shadow Performance overview card, Shadow Segments, and Shadow Kill Candidates UI.
- UI labels shadow data as `Shadow / would-trade only` and keeps it separate from paper/live PnL.
- Added dashboard tests for compact payload, status integration, and HTML labels.
