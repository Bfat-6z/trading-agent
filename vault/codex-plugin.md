---
tags: [reference, tooling]
updated: 2026-07-06
---

# Codex Plugin Playbook (openai/codex-plugin-cc)

**Luật sếp: LUÔN dùng Codex review cho thay đổi lớn.** $0 qua 9router. Đã bắt 10+ bug thật qua 3 vòng.

## Trạng thái: FULL file-access (1312 đã fix 2026-07-06)
Fix: `~/.codex/config.toml` `[windows] sandbox = "elevated"` → **"unelevated"** (elevated không tạo được logon session dưới sandbox của Claude Code → CreateProcessAsUserW 1312).

## Cách dùng đúng
```bash
# Review đọc repo thật (ưu tiên sau fix):
codex -c 'model_reasoning_effort="medium"' exec -s read-only "$(cat promptfile)" < /dev/null
# Companion (background jobs):
node <companion.mjs> adversarial-review --background --base <ref> "focus..."
node <companion.mjs> task --background --model cx/gpt-5.5 --effort xhigh "prompt"
```
Companion: `C:/Users/ACER/.claude/plugins/cache/openai-codex/codex/1.0.5/scripts/codex-companion.mjs`

## Quirks (trả học phí rồi)
- Model PHẢI prefix `cx/` (gpt-5.5) hoặc `cc/` — bare name → provider openai → 404
- `codex exec` phải `< /dev/null` (không nó chờ stdin); prompt dài → để trong FILE (PowerShell Start-Process bẻ quoting)
- `cancel` hỏng dưới git-bash → kill process codex.exe/node.exe; kill app-server giữa job → status file zombie "running" → xoá `%LOCALAPPDATA%/Temp/codex-companion/<proj>/jobs/`
- xhigh + diff to = 15-30+ phút → chạy nền, poll
- Review gate stop-time: WORK nhưng để OFF (nặng cho phiên chat); bật: `setup --enable-review-gate`
- Chưa dùng: `task --write` (Codex tự sửa code), `rescue` subagent, `transfer` — sẵn sàng khi có việc phù hợp
