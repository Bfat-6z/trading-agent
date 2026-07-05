---
tags: [log]
updated: 2026-07-06
---

# Nhật ký quyết định lớn (+ VÌ SAO)

> Append-only. Đọc trước khi muốn "cải tiến" — nhiều thứ trông như thiếu sót thực ra là quyết định có chủ đích.

## 2026-07-05
1. **Sàn firing $5M → $50M.** Mission tụt −$30 vì capitulation bắt dao rơi trên coin rác (POWER/IN/ESPORTS). Edge dương trung bình che phương sai per-coin. → chỉ đánh coin liquid.
2. **PROVEN-ONLY.** LLM discretionary đo được p=0.9998 = không edge. Bot chỉ fire theo method đã validate.
3. **Bỏ phanh ngày** (lệnh sếp, chấp nhận rủi ro). Backstop còn lại: sizing + $50M floor + cap notional 3×.
4. **Sizing = empirical Kelly** thay hardcode ([[sizing]]). Fable-5 review: binary-Kelly cũ nguy hiểm (mù tương quan).
5. **LOCKBOX là trọng tài tối cao.** S_QUIET_BEAR_COIL: OOS p=0.0004, net +423% → lockbox −139%, p=0.91 = OVERFIT, disarm. capitulation giữ lockbox cả 2 universe → armed duy nhất.
6. **Block bootstrap thay IID.** um_pb_02 p 0.003 → 0.032 dưới block = IID phóng đại 10×.
7. **C_DD 0.5 (half-Kelly), cap 3×** (lệnh sếp "đánh bé quá"). Không lên 0.7+ khi chưa có bằng chứng live.

## 2026-07-06
8. **Second brain = SQLite, KHÔNG vault markdown cho số liệu.** 15-agent research: narrative memory cho ground truth = memory laundering. LLM cấm ghi brain.db.
9. **Giết `_reflect()`** — bot tự viết directive rồi tự tin theo = laundering loop kinh điển.
10. **Novelty gate chặn cả FLAG_NEAR trên đường LLM** (nudge threshold = đường lách chính). A/B chủ đích đi đường ingest tay.
11. **Lessons 3 bậc** (candidate/advisory/active). Hard veto cần eff_n≥12 cụm (symbol,ngày) + mission_n≥3 âm. Shadow-only không bao giờ được veto mission. forward_test cố ý KHÔNG lesson-gate = probe stream chống tự-khoá.
12. **Cắt 5 agent theater** (dream_cycle, self_model, skill_forge, reflection, memory_consolidation) — 0 dependency từ mission path. Bài học: phải restart supervisor sau khi sửa specs.
13. **Fix Codex 1312:** `[windows] sandbox = "elevated"` → `"unelevated"`. Review file-access đầu tiên bắt 4 bug context-fed bỏ sót → từ nay review lớn phải cho Codex ĐỌC repo.
14. **MCP server hoãn** — brain_query.py CLI cover 100% nhu cầu, 0 dependency.
15. **Obsidian vault này** = narrative layer cho Claude + view cho sếp. Số liệu vẫn chỉ ở brain.db, vault/auto/ là RENDER read-only.

## Đang mở (chưa quyết)
- um_pb_02: lockbox $15M giữ (p=0.007) nhưng $50M mẫu mỏng (n=60) → forward-test phán.
- Baseline-relative lesson promotion (Codex đề xuất) — chờ mẫu lớn hơn.
- Behavioral/correlation hash chống semantic-dup — blueprint item #6.
