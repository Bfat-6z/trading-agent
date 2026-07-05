---
tags: [reference]
updated: 2026-07-06
---

# Sizing — mech_sizing.py (growth-optimal, data-derived)

Chuỗi nhân (mỗi fire trong cụm):
```
E = C_DD × kelly_empirical(r_shifted_LCB) × haircut_selection / corr_div
margin = min(E / lev, 25%) ; tổng notional cụm ≤ GROSS_EXP_CAP
```

| Thành phần | Giá trị | Vì sao |
|---|---|---|
| Empirical Kelly | argmax E[ln(1+E·r)] trên phân bố lệnh THẬT | thực tế có timeout, không phải win/lose nhị phân |
| LCB shrinkage | m − z·SE (z=1; 1.5 nếu n<500 hoặc p>0.01); LCB≤0 → skip | chống ước-lượng-lạc-quan |
| **Selection haircut** | ×0.5 tới khi forward_confirmed (≥30 lệnh shadow net-dương) | distribution lấy từ chính OOS đã chọn method = winner-biased (Codex R1) |
| C_DD | **0.5** (half-Kelly; env MECH_C_DD) | P(sập nửa vốn)~12%; sếp chấp nhận. Không lên 0.7 khi chưa có live proof |
| Crisis corr | cùng hướng ρ=0.85, ngược hướng ρ=0 (side-aware) | tương quan spike →1 lúc crash; hedge không bị phạt |
| GROSS_EXP_CAP | 3.0× notional | backstop duy nhất còn lại (phanh ngày đã tắt theo lệnh sếp) |

Distribution nguồn: `state/method_lab/survivor_distributions.json` (từ deep run trên đúng universe sẽ đánh). Side + forward_confirmed inject trong `_mechanical_decisions`.

Lịch sử: binary-Kelly cũ mù tương quan → 10.8× notional cụm dump (suýt tự sát) — xem [[decisions]] #4.
