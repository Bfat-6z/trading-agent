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

# TidalFi listed perps (boss screenshots 2026-07-15: "sàn chỉ có những coin này thôi") —
# signals for anything else are unactionable noise (KITE/VELVET/... can't be traded there).
TIDALFI_SYMBOLS = frozenset({
    "BTCUSDT", "ETHUSDT", "XAUUSDT", "SOLUSDT", "MUUSDT", "SPCXUSDT",
    "XAGUSDT", "ZECUSDT", "HYPEUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT",
    "NEARUSDT", "1000PEPEUSDT", "ADAUSDT", "INTCUSDT", "NVDAUSDT", "LINKUSDT",
    "UNIUSDT", "AVAXUSDT", "AMDUSDT", "XLMUSDT", "METAUSDT", "TSLAUSDT",
    "TRXUSDT", "LTCUSDT", "ORCLUSDT", "DOTUSDT", "ASTERUSDT", "1000SHIBUSDT",
    "TSMUSDT", "MSFTUSDT", "GOOGLUSDT", "AAPLUSDT", "AVGOUSDT", "AMZNUSDT",
    "OPENAIUSDT", "JPMUSDT", "CSCOUSDT", "WMTUSDT", "BRKBUSDT", "VUSDT",
})


def _on_prop(sym) -> bool:
    return str(sym or "") in TIDALFI_SYMBOLS


# --- TidalFi PUBLIC read-only API (boss's other agent validated it, 2026-07-15;
#     td.tidalfi.ai — no auth; /api/trading/terminal etc. are 401 and NOT used.
#     READ ONLY: signal accuracy on the boss's actual venue. No order path exists here.)
TIDALFI_API = "https://td.tidalfi.ai"
_TF_META: dict = {"ts": 0.0, "by_sym": {}}
_UPD_BACKOFF: dict = {"until": 0.0}     # getUpdates DNS-flap backoff (audit #7)


def _tf_get(path: str, timeout: float = 5.0) -> dict | list | None:
    try:
        req = urllib.request.Request(TIDALFI_API + path, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        return d.get("data") if isinstance(d, dict) and "data" in d else d
    except Exception as e:
        _log({"tf_api_err": path.split("?")[0], "error": repr(e)[:80]})
        return None


def _tf_meta(sym) -> dict | None:
    """Per-symbol tick/step/minNotional from TidalFi (cached 6h; None = fail-open)."""
    import time as _t
    if _t.time() - float(_TF_META.get("fail_ts") or 0) < 300:
        return _TF_META["by_sym"].get(str(sym or ""))   # negative-cache 5min (audit LOW:
    if not _TF_META["by_sym"] or _t.time() - _TF_META["ts"] > 6 * 3600:   # no 6s stall per call)
        rows = _tf_get("/api/market-data/symbols?status=TRADING", timeout=6.0)
        if not rows:
            _TF_META["fail_ts"] = _t.time()
        if isinstance(rows, dict):                 # venue wraps: data -> {symbols:[...]}
            rows = rows.get("symbols") or rows.get("list") or []
        if isinstance(rows, list) and rows:
            _TF_META["by_sym"] = {x["symbol"]: x for x in rows
                                  if isinstance(x, dict) and x.get("symbol")}
            _TF_META["ts"] = _t.time()
    return _TF_META["by_sym"].get(str(sym or ""))


def _tf_quote(sym) -> dict | None:
    """Live top-of-book on the boss's venue (None = fail-open, ticket keeps paper px)."""
    d = _tf_get(f"/api/trading/orderbook?symbol={sym}&limit=5", timeout=4.0)
    try:
        bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
        return {"bid": bid, "ask": ask, "spread_pct": (ask - bid) / ask * 100 if ask else 0.0}
    except Exception:
        return None


def _tf_round(px: float, tick) -> float:
    """Round a price to the venue tick so a copied ticket is accepted verbatim."""
    try:
        t = float(tick)
        if t <= 0:
            return px
        s = f"{t:f}".rstrip("0")
        dec = len(s.split(".")[1]) if "." in s else 0
        return round(round(px / t) * t, dec)
    except Exception:
        return px
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
    except Exception as e:
        # audit: a silently-lost save = lost sent_ids = RESEND FLOOD after restart.
        # Can't persist, but we CAN leave evidence.
        _log({"save_fail": repr(e)[:100]})


LOG = ROOT / "state" / "whale_signal.log"


def _log(ev: dict) -> None:
    try:
        import time as _t
        ev["ts"] = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _api(token: str, method: str, payload: dict, timeout: float = 8.0) -> dict | None:
    """Hardened (gap #1): on an HTTP error, still parse Telegram's body so callers see
    error_code/description/retry_after; a revoked token or 429 is LOGGED, never silent."""
    try:
        req = urllib.request.Request(
            _API.format(token, method),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"ok": False, "error_code": e.code, "description": str(e)[:120]}
        _log({"send_fail": method, "code": body.get("error_code"), "desc": str(body.get("description"))[:140]})
        return body
    except Exception as e:
        _log({"send_error": method, "error": repr(e)[:140]})
        return None


import re as _re


def _send(token: str, chat, text: str) -> bool:
    """Send with HTML; on a parse error (400 can't parse entities) retry once as PLAIN text so a
    bad tag can't drop a signal. 429 -> logged, retried next cycle (send-only, no in-loop sleep)."""
    res = _api(token, "sendMessage",
               {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
    if res and res.get("ok"):
        return True
    desc = str((res or {}).get("description") or "").lower()
    if res and res.get("error_code") == 400 and "parse" in desc:
        plain = _re.sub(r"<[^>]+>", "", text)
        res2 = _api(token, "sendMessage",
                    {"chat_id": chat, "text": plain, "disable_web_page_preview": True})
        return bool(res2 and res2.get("ok"))
    return False


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

    def stat(sub):
        return (sum(1 for r in sub if float(r.get("net", 0) or 0) > 0), len(sub))

    _fl = [r for r in rows if r.get("mech_method") == "flush_no_oi_mech"]
    _md = [r for r in rows if not r.get("mech_method")][-40:]   # recent model only
    out["flush"] = wr(_fl)
    out["model"] = wr(_md)
    out["recent"] = wr(rows[-30:])
    out["flush_stat"] = stat(_fl)    # numeric (wins, n) for the OWNER LAW winrate gate
    out["model_stat"] = stat(_md)
    return out


def _fmt_signal(p: dict, wr: dict | None = None, exp: dict | None = None) -> str | None:
    """Build a Telegram signal from an open-position row (paper), re-sized for the prop account.
    `exp` = prop exposure state (open count, aggregate risk, day est, near-cap flag)."""
    try:
        sym = str(p.get("symbol") or "").replace("USDT", "")
        side = str(p.get("side") or "").upper()
        entry = float(p.get("entry") or 0)
        sl = float(p.get("sl") or 0)
        tp = float(p.get("tp") or 0)
        lev = min(int(p.get("leverage") or 5), PROP_MAX_LEV)   # prop signal capped at 5x (boss)
        if not sym or side not in ("LONG", "SHORT") or entry <= 0 or sl <= 0 or tp <= 0:
            return None
        _meta = _tf_meta(p.get("symbol"))
        if _meta and _meta.get("tickSize"):        # venue-exact prices: copy = accepted verbatim
            _tk = _meta["tickSize"]
            entry, sl, tp = _tf_round(entry, _tk), _tf_round(sl, _tk), _tf_round(tp, _tk)
        _q = _tf_quote(p.get("symbol")) if _meta else None
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

        exp = exp or {}
        _n = exp.get("seq")
        head = f"🐋 <b>CÁ VOI TẬP SỰ</b> — KÈO {'#%d' % _n if _n else 'MỚI'}"
        if exp.get("dd_stop"):               # gap #10: max-DD $300 = challenge DEAD — loudest banner
            head = (f"🟥 <b>MAX-DD CHẠM TRẦN (−${exp.get('dd_now', 0):,.0f} / ${MAX_DD_CAP:.0f}) — "
                    f"DỪNG ĐÁNH HẲN.</b>\n" + head + " (chỉ tham khảo, KHÔNG vào)")
        elif exp.get("near_cap"):
            head = ("⛔ <b>GẦN TRẦN NGÀY — KÈO NÀY KHÔNG KHUYÊN VÀO</b>\n"
                    f"   (hôm nay ước tính {exp.get('day_est'):,.0f}$ / trần −${DAILY_LOSS_CAP:.0f})\n"
                    "🐋 <b>CÁ VOI TẬP SỰ</b> — kèo (tham khảo, cân nhắc BỎ)")
        import time as _t
        _lt = _t.gmtime(_t.time() + 7 * 3600)
        lines = [
            head,
            "━━━━━━━━━━━━━━",
            f"🕐 {_t.strftime('%H:%M', _lt)} (GMT+7) · hiệu lực ~<b>30 phút</b> — trễ hơn thì BỎ",
            f"📊 <b>{sym}</b> · {arrow} · x{lev}   <i>({src})</i>",
            f"📍 Entry: <code>{px(entry)}</code>",
        ]
        if _q:                                     # the boss's VENUE price, not Binance's —
            _sw = " ⚠️ spread rộng — LIMIT only" if _q["spread_pct"] > 0.10 else ""
            lines.append(f"🌊 TidalFi: bid <code>{px(_q['bid'])}</code> / ask "
                         f"<code>{px(_q['ask'])}</code> · spread {_q['spread_pct']:.3f}%{_sw}")
        lines += [
            f"🛑 SL: <code>{px(sl)}</code>  (−{sl_pct:.2f}%)",
            f"🎯 TP: <code>{px(tp)}</code>  (+{tp_pct:.2f}%)  ·  R:R <b>{rr:.1f}</b>",
            "━━━━━━━━━━━━━━",
            f"💰 <b>Prop $5K</b> (rủi ro 1%): vào ~<b>${notional:,.0f}</b> (ký quỹ ~${margin:,.0f} @ x{lev})",
            f"   → dính SL lỗ ~<b>${risk_usd:.0f}</b>. Trần ngày −${DAILY_LOSS_CAP:.0f} = tối đa <b>{n_losers} lệnh thua</b>.",
        ]
        if wr_line:
            lines.append(wr_line)
        if exp.get("n_open"):            # portfolio state so a correlated stack is visible (gap #2)
            conc = exp.get("concentration") or 0
            warn = " ⚠️ cùng chiều — coi chừng BTC dump quét cả cụm" if conc >= 3 else ""
            lines.append(f"📊 Đang mở <b>{exp['n_open']}</b> kèo · tổng risk ~<b>${exp.get('risk_usd',0)}</b>{warn}")
        lines.append(f"📅 Hôm nay: <b>{'+' if exp.get('day_est',0) >= 0 else ''}{exp.get('day_est',0):,.0f}$</b>"
                     f" (ước tính) · trần −${DAILY_LOSS_CAP:.0f}")
        if exp.get("dd_warn") or exp.get("dd_stop"):     # multi-day bleed line (gap #10)
            lines.append(f"📉 DD từ đỉnh: <b>−${exp.get('dd_now', 0):,.0f}</b> / trần ${MAX_DD_CAP:.0f}"
                         f"{' — SẮP CHÁY CHALLENGE, đánh nhỏ lại hoặc nghỉ' if not exp.get('dd_stop') else ''}")
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


def emit(open_rows: list[dict], closed_rows: list[dict] | None = None) -> int:
    """Send a Telegram signal for each NEW open position (dedup by pos id). PROP RISK STATE
    (gap #2): the signal carries the current prop-account picture — today's est P&L, open count,
    aggregate risk, same-direction concentration — and when near the −$200 daily cap the new-entry
    signal is RED-FLAGGED as 'không khuyên vào'. No-op until config has token + chat + enabled."""
    import time as _t
    cfg = _load()
    token, chat = cfg.get("bot_token"), cfg.get("chat_id")
    if not token or chat is None or not cfg.get("enabled", True):
        return 0
    sent = set(cfg.get("sent_ids") or [])
    wr = _winrates()
    est, w, l = _prop_day_est(closed_rows or [], _t.time())
    opens = open_rows or []
    n_open = len(opens)
    same_dir = {}
    for p in opens:
        same_dir[str(p.get("side"))] = same_dir.get(str(p.get("side")), 0) + 1
    risk = ACCOUNT_USD * RISK_PCT / 100.0
    import time as _t
    if not cfg.get("prop_start_ts"):                     # DD anchor: set ONCE, persist NOW
        cfg["prop_start_ts"] = int(_t.time() * 1000)     # (before any conditional save path)
        _save(cfg)
    _cum, _dd = _prop_maxdd(closed_rows or [], cfg["prop_start_ts"])
    exposure = {"n_open": n_open, "risk_usd": round(n_open * risk),
                "concentration": max(same_dir.values()) if same_dir else 0,
                "day_est": est, "near_cap": est <= -(DAILY_LOSS_CAP * 0.75),
                "cum": _cum, "dd_now": _dd,                       # gap #10: multi-day bleed
                "dd_warn": _dd >= MAX_DD_CAP * 0.75, "dd_stop": _dd >= MAX_DD_CAP}
    n = 0
    for p in opens:
        pid = p.get("pos_id") or f"{p.get('symbol')}_{p.get('entry_ts')}"
        if pid in sent:
            continue
        if not _on_prop(p.get("symbol")):    # not listed on TidalFi -> unactionable, skip
            sent.add(pid)                    # (marked so we never retry it)
            _log({"skip_not_on_prop": str(p.get("symbol"))})
            continue
        try:                                 # gap #9: never tell the owner to enter a STALE
            _age_min = (_t.time() * 1000 - float(p.get("entry_ts") or 0)) / 60000.0
        except Exception:
            _age_min = 0.0
        if _age_min > 45:                    # bot was down / backlog -> entry price is gone
            sent.add(pid)                    # (audit: silence left the boss blind if HIS limit
            _sym = str(p.get("symbol") or "").replace("USDT", "")   # also filled during the stall
            _send(token, chat,               # -> downgraded notice, one-shot, best-effort)
                  f"⏰ <b>KHỚP TRỄ — {_sym}</b> (mở ~{round(_age_min)} phút trước, bot vừa hồi). "
                  f"Kèo KHÔNG còn fresh — nếu limit của mày cũng đã khớp thì check vị thế + đặt SL.")
            _log({"skip_stale_entry": str(p.get("symbol")), "age_min": round(_age_min)})
            continue
        # OWNER LAW 2026-07-15: "winrate phải trên 50%" — a signal reaches the boss's
        # REAL prop account only from a source with a PROVEN >50% win rate (n>=10).
        # Unproven (n<10) = not proven >50% = no signal; the paper book keeps building
        # the track record regardless. min_wr/min_wr_n overridable in config.
        _is_fl = str(p.get("mech_method") or "").startswith("flush")
        _w, _n = (wr.get("flush_stat") if _is_fl else wr.get("model_stat")) or (0, 0)
        _min_wr = float(cfg.get("min_wr") or 50.0)
        _min_n = int(cfg.get("min_wr_n") or 10)
        if _n < _min_n or (_w / _n * 100.0) <= _min_wr:
            sent.add(pid)
            _log({"skip_low_winrate": str(p.get("symbol")),
                  "src": "flush" if _is_fl else "model",
                  "wr": round(_w / _n * 100.0, 1) if _n else None, "n": _n})
            continue
        exposure["seq"] = int(cfg.get("seq") or 0) + 1
        text = _fmt_signal(p, wr, exposure)
        if not text:
            sent.add(pid)                # malformed -> mark so we don't retry every cycle
            continue
        if _send(token, chat, text):
            sent.add(pid)
            cfg["seq"] = exposure["seq"]     # "KÈO #N" advances only on a DELIVERED signal
            n += 1
            _log({"sent": pid, "seq": exposure["seq"]})   # audit: success was unprovable from log
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
            if not _on_prop(r.get("symbol")):              # only trades the owner could follow
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


MAX_DD_CAP = 300.0        # TidalFi Two-Step: max total drawdown $300 = challenge DEAD


def _prop_maxdd(closed_rows: list[dict], start_ms: float) -> tuple[float, float]:
    """Cumulative prop estimate + trailing max-drawdown since the tracker anchor (gap #10:
    Two-Step challenges die from MULTI-DAY bleed the daily tracker never sees). PURE —
    the anchor (cfg['prop_start_ts']) is owned by the CALLER: this helper must not
    load+save its own config copy or the caller's later _save of a pre-anchor snapshot
    would silently drop the anchor and reset the DD clock every cycle. Trailing
    peak-to-now — stricter than a static floor, so warnings fire early, never late."""
    start = float(start_ms or 0)
    risk = ACCOUNT_USD * RISK_PCT / 100.0
    rows = []
    for r in closed_rows or []:
        try:
            ts = float(r.get("closed_ts") or 0)
            if ts >= start and _on_prop(r.get("symbol")) and r.get("actual_R") is not None:
                rows.append((ts, float(r["actual_R"]) * risk))
        except Exception:
            continue
    rows.sort(key=lambda x: x[0])
    cum = peak = 0.0
    dd = 0.0
    for _ts, v in rows:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return round(cum, 2), round(max(0.0, peak - cum), 2)   # (cum PnL, CURRENT drawdown)


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
    # FIRST-RUN BASELINE (bugfix 2026-07-14): with no watermark, EVERY historical close looked
    # "new" -> a 120-message burst -> Telegram 429 rate-limit. On first run, baseline to the newest
    # close and send NOTHING historical; only genuinely-new closes fire afterwards.
    if cfg.get("last_close_ts") is None:
        mx = max((float(r.get("closed_ts") or 0) for r in (closed_rows or [])), default=0)
        cfg["last_close_ts"] = mx
        _save(cfg)
        return 0
    wm = float(cfg.get("last_close_ts") or 0)
    new = [r for r in (closed_rows or []) if float(r.get("closed_ts") or 0) > wm]
    if not new:
        return 0
    if len(new) > 5:                 # safety cap: never burst more than 5 closes/cycle (429 guard)
        # audit LOW: keep the OLDEST 5 — wm then naturally stops before the remainder,
        # which drains next cycle (the old newest-5 + wm-rewind line was dead code that
        # silently dropped the older rows forever).
        new = sorted(new, key=lambda x: float(x.get("closed_ts") or 0))[:5]
    now_s = _t.time()
    est, w, l = _prop_day_est(closed_rows, now_s)
    left = DAILY_LOSS_CAP + est if est < 0 else DAILY_LOSS_CAP
    day_line = (f"📅 Hôm nay (reset 07:00): <b>{'+' if est >= 0 else ''}{est:,.0f}$</b> ước tính"
                f" ({w}W/{l}L) · trần −${DAILY_LOSS_CAP:.0f}")
    if not cfg.get("prop_start_ts"):                     # DD anchor (gap #10) — same as emit()
        cfg["prop_start_ts"] = int(now_s * 1000)
        _save(cfg)
    _cum, _dd = _prop_maxdd(closed_rows, cfg["prop_start_ts"])
    if _dd >= MAX_DD_CAP * 0.5:                          # only surface once it matters
        day_line += (f"\n📉 DD từ đỉnh: <b>−${_dd:,.0f}</b> / trần ${MAX_DD_CAP:.0f}"
                     + (" — <b>DỪNG ĐÁNH</b>" if _dd >= MAX_DD_CAP else ""))
    n = 0
    intact = True          # audit HIGH: wm used to advance past FAILED sends -> a close
    for r in sorted(new, key=lambda x: float(x.get("closed_ts") or 0)):   # notification (the
        _ts = float(r.get("closed_ts") or 0)          # boss's live exit!) was lost FOREVER on
        if not _on_prop(r.get("symbol")):             # a 429/DNS window. Now: wm freezes at the
            if intact:                                # first failure; that row + everything
                wm = max(wm, _ts)                     # after retries next cycle (burst cap still
            continue                                  # bounds the replay).
        text = _fmt_close(r, day_line)
        if text and est <= -150:
            text += "\n⛔ <b>GẦN TRẦN NGÀY — khuyên NGỪNG đánh tới 07:00 mai.</b>"
        ok = (not text) or _send(token, chat, text)   # malformed row = skip forever by design
        if text and ok:
            n += 1
            _log({"sent_close": r.get("symbol"), "ts": _ts})
        if ok and intact:
            wm = max(wm, _ts)
        elif not ok:
            intact = False
            _log({"close_send_fail_wm_hold": r.get("symbol"), "ts": _ts})
            # Codex blocker: rows AFTER a failure used to send fine while wm stayed frozen
            # -> resent every cycle until the failed one went through (spam x4). STOP at the
            # first failure; chronological retry next cycle keeps ordering + exactly-once.
            fr = cfg.get("close_fail") or {}
            fr = {"ts": _ts, "n": (fr.get("n") or 0) + 1} if fr.get("ts") == _ts else {"ts": _ts, "n": 1}
            cfg["close_fail"] = fr
            if fr["n"] >= 12:                     # dead-letter: ~12 cycles of the SAME row
                wm = max(wm, _ts)                 # failing = poison payload; advance past it
                _log({"close_dead_letter": r.get("symbol"), "ts": _ts})
                cfg.pop("close_fail", None)
            break
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
    import time as _t
    if _t.time() < float(_UPD_BACKOFF.get("until") or 0):
        return 0                       # audit: 44min DNS outage = one failed lookup + log
    d = _api(token, "getUpdates", {"offset": int(cfg.get("upd_offset") or 0) + 1, "limit": 10,
                                   "timeout": 0}, timeout=10.0)
    if not d or not d.get("ok"):
        _UPD_BACKOFF["until"] = _t.time() + 600   # spam per cycle -> back off 10min
        return 0
    _UPD_BACKOFF["until"] = 0.0
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


INCIDENTS = ROOT / "state" / "incidents_latest.json"


def emit_mgmt(open_rows: list[dict]) -> int:
    """MID-TRADE UPDATES (gap #3): the model manages its own open positions (moves SL/TP, cuts
    early). Those changes live in p['mgmt']; the owner holding a manual position must be told when
    the paper stop moves — else they sit on the original stop the system has already tightened.
    Tracks the last mgmt count per pos_id; on growth, sends the new SL/TP."""
    cfg = _load()
    token, chat = cfg.get("bot_token"), cfg.get("chat_id")
    if not token or chat is None or not cfg.get("enabled", True):
        return 0
    seen = cfg.get("mgmt_seen") or {}
    n = 0
    for p in open_rows or []:
        if not _on_prop(p.get("symbol")):             # entry was never signalled -> no updates
            continue
        pid = p.get("pos_id") or f"{p.get('symbol')}_{p.get('entry_ts')}"
        mg = p.get("mgmt") or []
        if len(mg) <= int(seen.get(pid, 0)):
            continue
        last = mg[-1] if mg else {}
        sym = str(p.get("symbol") or "").replace("USDT", "")

        def px(v):
            v = float(v)
            return f"{v:,.6f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.2f}"

        parts = []
        if last.get("sl") is not None:
            parts.append(f"SL → <code>{px(last['sl'])}</code>")
        if last.get("tp") is not None:
            parts.append(f"TP → <code>{px(last['tp'])}</code>")
        why = str(last.get("why") or "")[:90]
        text = (f"🔧 <b>CẬP NHẬT — {sym} {p.get('side')}</b>\n"
                f"   model dời {' · '.join(parts) if parts else 'mức'}\n"
                f"   👉 sửa lệnh trên TidalFi cho khớp." + (f"\n   💡 {why}" if why else ""))
        if _send(token, chat, text):
            seen[pid] = len(mg)
            n += 1
    if n:
        cfg["mgmt_seen"] = {k: v for k, v in list(seen.items())[-300:]}
        _save(cfg)
    return n


def emit_pending(pending_rows: list[dict]) -> int:
    """LIMIT PLACE / CANCEL (gap #4): the model often queues a LIMIT (wait for the pullback). Signal
    it WHEN placed so the owner's limit rests in the book at the same time as the paper's — not only
    after it fills (which would miss every fast flush). On disappearance: became a position (already
    signalled by emit) -> silent; else cancelled/expired -> notify."""
    cfg = _load()
    token, chat = cfg.get("bot_token"), cfg.get("chat_id")
    if not token or chat is None or not cfg.get("enabled", True):
        return 0
    prev = set(cfg.get("pending_seen") or [])
    filled = set(cfg.get("sent_ids") or [])
    cur = {}
    for q in pending_rows or []:
        qid = f"{q.get('symbol')}_{q.get('placed_ms')}"
        cur[qid] = q
    n = 0
    seen_next = set()          # audit HIGH: pending_seen was replaced with cur UNCONDITIONALLY —
    for qid, q in cur.items():                        # a failed place-send was recorded as "seen"
        if qid in prev:                               # and never retried (the boss's EARLY entry
            seen_next.add(qid)                        # ticket, lost). Now: a qid enters the seen
            continue                                  # set only after a DELIVERED place notice
        if not _on_prop(q.get("symbol")):             # (or is by-design silent: off-venue rows).
            seen_next.add(qid)                        # not on TidalFi -> never announced
            continue
        sym = str(q.get("symbol") or "").replace("USDT", "")
        side = str(q.get("side") or "").upper()
        ep = q.get("entry_px")
        if ep is None:
            seen_next.add(qid)                        # malformed -> don't retry forever
            continue

        def px(v):
            v = float(v)
            return f"{v:,.6f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.2f}"

        arrow = "🟢 LONG" if side == "LONG" else "🔴 SHORT"
        # enrich (owner group asked 'tỷ lệ đâu'): a resting-limit ticket must carry the same
        # info as a market entry — source, SL/TP, R:R, prop sizing + the win rates.
        lev = min(int(q.get("leverage") or 5), PROP_MAX_LEV)     # prop cap 5x
        slp = float(q.get("sl_pct") or 0)
        tpp = float(q.get("tp_pct") or 0)
        epf = float(ep)
        if side == "LONG":
            sl_px, tp_px = epf * (1 - slp / 100), epf * (1 + tpp / 100)
        else:
            sl_px, tp_px = epf * (1 + slp / 100), epf * (1 - tpp / 100)
        rr = (tpp / slp) if slp > 0 else 0
        is_flush = str(q.get("mech_method") or "").startswith("flush")
        src = "🤖 máy (flush)" if is_flush else "🧠 model"
        wr = _winrates()
        # OWNER LAW 2026-07-15: >50% proven win rate (n>=10) or no ticket — same gate as emit().
        _w, _n = (wr.get("flush_stat") if is_flush else wr.get("model_stat")) or (0, 0)
        if _n < int(cfg.get("min_wr_n") or 10) or (_w / _n * 100.0) <= float(cfg.get("min_wr") or 50.0):
            seen_next.add(qid)               # suppressed by law -> also no cancel-notice later
            _log({"skip_low_winrate": str(q.get("symbol")), "src": "flush" if is_flush else "model",
                  "wr": round(_w / _n * 100.0, 1) if _n else None, "n": _n, "path": "pending"})
            continue
        wr_src = wr.get("flush") if is_flush else wr.get("model")
        wr_bits = []
        if wr_src:
            wr_bits.append(f"win {'kèo flush' if is_flush else 'model gần'} <b>{wr_src}</b>")
        if wr.get("recent"):
            wr_bits.append(f"hệ 30 gần <b>{wr['recent']}</b>")
        wr_line = ("📈 " + " · ".join(wr_bits) + "\n") if wr_bits else ""
        risk_usd = ACCOUNT_USD * RISK_PCT / 100.0                # $50 = 1% acc
        notional = risk_usd / (slp / 100.0) if slp > 0 else 0
        margin = notional / max(1, lev)
        rr_line = f"   🛑 SL <code>{px(sl_px)}</code> · 🎯 TP <code>{px(tp_px)}</code>" + (f" · R:R <b>{rr:.1f}</b>\n" if rr else "\n") if slp > 0 and tpp > 0 else ""
        sz_line = f"   💵 vốn ~<b>${margin:,.0f}</b> · x{lev} · rủi ro <b>${risk_usd:,.0f}</b>\n" if notional > 0 else ""
        text = (f"⏳ <b>CHỜ LIMIT — {sym}</b> · {arrow} · x{lev}   <i>({src})</i>\n"
                f"━━━━━━━━━━━━━━\n"
                f"   đặt LIMIT ở <code>{px(ep)}</code> (chờ giá về, ~2h)\n"
                f"{rr_line}{sz_line}{wr_line}"
                f"   👉 đặt limit trên TidalFi ngay để nằm cùng book.")
        if _send(token, chat, text):
            n += 1
            seen_next.add(qid)
            _log({"sent_pending": qid})
        # failed place-send -> NOT seen -> retried next cycle
    for qid in prev - set(cur):                       # gone: cancelled/expired (not a fill)
        # a pos_id for a filled pending contains the coin; if we already signalled a same-coin
        # position recently, treat as fill (silent). Heuristic: coin part present in sent_ids.
        coin = qid.split("_")[0]
        if not _on_prop(coin):                        # never announced -> nothing to cancel
            continue
        if any(coin in str(s) for s in filled):
            continue
        sym = coin.replace("USDT", "")
        if _send(token, chat, f"❌ <b>HỦY LIMIT — {sym}</b> (giá chạy quá / hết hạn). Nếu mày đã đặt limit trên TidalFi thì HỦY."):
            n += 1
        else:
            seen_next.add(qid)    # failed CANCEL notice: keep it "seen" so prev-cur
                                  # re-fires the cancel next cycle instead of losing it
    cfg["pending_seen"] = list(seen_next)[-100:]
    _save(cfg)
    return n


def emit_ops_alerts() -> int:
    """OPS ALERT DRAIN (gap #8): incidents ARE recorded (state/incidents_latest.json) but nobody
    is told — the supervisor wedged 77min + a 6h quarantine happened silently. Push each NEW open,
    action-required incident to Telegram (deduped by incident_id). Distinct '🔧 HỆ THỐNG' prefix so
    it's not confused with a trade signal. Only recent (<6h) unresolved ones -> no startup flood."""
    import time as _t
    cfg = _load()
    token, chat = cfg.get("bot_token"), cfg.get("chat_id")
    if not token or chat is None or not cfg.get("ops_alerts", True):
        return 0
    try:
        d = json.loads(INCIDENTS.read_text(encoding="utf-8"))
    except Exception:
        return 0
    incs = []
    if isinstance(d.get("latest"), dict):
        incs.append(d["latest"])
    for r in (d.get("recent") or []):
        if isinstance(r, dict):
            incs.append(r)
    sent = set(cfg.get("alerted_incidents") or [])
    n = 0
    for inc in incs:
        iid = inc.get("incident_id")
        if not iid or iid in sent:
            continue
        if inc.get("resolved_at") or inc.get("closed_at"):
            sent.add(iid)                # already handled -> just remember, don't alert
            continue
        try:                             # skip stale (>6h) so a first run doesn't flood
            op = inc.get("opened_at")
            if op:
                import calendar as _cal
                ts = _cal.timegm(_t.strptime(str(op)[:19], "%Y-%m-%dT%H:%M:%S"))
                if _t.time() - ts > 6 * 3600:
                    sent.add(iid)
                    continue
        except Exception:
            pass
        text = ("🔧 <b>HỆ THỐNG — SỰ CỐ</b>\n"
                f"• {inc.get('dedupe_key') or inc.get('incident_id')}\n"
                f"• cần: {inc.get('action_required') or '—'}\n"
                f"<code>{str(inc.get('detail'))[:180]}</code>")
        if _send(token, chat, text):
            sent.add(iid)
            n += 1
    if n or len(sent) != len(set(cfg.get("alerted_incidents") or [])):
        cfg["alerted_incidents"] = list(sent)[-200:]
        _save(cfg)
    return n


def tick(open_rows: list[dict], closed_rows: list[dict], pending_rows: list[dict] | None = None,
         status_fn=None) -> None:
    """One call per mission cycle: entry + LIMIT-place/cancel + mid-trade updates + exits + ops
    alerts + /status. Each part isolated — one failing must not stop the others."""
    for fn in ((lambda: emit(open_rows, closed_rows)),
               (lambda: emit_pending(pending_rows or [])),
               (lambda: emit_mgmt(open_rows)),
               (lambda: emit_closes(closed_rows)),
               (lambda: emit_ops_alerts()),
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
