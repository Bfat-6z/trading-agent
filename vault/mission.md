---
tags: [mission]
updated: 2026-07-06
---

# Mission $100 → $1000 (paper)

**Trạng thái honest (2026-07-06):** equity ~$68 (đáy do lỗi universe coin rác, đã sửa — xem [[decisions]] #1). KHÔNG fake-reset: bot leo lại từ hố nó tự đào, scorecard phải thật.

## Vũ khí hiện tại
- **1 method armed:** `capitulation_long` (LONG, RSI<22 + vol; SL1/TP6/TO48 tối ưu grid) — method DUY NHẤT qua lockbox cả 2 universe. Hiếm fire (chờ thị trường sập) → mission sẽ CHẬM. Đó là sự thật, không phải bug.
- **Ứng viên:** um_pb_02 đang forward-test. Nguồn method mới: lab 3h/lần + novelty gate.
- Sizing: half-Kelly × 0.5 haircut (chưa forward-confirmed) — xem [[sizing]].

## Kỳ vọng đúng
Edge thật cực hiếm (~650 trials → 1 sống). Tăng tốc mission = tìm thêm method qua lockbox, KHÔNG phải hạ ngưỡng. Mọi lần hạ ngưỡng trong quá khứ đều trả giá (xem [[decisions]]).

## Xem nhanh
`python brain_query.py armed` · dashboard horizon-ui · `state/memory/BRAIN_SUMMARY.md`
