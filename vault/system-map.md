---
tags: [architecture]
updated: 2026-07-06
---

# System Map — trading-agent

## Vòng đời tự trị (khép kín, không điểm nào LLM ghi vào sự thật)
```
LLM đề xuất method (DSL)
  → NOVELTY GATE (brain.novelty_gate: mộ 358 idea chết + pool/seeds — REJECT mọi thứ trừ PASS trên đường LLM)
  → method_lab (screening 3h, BH-FDR)  →  deep_validation (grid SL/TP/TO + block bootstrap + LOCKBOX)
  → trial GHI VĨNH VIỄN vào brain.db (DSR ledger)
  → forward_test (shadow ledger = trọng tài data-live, KHÔNG lesson-gate = probe stream)
  → arm thủ công vào armed_methods.json (sync vào method_state)
  → llm_trader đánh mission (lesson gate + mech_sizing)
  → autopsy (MAE/MFE + entry_feats) → mine_lessons (cơ học) → gate lệnh sau ↺
```

## File cốt lõi (đường dẫn = trading-agent/)
| File | Vai trò | Điểm nóng |
|---|---|---|
| `llm_trader.py` | Bot mission. PROVEN_ONLY=1: chỉ fire khi method armed match trên bar đóng | `_mechanical_decisions` (fire+lesson gate+sizing), `open_positions`, `resolve` (đóng lệnh+autopsy), `_hot_universe` ($50M floor + rank vol 1h) |
| `brain.py` | Second brain: registry SQLite + event log hash-chained | `novelty_gate`, `record_trials`, `mine_lessons`, `lesson_hits`, `_chain_lock` (fail-closed) |
| `method_canonical.py` | Identity v2 của method: side+conds+sl/tp+**timeout**; labels bị loại | `method_hash` (gate key), `bucketed_hash` (advisory) |
| `method_lab.py` | DSL engine + backtest không lookahead + BH-FDR | `feature_frame`, `method_fires`, `backtest_method`, `run_lab` (dedup hash ở đầu) |
| `method_lab_runner.py` | Loop 3h: LLM propose → gate → validate pool | `propose_methods` (gate chặn non-PASS), ghi first-ever trials |
| `deep_validation.py` | Validator chuẩn: train 55% / OOS-select 25% / **LOCKBOX 20%** | ghi trials + `sync_armed_state` sau mỗi run |
| `forward_test.py` | Shadow ledger trên bar live (fill y hệt backtest) | MAE/MFE + entry_feats + as-traded hash (gồm timeout) |
| `mech_sizing.py` | Size = empirical Kelly × haircut × crisis-corr | xem [[sizing]] |
| `llm_trader_scorecard.py` | **BLOCK** bootstrap + block permutation (IID = lạc quan giả) | `_block_len ~ n^(1/3)` |
| `brain_query.py` | CLI read-only: counts/graveyard/lessons/armed/render | thay MCP server |
| `agent_process_supervisor.py` | Fleet 25 agent. ⚠️ **sửa specs XONG PHẢI restart supervisor** (bài học zombie) | specs() |

## State quan trọng (state/)
- `memory/brain.db` + `memory/events.jsonl` — ground truth (LLM read-only) + `memory/BRAIN_SUMMARY.md`
- `method_lab/armed_methods.json` — bộ armed (hand-curated, lab KHÔNG ghi đè được)
- `llm_trader/{account,positions,closed,governance}` — sổ mission
- `forward_test/shadow_*` — sổ shadow

## Bẫy đã trả học phí (đừng lặp)
- Coin rác <$50M = dao rơi ([[decisions]] #1)
- OOS-select đẹp ≠ edge ([[decisions]] #5 — lockbox bắt S_QUIET_BEAR_COIL)
- Sửa supervisor specs mà không restart process = zombie respawn
- PowerShell Start-Process bẻ gãy quoting prompt dài → prompt vào file
- `codex exec` phải `< /dev/null` (không nó đợi stdin)
