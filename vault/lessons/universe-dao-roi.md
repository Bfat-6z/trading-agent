---
tags: [lesson, core]
---

# Universe & dao rơi — vì sao floor $50M

Mission từng tụt **−$30 (equity 100→70)** trong 1 ngày vì một sai lầm scope: nới lưới lên 220 coin ($5M+) theo tinh thần "cả sàn" **mà không có cổng chất lượng**.

**Cơ chế chết:** capitulation = mua RSI<22 chờ hồi. Trên BTC/ETH nó hồi thật. Trên coin rác $6-20M (POWER, IN, ESPORTS, ORDI) chúng **sập liên tục → RSI<22 liên tục → bot dồn fire đúng vào nhóm dump-thẳng-không-hồi**. POWER dính SL 3 lần liên tiếp.

**Bài học thống kê:** backtest +0.15R là **trung bình 205 coin** — kỳ vọng dương trung bình che giấu phương sai per-coin khổng lồ. Live firing tự dồn vào cái đuôi tệ nhất (adverse selection).

**Luật rút ra:**
- QUÉT rộng được, **ĐÁNH chỉ nơi method được validate** → `UNIVERSE_MIN_QVOL = $50M`
- Universe xếp theo **vol khung đang đánh** (SCAN_TF=1h) — coin nóng thật, không phải to nhờ hôm qua
- Sizing đúng không cứu được universe −EV (hôm đó sizing mới đã cứu một nửa thiệt hại, nhưng gốc vẫn là universe)

Liên quan: [[sizing]] · [[decisions]] #1 · [[mission]]
