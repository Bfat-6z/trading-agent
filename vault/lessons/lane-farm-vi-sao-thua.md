---
tags: [lesson, core, lane-farm]
source: workflow lane-loss-lessons (7 agent, 103 lane-row, brain.db trade_autopsy)
window: 2026-07-06 → 2026-07-11 (5 ngày, 1 regime)
---

# Lane Farm — vì sao 30 chiến lược này thua

Mổ 30 lane âm (mỗi lane = 1 tài khoản $100 test đúng 1 setup) từ `trade_autopsy` trong brain.db. 7 agent chạy song song, 103 lane-row. Đây là bài học chéo-kênh — **không phải "tìm signal xịn hơn" mà là fix cấu trúc entry/exit/universe**.

## Lý do LỚN NHẤT (một câu)

Cả 30 lane thua vì **trigger đơn-điều-kiện bắn NGAY tại nến exhaustion/extension, ghép stop cố định ~1R KHÔNG chuẩn hóa theo volatility**. Win% chỉ 27–45% trong khi mỗi stop đã ăn phí thành **−1.04 đến −1.10R**.

Bằng chứng quyết định: winner vs loser tách nhau gần như hoàn toàn ở đúng **2 feature vào lệnh — `atr_pct` và khoảng cách tới EMA200** — còn feature của chính trigger (rsi, ret) thì winner/loser **y hệt nhau**. Nghĩa là: **trigger không mang thông tin hướng đi; nó chỉ chọn đúng điểm đảo chiều để mình vào sai.**

Cộng 2 khoản thuế cấu trúc:
- **Stop-drag:** −1.05R × tỷ lệ stop 40–73% = chảy máu ngay cả khi vào ngẫu nhiên → đó chính là **sàn L00_random −0.41R**.
- **Universe bẩn:** ~50–60% tổng lỗ đến từ **perp cổ phiếu/hàng hóa kém thanh khoản** (SKHYNIX, SNDK, SOXL, SAMSUNG, MU, INTC, DRAM, SPCX, EWY, XAU/XAG, CL/BZ) — gap, mean-revert, up-drift cấu trúc, KHÔNG có continuation kiểu crypto.

## 9 cơ chế thua (rank theo blast radius)

1. **Fixed 1R stop không vol-normalized → noise-stop** (~15 lane). LOSER có `atr_pct` gấp **1.3–2×** WINNER (mo_breakout20 loser 1.21 vs win 0.60). Bằng chứng là stop-width chứ không phải sai hướng: **timeout survivors DƯƠNG** (+0.07→+0.36R), TP trả +1.5→+2.5R. → Stop theo **ATR/structure (~1.5–2× ATR hoặc dưới swing)**, KHÔNG xóa signal, chỉ sửa bracket.

2. **Chase-extension trên momentum LONG** (~7 lane, nhóm cứu được nhất). LOSER `px_vs_ema200` 4–6.8% vs winner ~1–3%. `mo_ema_stack_trend`: loser 6.83 vs win 3.28 (separator sạch nhất). → **Trần extension**: bỏ qua nếu `px_vs_ema200 > 3–4%` hoặc `rsi > 68` hoặc `atr_pct > 1.1`. Chỉ mua stack khi giá còn SÁT EMA / first pullback.

3. **Short EXHAUSTION / vào muộn khi đã oversold** (~8 lane, nhiều lane dưới sàn random). Vào khi `dd96 −5→−10%`, rsi 30–44. Stop **rất nhanh: median 2–3.5 bar**; owner_4h 76% stop trong ≤3 bar. → **Cấm short continuation khi `rsi<40` / `dd96<−6%`.** Chỉ short bear-state như **fade-the-bounce** sau khi hồi về resistance/lower-high.

4. **Vào trên chính nến trigger (ignition/reclaim/breakout) = false break** (~6 lane). L_SQ_IGN & S_SQ_IGN fail **y hệt nhau** → volume-expansion KHÔNG predictive về hướng. 58% stop tại −1.10R (slippage nến ignition). → Vào ở **retest/hold** (close giữ level 2–3 bar), không phải nến bùng nổ.

5. **Timeout-bleed trong tape low-vol/neutral** (~5 lane). tiktok_triangle: 68% timeout ~0R, **1 TP/60**. `atr 0.5–0.9%`, `vol_ratio≈1`, `ret20≈0`. → Gate bắt buộc **vol_ratio>1.3 + directional regime** (px>EMA200, ret20 cùng chiều). Triangle trên gold/silver/RWA → kill thẳng.

6. **Payoff geometry gãy — TP quá xa so với horizon** (~5 lane). mo_ema_stack_dn **0/48 TP**, mo_roc_dn 2/61. Timeout survivors dương nhưng TP +2.46R gần như không chạm trong 15–24 bar. → Kéo TP về **reachable (~0.8–1R)** hoặc **trail** để bank survivor drift. Lỗi bracket, sửa exit trước khi phán signal.

7. **Universe contamination** (~50% tổng loss). ~13 ticker perp cổ phiếu/hàng hóa nuốt gần nửa lỗ. SKHYNIX một mình −0.128 trên mo_roc_dn. 10/16 lane Cluster 6 là pure SHORT đè lên đúng nhóm up-drift. → **Blacklist universe về crypto thanh khoản** — 1 filter xóa gần nửa chảy máu, độc lập chất lượng signal.

8. **Fade strength KHÔNG có exhaustion trigger** (~4 lane). rsi_overbought_fade (n=104, −0.063): winner/loser feats **y hệt** (rsi 75/75) — RSI-overbought/divergence/capitulation KHÔNG phải tín hiệu đảo chiều. → Yêu cầu **exhaustion trigger cụ thể**: bearish structure break / lower-high / CVD divergence / volume climax-and-fade.

9. **Short "quiet" low-volume drift** (~3 lane). `vol_ratio 0.54–0.61` (dưới trung bình) → không có seller conviction → squeeze stop trong ~4 bar. → **Cấm short khi `vol_ratio<0.7`**; breakdown short cần volume EXPANSION.

## Kill benchmark (dùng cho method lab)

**L00_random = sàn alpha −0.41R / 73% stop.** Lane nào meanR không **rõ ràng vượt −0.41R** = KHÔNG có edge → **KILL, không tune.**

Đang ở/dưới sàn (giết): `M15_MOM_BEAR −0.564`, `mo_breakdown20 −0.529`, `S_SQZ_FLAT_DROP −0.46`, `mo_ema_stack_dn −0.453`, `owner_4h_bear −0.436`, `L_MOM_200_RECLAIM −0.420`.

Lane đáng CỨU (survivor drift dương + TP thực, sửa bằng extension gate + ATR stop, không xóa): **ema_stack_long (−0.10)**, **mo_ema_stack_trend (−0.025, separator sạch nhất)**, **rsi_dip_in_uptrend (−0.029 nhưng trend gate mislabel: ret20 âm cho cả win lẫn loss)**, **mo_roc_up (−0.16)**, **momo_breakout_long (−0.05)**, **momo_breakdown_short (−0.043)**.

## Caveat độ tin cậy (BẮT BUỘC đọc)

- Data chỉ **5 ngày (2026-07-06→07-11), MỘT regime**, n=25–113/lane.
- `mfe_pct/mae_pct` NULL → phân biệt noise-stop vs thesis-wrong là **suy luận từ exit-mix + entry_feats**, không đo trực tiếp.
- `funding_z = 0.0` mọi row → **feature funding chết, cần fix** (microstructure không vào được autopsy).
- Mọi verdict là **low-confidence, dễ overfit một tuần** → forward-test trước khi cấp vốn hay đổi mission.

## Cách áp vào bot (2 nhóm hành động)

**Fix cấu trúc (áp cho TẤT CẢ lane + mission, độc lập signal):**
1. Stop ATR/structure thay fixed-1R.
2. Universe blacklist perp cổ phiếu/hàng hóa (giữ crypto thanh khoản — trùng luật [[universe-dao-roi]] floor $50M).
3. TP reachable / trail thay fixed 1.5–2.5R.

**Fix per-signal (chỉ nhóm cứu được):** extension gate (px_vs_ema200 / rsi / atr_pct ceiling) + entry ở retest/hold thay nến trigger.

Liên quan: [[universe-dao-roi]] · [[llm-khong-co-edge]] · [[sizing]] · [[decisions]] · [[validation-pipeline]]
