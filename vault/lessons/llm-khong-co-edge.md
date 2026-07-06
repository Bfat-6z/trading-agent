---
tags: [lesson, core]
---

# Bài học gốc: LLM tự nghĩ KHÔNG có edge

**Đo được, không phải ý kiến:** LLM discretionary ("coin này nhìn ngon, vào!") chấm scorecard **p=0.9998** = không hơn tung xu, trừ phí thì âm. 280-file NeuroCore "self-thinking" audit ra phần lớn là **kịch**: synthetic data, fake exits, learning loop chết.

**Hệ quả thiết kế** (xem [[system-map]]):
- PROVEN_ONLY: bot chỉ fire theo method đã qua [[validation-pipeline]]
- LLM chỉ được **SINH giả thuyết + PHẢN BIỆN** — không bao giờ quyết lệnh, không bao giờ ghi ground truth
- Vòng `_reflect()` (bot tự viết niềm tin rồi tự tin theo) = memory laundering → đã giết ([[decisions]] #9)

**Giá trị thật của LLM trong hệ:** đẻ candidate cho lab (rẻ, đa dạng) + Codex adversarial review (bắt 10+ bug thật). Đúng vai = quý; sai vai = đốt tiền.

Liên quan: [[overfit-va-lockbox]] · [[decisions]]
