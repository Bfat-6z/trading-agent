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

16. **Purge/embargo tại biên split** (harvest playbook #2): label window từng xuyên biên → train peek OOS, OOS peek lockbox. Vá xong; **capitulation SỐNG SÓT purge** (lockbox +0.61→0.72R, p≈0.009-0.012) = edge thật, không phải leak. Block bootstrap không chữa được leak loại này — purge là bắt buộc.
17. **Family exit-grid = SUPERSET, không bao giờ bớt** — Run B bác thuyết "MR cần timeout chặt" cho capitulation (TO8 sập lockbox). Grid theo family chỉ THÊM exit để test, data quyết. Armed params SL1/TP6/TO48 tái xác nhận (Run C).
18. **DSR 2 mẫu số** (registry ~705 + lockbox-exposed): cột thông tin, không auto-tước. capitulation lb_p_defl ≈ 0.13-0.17 = "hứa hẹn đã xác nhận 2 lần", chưa phải chắc chắn — nói thật với chính mình.
19. **Harvest có não ≠ harvest cũ**: 51/51 candidate mới thật (0 rebrand nhờ nghĩa địa trong prompt) nhưng 0 robust — sweep-reclaim/SMC folklore thua đo được trên nghìn lệnh. Giá trị thật của vòng cào = playbook engineering, không phải method.

## Đang mở (chưa quyết)
- um_pb_02: lockbox $15M giữ (p=0.007) nhưng $50M mẫu mỏng (n=60) → forward-test phán.
- Baseline-relative lesson promotion (Codex đề xuất) — chờ mẫu lớn hơn.
- Behavioral/correlation hash chống semantic-dup — blueprint item #6.

## 2026-07-06 (vòng winrate)
20. **ARM wr_flush_notknife (đứng trước capitulation).** Nguồn gốc: harvest 10-agent + mining 1.104 fires của chính mình. Cơ chế: đòi climax thật (bar_z≤−2) + chặn sập-giai-đoạn-cuối (dd96≤18%). Validation fixed-exit (kế thừa SL1/TP6/TO48 — zero exit-multiplicity, purged): lockbox +1.16R net+80% p=0.0002, cả 2 segment dương, sống Šidák toàn-lockbox-exposure. Win 45.8% vs cha 32.9%, mean 6×. Bài học đối chứng: folklore web (Connors-uptrend, nến búa) chết/mâu thuẫn — data mình + lockbox là trọng tài duy nhất. bear_noasia + deep15 → forward-watch.

## 2026-07-09 → 07-10 (vòng full-trust + verdict + redesign)
21. **FULL-TRUST experiment (lệnh sếp "đừng bảo thủ QUÁ"):** gỡ choppy/wick/chase/vol gates, bật DISCRETIONARY (PROVEN_ONLY 0), gpt-5.5 vision 3 khung, giết timeout cho lệnh discretionary. Giữ NGUYÊN: LAW x5/x10 + 5-10% + paper-LOCKED, gap-veto, daily-breaker. Đây là thí nghiệm ĐO có chủ đích, không phải buông.
22. **VERDICT n=81 (2 vòng adversarial Opus): discretionary 15m KHÔNG CÓ EDGE — quyết bằng số.** WR 14.8% vs breakeven 64.6% (payoff 0.55); KHÔNG subset +EV; 96.7% lỗ = sai HƯỚNG entry (fee/funding chỉ 3.3%); reason-proxy: 54/57 loss mid+major là `sl` full, 0 `trail` → **THESIS-WRONG** (entry sai từ đầu, không phải noise-stop). Bẫy phân tích đã né: brain.db `trade_autopsy` chỉ giữ ~40 row gần nhất (subset MÉO — kết luận đầu sai vì nó); ledger thật = `closed.jsonl` n=109. "Micro oversizing 5.7×" của reviewer = artifact chronology (micro đánh sớm khi equity còn $100). → P0 (MFE/MAE trong resolve) + P1 (calibration_report + progress.jsonl) shipped để xác nhận split chính xác trên close mới.
23. **REDESIGN owner-approved: TIN + CHART two-pass** (spec `plans/redesign_tin_va_chart_v1.md`). Sếp chọn: (1B) chạy tiếp lấy P0; (2) "vừa theo tin vừa theo chart, nhìn cả 3 khung → chọn khung → nhìn LẠI khung đó rồi mới quyết". Kiến trúc: trigger paths TRONG CODE — model đã chứng minh lờ prompt-rule (news/whale/funding_extreme/flush_oi_dn/flush_no_oi/chart_align) → stage-1 quét 3 khung → stage-2 second-look khung đã chọn (model REJECT = bỏ lệnh; lỗi kỹ thuật = pass-through có tag, tránh bẫy 0-trades) → mỗi lệnh dán `trigger_paths`+`stage2` → đo per-path, kill path bleed ở n≥20. Whale data contract `shadow_only` được tôn trọng (context-only tới khi proven).
24. **R1 (đo ngầm, LIVE) + R2 (gate+two-pass, DARK sau `state/llm_trader/redesign.flag`) shipped cùng ngày** — 3 vòng Opus review (SHIP; flag-off bit-for-bit identical; LAW un-weakenable bởi stage-2). OI probe hang-proof tách `flush_oi_dn` (setup +EV duy nhất, dòng dõi clf_oi_dn) đo live ngay từ dark mode. Flip = touch flag file + respawn mission (verified: env-only sẽ đòi restart cả fleet — WMI-fragile). Ngưỡng tune 1 LẦN trên ≥24h trigger_log rồi FREEZE vào code (bài học Šidák). Sơ bộ 0.7h: chart_align nhận ~nửa vũ trụ (~1237 candidates/ngày) → tune sẽ siết mạnh. Panel "Hệ đo mới · tin+chart" trên horizon-ui cho sếp tự theo dõi. Bug bắt sống trong vòng: pytest ghi đè heartbeat PROD qua mid-cycle _hb của stage-2 (false-fresh có thể che mission chết) → cách ly bằng autouse fixture.
