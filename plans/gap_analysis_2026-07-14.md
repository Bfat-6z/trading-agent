Skill: none — synthesis/ranking task, no code edits or debugging involved.

# Build Plan hợp nhất — Gap Audit 4 chiều (signal-bot / mission-loop / risk-prop / ops-data)

## 1. Executive Summary — 3 gap quan trọng nhất

**① Signal layer mù hoàn toàn về trạng thái prop account (gộp 5 finding).** Mỗi signal size độc lập ($50 risk → notional không clamp), không đếm số kèo đang mở, không biết hôm nay đã thua bao nhiêu, không cảnh báo 3 flush-LONG cùng cycle = một cược BTC-beta duy nhất. Kịch bản cháy cụ thể: ngày đang −$160, flush bắn thêm signal full-size, SL hit → thủng $200 daily cap → challenge $5K fail **bởi chính signal mà bot đủ dữ liệu để chặn**. Đây là cluster duy nhất chạm trực tiếp tiền thật và owner đã approve daily prop tracker — làm đầu tiên.

**② Signal không "follow được": mọi thứ sau entry là vô hình.** Paper book trail SL về BE, đóng sớm, đặt/hủy LIMIT — owner không nhận được gì. Hệ quả kép: (a) owner ôm stop gốc trong khi paper đã thoát hòa → ăn nguyên −$50 trên trade hệ thống tính là scratch; (b) LIMIT signal chỉ bắn **sau khi** paper đã fill → owner chỉ fill khi có retest, tức là hụt đúng những cú flush-bounce là edge duy nhất đã confirm. Win-rate footer đang quảng cáo một track record mà người theo tay **về mặt cấu trúc không thể tái tạo**. Owner đã approve close-notifications — mở rộng thành full lifecycle (place → fill → mgmt update → close/cancel).

**③ Learning loop hở ở đúng chỗ tiền chảy nhiều nhất: flush mech auto-fire trên verdict đóng băng + ledger không có provenance.** Path bắn tự động 3 lệnh x10/cycle dựa trên hardcoded string "CONFIRMED", không có disarm condition, calibration còn exclude mech rows. Đồng thời closed ledger không stamp model/mode/tide → mọi câu trả lời "có edge không" (lý do tồn tại của cả project) bị confound qua 3 model era. Stamp provenance là vài dòng code nhưng làm mọi A/B tương lai khả thi hồi tố — làm ngay kẻo mất thêm data-days.

Nguyên tắc xuyên suốt: **không thêm resident process nào** (máy đói RAM) — mọi thứ piggyback vào loop/supervisor/scheduled-task sẵn có.

## 2. Bảng ranked đầy đủ

| # | P | Effort | Item | What + Why (1 dòng) |
|---|---|--------|------|---------------------|
| 1 | P0 | S | **Telegram sender hardening** | Log mọi send fail + error_code, honor `retry_after` (429), stop retry 4xx permanent, fallback plain-text khi HTML parse fail — nền móng cho mọi item Telegram phía dưới; hiện tại token revoked = im lặng vĩnh viễn. |
| 2 | P0 | M | **Prop risk state layer** (gộp: cap-suppression + aggregate exposure + margin clamp + cluster warning) | emit() gọi `_prop_day_est()` trước khi bắn KÈO MỚI (chặn/red-flag khi est ≤ −$150), in "kèo đang mở: N, tổng risk $X", clamp notional/margin vs $5K, đếm same-direction alt-LONG — chặn đúng kịch bản cháy challenge trong 1 nến. |
| 3 | P0 | M | **Position lifecycle notifications** (mgmt/close — owner đã approve) | Detect thay đổi `p['mgmt']`/trail/BE/early-close per pos_id → gửi message UPDATE mới (lưu message_id để reply-thread, KHÔNG build editMessageText); hết cảnh owner ôm stop gốc trong khi paper đã hòa vốn. |
| 4 | P0 | M | **LIMIT placement + cancel signals** | Bắn signal tại thời điểm vào PENDING book ("resting limit tại X, valid ~2h") + notice khi cancel/expire — để lệnh tay của owner nằm trong book cùng lúc với paper, thay vì fill-sau-retest hụt hết flush edge. |
| 5 | P0 | S | **Provenance + tide stamping** | Stamp `model / pipeline_mode / decision_path(vision‖numeric_fallback) / tide_at_entry / tide_aligned` vào open+closed rows — vài dòng, mở khóa mọi phân tích era/compliance về sau; mỗi ngày chưa stamp là data-day mất trắng. |
| 6 | P0 | M | **Flush edge-decay monitor + auto-disarm** | Job so rolling mission-realized mean-R của `flush_*_mech` vs shadow expectation, dưới ngưỡng → disarm + alert (chạy trong cycle sẵn có) — path auto-fire to nhất không thể chạy vô hạn trên verdict hardcoded. |
| 7 | P0 | S | **Backup brain.db + events.jsonl** | Scheduled task (Windows Task Scheduler, không daemon): sqlite `.backup()` + copy events.jsonl/chain_head sang ổ khác/cloud, half-day; git hóa fleet_watchdog.ps1/run_*.bat — 648 trials + hash-chain hiện là single-copy trên 1 ổ đĩa, mất là mất bằng chứng không replay được. |
| 8 | P0 | S | **Ops alert drain qua Telegram sẵn có** | Piggyback vào llm_trader loop hoặc supervisor tick: drain `alert_outbox.jsonl` → sender của whale_signal (sau item 1) — vụ supervisor wedge 77 phút + quarantine 6h đã xảy ra mà không ai biết. |
| 9 | P1 | S | **Signal timestamp + expiry + sequence ID** (gộp 2 finding) | In giờ signal + "hết hạn sau X phút", skip row cũ hơn ~2 cycle khi send, đánh số "kèo #N" + note "lệnh trước trên coin này đã đóng" — chặn stale-flood lúc onboard và nhầm re-fire với duplicate. |
| 10 | P1 | S | **Max-DD $300 cumulative tracker** | Mở rộng prop layer (item 2): running peak/trough cross-day, cảnh báo khi tiến gần −$300 và đưa vào /status — Two-Step chết vì bleed đa ngày mà daily tracker không thấy. |
| 11 | P1 | S | **Memory recency decay / era windowing** | Half-life decay hoặc filter theo era-tag (cần item 5) trong `aggregate_stats`/`distill_lessons` — model 5.6-sol đang bị dạy bằng track record của degen-era 5.5 đã chết, unwindowed lessons = anti-learning. |
| 12 | P1 | M | **Stage-2 rejection shadow ledger** | Persist full bracket khi REJECT, replay bằng resolve math sẵn có (cùng pattern shadow_trigger_eval), thêm by_stage2 vào calibration — stage-2 giết phần lớn proposals mà chưa ai biết nó cộng hay trừ EV, và rejection-memory đang compound lỗi chưa đo. |
| 13 | P1 | S | **Per-process memory watchdog trong supervisor** | Supervisor đã enumerate PID per agent → sample WorkingSet mỗi tick, alert/restart khi vượt ngưỡng (KHÔNG process mới) — leak 750MB đã giết mission một lần, 16GB RAM không có lần hai miễn phí. |
| 14 | P1 | M | **Log rotation thật sự chạy** | Scheduled task gọi retention_policy/archive sẵn có + truncate nguồn (whale_flow 559MB, micro 486MB...); sửa shadow_trigger_eval đọc incremental (offset) thay vì full-file — chặn cả disk-fill lẫn heartbeat-blown → kill-loop giả. |
| 15 | P1 | S | **Universe listing screen tự động** | Job diff Binance perp listing vs known bases + heuristic RTH-volume/weekend-ATR, alert base ticker lạ — tokenized stocks ăn ~50% lane loss và rename (SKHY) lách blacklist tay. |
| 16 | P1 | M | **Discretionary aggregate exposure cap** | Cap tổng notional (hiện cho phép ~23x equity) + same-side concentration trên path LLM (mech đã có cluster cap) — portfolio gap-tail là đúng kiểu ruin HMSTR, và concentration này export thẳng ra prop signal. |
| 17 | P2 | S | **MTM check trong daily breaker** | Cộng unrealized của open positions vào check −15% (vài dòng trong lr.daily_breaker) — flush day 12 lệnh −8% unrealized mà breaker báo "ok" là lỗ hổng thật nhưng paper-only nên xếp sau. |
| 18 | P2 | M | **Board-pass counterfactual scoring** | Batch job join picks → N-bar returns, so vs activity-fallback + random-6 — selection stage 0 chưa đo, nhưng chờ cost-efficient redesign ổn định pipeline đã rồi hãy đo kẻo đo xong lại đập. |
| 19 | P2 | S | **Daily health digest qua Telegram** | 1 message/ngày (fleet state, incidents, paper PnL, verdict changes) compose trong daily_exam sẵn có — thay thế vòng lặp "mở Claude session hỏi" đang đốt $200 sub. |
| 20 | P2 | S | **News-path direction fix trong shadow eval** | `_direction()` trả None cho 'news' → mọi news-fire rơi khỏi đo edge; fix nhỏ để path này ít nhất đo được thay vì vô hình. |

**Thứ tự thi công đề xuất:** 1 → 2 → 5 → 7 (tuần đầu, toàn S trừ 2) → 3 → 4 → 6 → 8 → rồi P1 theo bảng. Item 5 và 7 làm sớm dù không "gấp" vì chi phí trì hoãn tích lũy theo ngày (data không stamp / không backup là mất vĩnh viễn).

## 3. DO-NOT-BUILD — chủ động bỏ

| Bỏ | Lý do |
|----|-------|
| **editMessageText / sửa message gốc** | Message UPDATE mới + reply-thread (lưu message_id) đạt 95% giá trị với 20% độ phức tạp; edit-in-place dễ silent-fail (message quá 48h, parse error) đúng kiểu lỗi item 1 vừa phải vá. |
| **Full latency/duration instrumentation + per-cycle thinking archive** | Speculative: chưa có quyết định nào chờ số liệu này, pipeline đang bị redesign vì cost — instrument xong lại vứt. Nếu cần realism, stamp 1 field `price_age_s` lúc fill trong item 5 là đủ (gần free). |
| **Cancelled-limit counterfactual replay** | Defer, không kill hẳn: chờ stage-2 shadow (item 12) dựng xong framework replay + chờ limit-doctrine sống sót qua redesign. Build bây giờ = M effort cho doctrine có thể bị xóa. |
| **Backfill lịch sử cho chart_align / whale / funding_extreme** | Forensic đã cho thấy các path này gần chết; đổ M effort backfill cho trigger sắp bị KILL là ngược ưu tiên. Để verdict engine tự tích n_live; chỉ fix news-direction (item 20) vì rẻ. |
| **Drawdown-adaptive RISK_PCT (tự co 1%→0.5%)** | Sizing tự động chưa prove được trên chính paper book; với prop, **halt/cảnh báo** (item 2+10) an toàn và dễ audit hơn logic co giãn — thêm degree-of-freedom chưa có bằng chứng = thêm chỗ bug. |
| **Bất kỳ resident process mới nào** (alert daemon, digest daemon, watchdog process riêng) | Máy đói RAM, leak đã giết mission một lần. Mọi item trên đều piggyback: supervisor tick, llm_trader cycle, daily_exam, Windows Task Scheduler one-shot. |
| **Signal-side auto-position-management đầy đủ (bot tự tính partial TP, pyramid cho owner)** | Owner trade tay; ticket càng phức tạp càng khó thi hành đúng. Giữ signal = entry/SL/TP/update rõ ràng, phần còn lại là kỷ luật người. |
| **Sửa fail-open kill_switch / equity-wipe từ bughunt cũ trong lane-farm** | Không nằm trong 4 dimension này và lane-farm là paper sandbox đang có plan Tier1-5 riêng (plans/bughunt_2026-07-08.md) — đừng trộn scope. |