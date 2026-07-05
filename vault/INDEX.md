---
tags: [moc]
updated: 2026-07-06
---

# 🧠 Trading Agent — Second Brain (INDEX)

> **Cho Claude (em):** đọc file này ĐẦU PHIÊN khi làm việc ở trading-agent — nó là bản đồ context nhanh nhất.
> **Cho sếp:** mở folder `E:\keo-moi-mail\trading-agent\vault` trong Obsidian làm vault → graph view thấy toàn bộ.

## Bản đồ
- [[system-map]] — kiến trúc toàn hệ: file nào làm gì, data chảy thế nào, invariants CẤM vi phạm
- [[mission]] — mission $100→$1000: trạng thái honest hiện tại
- [[decisions]] — nhật ký các quyết định lớn + LÝ DO (đọc trước khi định "cải tiến" gì)
- [[validation-pipeline]] — cách một method được sinh → kiểm → arm (novelty gate, lockbox, block bootstrap)
- [[sizing]] — mech_sizing: Kelly + haircut + crisis-correlation
- [[codex-plugin]] — playbook dùng Codex review (đã fix 1312, file-access OK)

## Số liệu sống (auto-render từ brain.db — ĐỪNG SỬA TAY)
- [[auto/armed]] — method đang được đánh tiền mission
- [[auto/lessons]] — 6 bài học pre-registered + trạng thái 3 bậc
- [[auto/graveyard]] — nghĩa địa idea chết (chống re-test)
- [[auto/trials-stats]] — bộ đếm DSR

## Luật bất biến (tóm tắt — chi tiết trong [[system-map]])
1. **PAPER-ONLY vĩnh viễn** — live LOCKED, không bao giờ gọi order thật.
2. **LLM không được GHI vào ground truth** (brain.db/events.jsonl) — chỉ đọc + đề xuất vào quarantine.
3. **trials append-only** — xoá 1 trial chết = thổi phồng mọi p-value tương lai.
4. **Chỉ arm method qua LOCKBOX** (data chưa từng dùng để chọn). OOS đẹp mà rớt lockbox = overfit (S_QUIET_BEAR_COIL là bằng chứng).
5. **Luôn dùng Codex plugin review** cho thay đổi lớn (lệnh sếp, $0 qua 9router).
6. Leverage x5/x10, cache/install trên E:, secrets gitignored, không model haiku.
