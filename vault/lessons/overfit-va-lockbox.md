---
tags: [lesson, core]
---

# Overfit & Lockbox — chuyện S_QUIET_BEAR_COIL

Case study đắt nhất của hệ:

| Bước | S_QUIET_BEAR_COIL (SHORT) trông thế nào |
|---|---|
| OOS-select | p=0.0004, net **+423%**, 7860 lệnh — "chén thánh" |
| Bot arm + đánh | Mở 7 short liền |
| **LOCKBOX** (data chưa từng dùng để chọn) | **−0.046R, net −139%, p=0.91 = OVERFIT** |
| Kết | Tước ngay; lệnh cũ resolve lỗ (equity dính dư chấn) — nhưng chặn được trước khi cháy sâu |

**Ba lớp tự-lừa đã bị vạch trong cùng tuần:**
1. **IID bootstrap phóng đại**: um_pb_02 p 0.003 → 0.032 dưới block bootstrap (crypto returns tự tương quan)
2. **OOS-select không đủ**: chọn trên nó = nó không còn vô tư. Chỉ lockbox mới là trọng tài
3. **Biên split rò rỉ**: label window xuyên biên → đã purge/embargo ([[decisions]] #16). Capitulation sống sót purge (+0.72R, p=0.009) = edge thật

**Chuỗi phán quyết chuẩn:** OOS đẹp → *nghi ngờ* · lockbox giữ → *hứa hẹn* · forward-test live giữ → *mới được tiền thật*. um_pb_02 đang chết ở bước 3 (23 lệnh, win 8.7%) — hệ làm đúng việc, không tốn xu nào.

Liên quan: [[validation-pipeline]] · [[auto/graveyard]] · [[decisions]]
