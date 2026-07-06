---
tags: [lesson, manual-trading]
---

# Bài học trade thủ công (di sản các phiên 05/2026)

Đúc từ giai đoạn sếp + Claude trade tay Binance futures (trước khi chuyển hẳn sang bot proven-only). Manual của sếp 3/3 win; picks thuật toán của Claude 2/9 — **đừng ép B+ setup, chờ A+**.

## 10 lỗi trả học phí (−$1.41 drawdown + $0.09 lẻ)
1. Bỏ qua funding/OI trước khi vào → vào đúng lúc crowded
2. Chọn sector laggard thay vì leader
3. Sai toán futures (notional vs margin)
4. Quản lời kém: không dời SL khi +1R → lời thành lỗ
5. **Dead zone** (02:00-06:00 UTC): volume mỏng, stop hunt dày — tránh vào lệnh
6. **Fake breakout / stop hunt**: breakout không volume = mồi; chờ retest
7. Không check lịch unlock/news trước khi vào
8. Khi 2 agent phân tích mâu thuẫn → đứng ngoài (đừng cherry-pick cái mình thích)
9. Trail SL phải theo **cấu trúc** (swing low/high), không theo % cứng
10. **Đừng đuổi lệnh khi sốt ruột** ("chậm quá pump rồi" → MARKET mua đỉnh wick, đắt hơn LIMIT $2.2 — ZEC 05/26)

## Nguyên tắc A+ (từ kèo HYPE +$2.91)
Multi-source research → 3 lớp cross-check (macro/order-flow/chart) → SL theo cấu trúc → trail true-BE. **Số lệnh ít + chất lượng cao thắng số lượng.**

## Đã cơ khí hoá vào bot
Chase gate (RSI≥65 + extension), low-vol gate, counter-momentum block, structure SL/TP, [[auto/lessons]] templates (chase_pump, dead_tape...) — bot học đúng các vết sẹo này qua số liệu.

Liên quan: [[universe-dao-roi]] · [[system-map]]
