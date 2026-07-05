---
tags: [reference]
updated: 2026-07-06
---

# Validation Pipeline — một method sống hay chết thế nào

```
DSL candidate (LLM propose / ingest tay / seed)
 │  novelty gate: method_hash v2 (side+conds+sl/tp+timeout, KHÔNG label)
 │  vs mộ brain.db ∪ pool ∪ seeds → REJECT_EXACT / REJECT_KNOWN_WINNER / FLAG_NEAR / PASS
 ▼
method_lab (3h): 100 coin, BH-FDR + hysteresis → survivors.json (RESEARCH only, không đụng tiền)
 ▼
deep_validation (chuẩn arm): universe $50M (hoặc $15M rộng), 5 tháng 15m
 │  per coin: train 55% (chọn SL×TP×TO grid 16 combo) / OOS-select 25% (chấm) / LOCKBOX 20% (không ai đụng)
 │  scorecard: BLOCK bootstrap CI + BLOCK sign-flip permutation (block ~ n^(1/3))
 │  ROBUST = lockbox p<0.05, n≥100, dương + OOS chỉ cần cùng hướng
 ▼
trials ghi vĩnh viễn (verdict DEAD/LOCKBOX_PASS/PENDING + failure_mode + as_traded_hash)
 ▼
forward_test shadow (fill y hệt backtest, data live) → ≥30 lệnh net-dương = forward_confirmed → sizing bỏ haircut
 ▼
arm tay → armed_methods.json (llm_trader đọc, lab không ghi đè được)
```

## Ngưỡng nhớ nằm lòng
- p<0.005 (block) = chuẩn cứng cho claim đơn lẻ; BH-FDR khi test cả pool
- lockbox RỚT = overfit bất kể OOS đẹp cỡ nào (S_QUIET_BEAR_COIL: OOS +423% vẫn chết)
- n<30 = không kết luận; universe nhỏ → BH quá nghiêm → nhìn strong-solo + lockbox

## Chạy
```
DEEP_ONLY_IDS="a,b" DEEP_MIN_QVOL=50000000 venv/Scripts/python.exe deep_validation.py
venv/Scripts/python.exe tf_validation.py --tf 1h     # per-timeframe
```
Cả hai TỰ ghi trials vào brain (đừng bypass).
