"""CÁ VOI TẬP SỰ — Telegram signal bot (owner 2026-07-13).

The mission (llm_trader) decides paper trades; this ALSO pushes each new entry to Telegram as
a signal the owner places MANUALLY on the TidalFi $5K prop challenge. Send-only (no polling ->
~0 RAM), fail-soft (a Telegram error must never touch the trading loop), dedup by position id.

PROP-SAFE SIZING (NOT the paper account's degen size): the challenge caps daily loss at $200
and max drawdown at $300 on $5000, target $250. So each signal recommends risking ONLY ~1% =
$50 (=> 4 losers before the daily cap). Position notional = risk$ / SL-distance. The SIGNAL
(coin/side/entry/SL/TP) mirrors the mission exactly; only the SIZE is re-scaled for the prop.

Config lives in state/whale_signal.json: {"bot_token","chat_id","enabled",...}. Dark (no-op)
until bot_token + chat_id are set — get_chat_id() auto-detects the chat once the owner messages
the bot. NEVER commits the token (state/ is gitignored runtime data).
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = ROOT / "state" / "whale_signal.json"

ACCOUNT_USD = 5000.0      # TidalFi 5K Two-Step Challenge
RISK_PCT = 1.0            # risk per trade => $50; daily cap $200 = 4 losers of headroom
DAILY_LOSS_CAP = 200.0
PROP_MAX_LEV = 5          # boss 2026-07-13: "ký quỹ max 5x thôi" — cap the SIGNAL leverage for prop
_API = "https://api.telegram.org/bot{}/{}"


def _load() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        CFG.parent.mkdir(parents=True, exist_ok=True)
        tmp = CFG.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        import os
        os.replace(tmp, CFG)
    except Exception:
        pass


def _api(token: str, method: str, payload: dict, timeout: float = 8.0) -> dict | None:
    try:
        req = urllib.request.Request(
            _API.format(token, method),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def get_chat_id(token: str) -> int | None:
    """Auto-detect the owner's chat id from the most recent message to the bot (setup helper —
    owner sends any message to the bot, then we read it here). Returns None if none yet."""
    d = _api(token, "getUpdates", {"limit": 5, "timeout": 0}, timeout=10.0)
    if not d or not d.get("ok"):
        return None
    for upd in reversed(d.get("result") or []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        cid = (msg.get("chat") or {}).get("id")
        if cid is not None:
            return int(cid)
    return None


CLOSED = ROOT / "state" / "llm_trader" / "closed.jsonl"


def _winrates() -> dict:
    """Real win rates from the paper ledger for the signal footer (owner asked 'tỷ lệ win đâu').
    Split by SOURCE + a recent-30 window, because the lifetime overall is dragged down by the
    replaced gpt-5.5 degen era — the recent/flush numbers are what this system actually is."""
    out = {}
    try:
        rows = [json.loads(l) for l in CLOSED.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return out

    def wr(sub):
        if not sub:
            return None
        w = sum(1 for r in sub if float(r.get("net", 0) or 0) > 0)
        return f"{round(w / len(sub) * 100)}% ({w}/{len(sub)})"

    out["flush"] = wr([r for r in rows if r.get("mech_method") == "flush_no_oi_mech"])
    out["model"] = wr([r for r in rows if not r.get("mech_method")][-40:])   # recent model only
    out["recent"] = wr(rows[-30:])
    return out


def _fmt_signal(p: dict, wr: dict | None = None) -> str | None:
    """Build a Telegram signal from an open-position row (paper), re-sized for the prop account."""
    try:
        sym = str(p.get("symbol") or "").replace("USDT", "")
        side = str(p.get("side") or "").upper()
        entry = float(p.get("entry") or 0)
        sl = float(p.get("sl") or 0)
        tp = float(p.get("tp") or 0)
        lev = min(int(p.get("leverage") or 5), PROP_MAX_LEV)   # prop signal capped at 5x (boss)
        if not sym or side not in ("LONG", "SHORT") or entry <= 0 or sl <= 0 or tp <= 0:
            return None
        sl_pct = abs(entry - sl) / entry * 100
        tp_pct = abs(tp - entry) / entry * 100
        if sl_pct <= 0:
            return None
        rr = tp_pct / sl_pct
        risk_usd = ACCOUNT_USD * RISK_PCT / 100.0                 # $50
        notional = risk_usd / (sl_pct / 100.0)                    # position size in USDT for a $50 risk
        margin = notional / max(1, lev)
        n_losers = int(DAILY_LOSS_CAP / risk_usd)                 # 4
        arrow = "🟢 LONG" if side == "LONG" else "🔴 SHORT"
        is_flush = str(p.get("mech_method") or "").startswith("flush")
        src = "🤖 máy (flush)" if is_flush else "🧠 model"
        why = (p.get("rationale") or "")[:160]
        wr = wr or {}
        wr_src = wr.get("flush") if is_flush else wr.get("model")
        wr_line = None
        if wr_src or wr.get("recent"):
            _lbl = "kèo flush" if is_flush else "model gần"
            parts = []
            if wr_src:
                parts.append(f"win {_lbl} <b>{wr_src}</b>")
            if wr.get("recent"):
                parts.append(f"hệ 30 lệnh gần <b>{wr['recent']}</b>")
            wr_line = "📈 " + " · ".join(parts)

        def px(v):
            return f"{v:,.6f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.2f}"

        lines = [
            "🐋 <b>CÁ VOI TẬP SỰ</b> — KÈO MỚI",
            "━━━━━━━━━━━━━━",
            f"📊 <b>{sym}</b> · {arrow} · x{lev}   <i>({src})</i>",
            f"📍 Entry: <code>{px(entry)}</code>",
            f"🛑 SL: <code>{px(sl)}</code>  (−{sl_pct:.2f}%)",
            f"🎯 TP: <code>{px(tp)}</code>  (+{tp_pct:.2f}%)  ·  R:R <b>{rr:.1f}</b>",
            "━━━━━━━━━━━━━━",
            f"💰 <b>Prop $5K</b> (rủi ro 1%): vào ~<b>${notional:,.0f}</b> (ký quỹ ~${margin:,.0f} @ x{lev})",
            f"   → dính SL lỗ ~<b>${risk_usd:.0f}</b>. Trần ngày −${DAILY_LOSS_CAP:.0f} = tối đa <b>{n_losers} lệnh thua</b>.",
        ]
        if wr_line:
            lines.append(wr_line)
        # DELAY GUARD (owner: "báo rồi đánh tay thì có độ trễ"): a manual fill lags the signal, so
        # place a LIMIT at the entry (fills at the right price or not at all — never chase Market),
        # and if price has already run toward TP past a small tolerance, SKIP instead of chasing.
        tol = 0.5 if is_flush else 0.8            # flush bounces are faster -> tighter chase window
        if side == "LONG":
            skip_above = entry * (1 + tol / 100)
        else:
            skip_above = entry * (1 - tol / 100)
        lines.append(
            f"⏱ Đặt <b>LIMIT</b> ở giá entry (KHÔNG Market). Nếu giá đã chạy quá "
            f"<code>{px(skip_above)}</code> về phía TP → <b>BỎ</b>, đừng đuổi.")
        # TAP-TO-COPY order (owner wanted a 'copy button'): a Telegram <code> line copies on tap,
        # so one tap gives the full ticket to paste on TidalFi — no browser automation, ToS-safe.
        lines.append(f"📋 <code>{sym} {side} · vốn ${margin:,.0f} x{lev} · SL {px(sl)} · TP {px(tp)}</code>")
        lines.append("⚠️ Vào TAY trên TidalFi (bấm dòng trên để copy). Kèo tham khảo, không phải lệnh tự động.")
        if why:
            lines.append(f"💡 {why}")
        return "\n".join(lines)
    except Exception:
        return None


def emit(open_rows: list[dict]) -> int:
    """Send a Telegram signal for each NEW open position (dedup by pos id). Called from the
    mission loop end. No-op (returns 0) until config has bot_token + chat_id + enabled."""
    cfg = _load()
    token, chat = cfg.get("bot_token"), cfg.get("chat_id")
    if not token or chat is None or not cfg.get("enabled", True):
        return 0
    sent = set(cfg.get("sent_ids") or [])
    wr = _winrates()
    n = 0
    for p in open_rows or []:
        pid = p.get("pos_id") or f"{p.get('symbol')}_{p.get('entry_ts')}"
        if pid in sent:
            continue
        text = _fmt_signal(p, wr)
        if not text:
            sent.add(pid)                # malformed -> mark so we don't retry every cycle
            continue
        res = _api(token, "sendMessage",
                   {"chat_id": chat, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": True})
        if res and res.get("ok"):
            sent.add(pid)
            n += 1
        # transient send failure -> NOT marked -> retried next cycle (a signal is worth retrying)
    if n or len(sent) != len(set(cfg.get("sent_ids") or [])):
        cfg["sent_ids"] = list(sent)[-500:]
        _save(cfg)
    return n


def _prop_day_window_start(now_s: float) -> float:
    """Start of the CURRENT prop day: TidalFi daily loss resets 07:00 GMT+7."""
    import time as _t
    import calendar as _cal
    lt = _t.gmtime(now_s + 7 * 3600)                      # GMT+7 wall clock
    day_start_gmt7 = _cal.timegm((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 0, 0, 0, 0, 0)) - 7 * 3600
    if now_s < day_start_gmt7:                            # before 07:00 -> window began yesterday
        day_start_gmt7 -= 86400
    return day_start_gmt7


def _prop_day_est(closed_rows: list[dict], now_s: float) -> tuple[float, int, int]:
    """Estimated prop-account P&L for TODAY's window if the owner took every signal at 1% risk:
    each closed trade contributes actual_R * $50 (signal risk). Returns (est_usd, wins, losses)."""
    start_ms = _prop_day_window_start(now_s) * 1000
    est = 0.0; w = 0; l = 0
    risk = ACCOUNT_USD * RISK_PCT / 100.0
    for r in closed_rows or []:
        try:
            if float(r.get("closed_ts") or 0) < start_ms:
                continue
            aR = r.get("actual_R")
            if aR is None:                                 # fallback: net/margin r (leverage-scaled)
                continue
            est += float(aR) * risk
            if float(r.get("net") or 0) > 0: w += 1
            else: l += 1
        except Exception:
            continue
    return round(est, 2), w, l


def _fmt_close(r: dict, day_line: str) -> str | None:
    """Exit notification: the signal's OTHER half (owner: bot chỉ báo vào, không báo ra)."""
    try:
        sym = str(r.get("symbol") or "").replace("USDT", "")
        side = str(r.get("side") or "").upper()
        exit_px = float(r.get("exit") or 0)
        reason = str(r.get("reason") or "")
        aR = r.get("actual_R")
        if not sym or exit_px <= 0:
            return None
        risk = ACCOUNT_USD * RISK_PCT / 100.0
        prop = (float(aR) * risk) if aR is not None else None
        win = (prop or 0) > 0 or float(r.get("net") or 0) > 0
        head = "✅ CHỐT LỜI" if win else "🔻 ĐÓNG LỆNH"
        why = {"tp": "chạm TP 🎯", "sl": "dính SL 🛑", "trail": "trail stop khóa lời 🪤",
               "llm_close": "model TỰ CẮT (đổi ý) ✂️", "timeout": "hết giờ (timeout kèo máy) ⏱",
               "liquidation": "THANH LÝ ⚠️"}.get(reason, reason)

        def px(v):
            return f"{v:,.6f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.2f}"

        lines = [f"{head} — <b>{sym}</b> {side}",
                 f"   thoát <code>{px(exit_px)}</code> · {why}"]
        if prop is not None:
            lines.append(f"   prop ước tính: <b>{'+' if prop >= 0 else ''}{prop:,.0f}$</b> (risk $50/kèo)")
        if day_line:
            lines.append(day_line)
        return "\n".join(lines)
    except Exception:
        return None


def emit_closes(closed_rows: list[dict]) -> int:
    """Send exit notifications for NEW closes since the last watermark + the daily prop line.
    Near the daily cap -> loud warning. Same fail-soft/dedup discipline as emit()."""
    import time as _t
    cfg = _load()
    token, chat = cfg.get("bot_token"), cfg.get("chat_id")
    if not token or chat is None or not cfg.get("enabled", True):
        return 0
    wm = float(cfg.get("last_close_ts") or 0)
    new = [r for r in (closed_rows or []) if float(r.get("closed_ts") or 0) > wm]
    if not new:
        return 0
    now_s = _t.time()
    est, w, l = _prop_day_est(closed_rows, now_s)
    left = DAILY_LOSS_CAP + est if est < 0 else DAILY_LOSS_CAP
    day_line = (f"📅 Hôm nay (reset 07:00): <b>{'+' if est >= 0 else ''}{est:,.0f}$</b> ước tính"
                f" ({w}W/{l}L) · trần −${DAILY_LOSS_CAP:.0f}")
    n = 0
    for r in sorted(new, key=lambda x: float(x.get("closed_ts") or 0)):
        text = _fmt_close(r, day_line)
        if text and est <= -150:
            text += "\n⛔ <b>GẦN TRẦN NGÀY — khuyên NGỪNG đánh tới 07:00 mai.</b>"
        if text:
            res = _api(token, "sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                              "disable_web_page_preview": True})
            if res and res.get("ok"):
                n += 1
        wm = max(wm, float(r.get("closed_ts") or 0))
    cfg["last_close_ts"] = wm
    _save(cfg)
    return n


def handle_commands(status_fn=None) -> int:
    """Piggyback /status on the mission loop (getUpdates with offset — no extra process, ~one
    cycle latency). status_fn() -> str provides the reply body."""
    cfg = _load()
    token, chat = cfg.get("bot_token"), cfg.get("chat_id")
    if not token or chat is None:
        return 0
    d = _api(token, "getUpdates", {"offset": int(cfg.get("upd_offset") or 0) + 1, "limit": 10,
                                   "timeout": 0}, timeout=10.0)
    if not d or not d.get("ok"):
        return 0
    n = 0
    last = int(cfg.get("upd_offset") or 0)
    for upd in d.get("result") or []:
        last = max(last, int(upd.get("update_id") or 0))
        msg = upd.get("message") or {}
        text = str(msg.get("text") or "").strip().lower()
        if (msg.get("chat") or {}).get("id") != chat:
            continue
        if text.startswith("/status") or text in ("status", "sao roi", "sao rồi"):
            body = None
            try:
                body = status_fn() if status_fn else None
            except Exception:
                body = None
            _api(token, "sendMessage", {"chat_id": chat, "parse_mode": "HTML",
                                        "text": body or "🐋 đang chạy — chưa có dữ liệu."})
            n += 1
    if last != int(cfg.get("upd_offset") or 0):
        cfg["upd_offset"] = last
        _save(cfg)
    return n


def tick(open_rows: list[dict], closed_rows: list[dict], status_fn=None) -> None:
    """One call per mission cycle: entry signals + exit notifications + command handling.
    Each part isolated — one failing must not stop the others."""
    for fn in ((lambda: emit(open_rows)), (lambda: emit_closes(closed_rows)),
               (lambda: handle_commands(status_fn))):
        try:
            fn()
        except Exception:
            pass


if __name__ == "__main__":                # CLI: setup + smoke test
    import sys
    cfg = _load()
    if len(sys.argv) >= 3 and sys.argv[1] == "set-token":
        cfg["bot_token"] = sys.argv[2]
        _save(cfg)
        cid = get_chat_id(sys.argv[2])
        if cid is not None:
            cfg["chat_id"] = cid
            _save(cfg)
            _api(sys.argv[2], "sendMessage",
                 {"chat_id": cid, "text": "🐋 Cá Voi Tập Sự đã kết nối. Từ giờ mọi kèo sẽ báo về đây."})
            print(json.dumps({"ok": True, "chat_id": cid}))
        else:
            print(json.dumps({"ok": False, "need": "nhắn 1 tin cho bot rồi chạy lại"}))
    elif len(sys.argv) >= 2 and sys.argv[1] == "test":
        print(_fmt_signal({"symbol": "BTCUSDT", "side": "LONG", "entry": 62341.3,
                           "sl": 61094.0, "tp": 64834.0, "leverage": 10,
                           "mech_method": "flush_no_oi_mech",
                           "rationale": "Capitulation flush -> bounce."}, _winrates()))
    else:
        print("usage: whale_signal.py set-token <TOKEN> | test")
