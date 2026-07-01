# Trading Agent — Trạng thái hệ thống (Demo)

*Cập nhật: 2026-07-01 · Chế độ: PAPER / MÔ PHỎNG (không dùng tiền thật)*

## Tóm tắt 1 dòng

Một **agent giao dịch futures tự động** chạy trên **dữ liệu thị trường thật**, tự
phân tích, **tự vẽ chart cho mỗi lệnh**, có dashboard theo dõi trực tiếp — và
**tuyệt đối chưa đụng tiền thật** cho tới khi chứng minh được lợi nhuận.

## Xem demo

- **Link cho sếp (Cloudflare, xem từ xa):**
  `https://evening-expand-deaf-demonstrated.trycloudflare.com/#token=Demo-cJ5cnhlXiUqgcy2S5OEubRzQ2mcqH4_7`
  *(mở nguyên link kèm `#token=...` — token cho phép xem dashboard qua tunnel. Link trycloudflare đổi mỗi lần khởi động lại.)*
- **Local:** http://127.0.0.1:8090
- **Khởi động:** `python start_demo.py` · **Dừng:** `python start_demo.py --stop`
- **Mở tunnel:** `cloudflared.exe tunnel --url http://127.0.0.1:8090` (in ra link + token)

## ĐÃ CHẠY ĐƯỢC (kiểm chứng)

| Hạng mục | Trạng thái |
|---|---|
| **An toàn tiền thật** | 🔒 Khóa cứng (fail-closed). Mọi lệnh live bị chặn ở tầng đặt lệnh, kể cả script tay. Chỉ mở khi set biến môi trường rõ ràng — hiện KHÔNG set. |
| **Dữ liệu thật** | Nến OHLCV đóng thật từ Binance, không bịa. Quyết định + thoát lệnh đều trên dữ liệu thật, không rò rỉ tương lai. |
| **Mô hình chi phí thật** | Phí, spread, slippage (theo thanh khoản), funding, thanh lý — tính sát thực tế (thiên về thận trọng). |
| **Agent tự vẽ chart** | Mỗi lệnh sinh 1 biểu đồ PNG: nến + EMA20/50 + volume + SL/TP. Xem được trên dashboard. |
| **Nhiều lớp quản trị rủi ro** | Circuit breaker, giới hạn lỗ ngày, throttle drawdown, cổng kỳ vọng dương, kiểm tra thanh khoản. Agent **từ chối** vào lệnh xấu. |
| **Dashboard trực tiếp** | Vốn, vị thế, lịch sử lệnh, chart từng lệnh, tình trạng các agent nền. |
| **Kiểm thử** | 832 bài test tự động pass. Mỗi thay đổi đều có test + kiểm tra độc lập. |

## ĐANG KIỂM CHỨNG (chưa xong)

| Hạng mục | Trạng thái |
|---|---|
| **Chiến lược có lợi nhuận** | **CHƯA CÓ.** Đã kiểm chứng nghiêm ngặt setup EMA + volume + pullback trên 9 coin × 9 tháng dữ liệu thật (khung 5m, 1h, 4h), với holdout niêm phong. Kết quả: **chưa có lợi thế** (kỳ vọng âm sau chi phí). Đây là kết luận trung thực — hệ thống **không** giả vờ có lãi. |
| **Hướng tiếp theo** | Kiểm chứng các phương pháp chart khác (SMC, order block, khung cao hơn, vào lệnh maker để giảm phí) — mỗi phương pháp phải vượt qua đúng cỗ máy backtest này trước khi được phép giao dịch. |

## Nguyên tắc (vì sao tin được)

1. **Không hứa ngày ra lãi.** Chỉ báo cáo cái gì đo được.
2. **Chứng minh hoặc loại bỏ.** Mỗi chiến lược phải có lợi thế thống kê thật trên
   dữ liệu chưa từng thấy, nếu không → loại, không vặn số cho đẹp.
3. **Không tiền thật cho tới khi đủ bằng chứng.** Live bị khóa mặc định.

## Con số demo hiện tại

- Tài khoản mô phỏng: **$100** (demo tươi)
- Vài lệnh demo mẫu (dữ liệu + chi phí + chart THẬT, gắn nhãn demo) để minh hoạ
  luồng vào/thoát lệnh và biểu đồ. *(Các lệnh này lỗ nhẹ — phản ánh đúng việc
  chiến lược hiện tại chưa có lợi thế; hệ thống báo cáo trung thực.)*
- Lịch sử thật trước đó: ~950 lệnh mô phỏng đã lưu (dùng để phân tích, không phải quảng cáo lãi).

## Repo

Mã nguồn: https://github.com/Bfat-6z/trading-agent (công khai để review kiến trúc).
