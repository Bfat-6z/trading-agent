"""Tiny FastAPI server serving bot log + position status. Mobile-friendly HTML.
Real-time log via Server-Sent Events (SSE)."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

import asyncio
import hmac
import os
import time
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, StreamingResponse
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

app = FastAPI(title="Trading Bot Monitor")
LOG = Path("state/futures_watch.log")
ZEC_LOG = Path("state/zec_monitor.log")
TV_LOG = Path("state/tv_webhook.log")

TV_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")
TV_MAX_MARGIN = float(os.getenv("TV_WEBHOOK_MAX_MARGIN_USD", "2.0"))
TV_DEFAULT_LEV = int(os.getenv("TV_WEBHOOK_DEFAULT_LEVERAGE", "5"))


@app.get("/", response_class=HTMLResponse)
def home():
    """Mobile-optimized dashboard with SSE live log."""
    return """
<!DOCTYPE html>
<html lang="vi"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Trading Bot Live</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; background:#0d1117; color:#c9d1d9; margin:0; padding:0.5rem; font-size:13px; line-height:1.4; }
  h2 { color:#58a6ff; font-size:1rem; margin:0.8rem 0 0.3rem 0; border-bottom:1px solid #30363d; padding-bottom:0.2rem; }
  pre { background:#161b22; color:#c9d1d9; padding:0.5rem; border-radius:6px; overflow-x:auto; font-size:11px; line-height:1.35; white-space:pre-wrap; word-wrap:break-word; max-height:70vh; overflow-y:auto; }
  .pos { color:#3fb950; }
  .neg { color:#f85149; }
  .meta { color:#8b949e; font-size:11px; }
  .nav { display:flex; gap:0.5rem; margin:0.5rem 0; flex-wrap:wrap; }
  .nav a { background:#21262d; color:#58a6ff; padding:0.4rem 0.6rem; border-radius:6px; text-decoration:none; font-size:12px; }
  .status-card { background:#161b22; padding:0.5rem 0.7rem; border-radius:6px; margin:0.4rem 0; border-left:3px solid #58a6ff; }
  .live-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#3fb950; margin-right:6px; animation:pulse 1.5s infinite; }
  @keyframes pulse { 0%, 100% { opacity:1 } 50% { opacity:0.3 } }
  .new-line { background:rgba(63,185,80,0.08); }
</style>
</head><body>
<h2><span class="live-dot"></span>Trading Bot Live</h2>
<div class="nav">
  <a href="/status">Status JSON</a>
  <a href="/log">Full Log</a>
  <a href="/zec">ZEC Monitor</a>
  <a href="/orders">Orders</a>
</div>
<div id="status">Loading status...</div>
<h2>Live Log <span id="stream-state" class="meta">polling...</span></h2>
<pre id="log">(loading...)</pre>
<script>
let lastLogHash = '';
async function loadAll() {
  try {
    const [s, l] = await Promise.all([
      fetch('/status').then(r=>r.json()),
      fetch('/log?n=80').then(r=>r.text())
    ]);
    // status
    let html = '';
    if (s.position) {
      const pnl = parseFloat(s.position.unrealized_pnl);
      const cls = pnl >= 0 ? 'pos' : 'neg';
      html += '<div class="status-card"><b>' + s.position.symbol + ' ' + s.position.side + '</b> qty ' + s.position.qty + '<br>';
      html += 'Entry: $' + s.position.entry_price + ' • Mark: $' + s.position.mark_price + '<br>';
      html += '<span class="' + cls + '">unPnL: $' + s.position.unrealized_pnl + '</span> • Liq: $' + s.position.liquidation_price + '</div>';
    } else {
      html += '<div class="status-card">No open position</div>';
    }
    html += '<div class="status-card meta">Wallet: $' + s.wallet_balance + ' • Available: $' + s.available + '</div>';
    document.getElementById('status').innerHTML = html;
    // log — only re-render if changed
    const logEl = document.getElementById('log');
    const hash = l.length + ':' + l.slice(-100);
    if (hash !== lastLogHash) {
      logEl.textContent = l;
      logEl.scrollTop = logEl.scrollHeight;
      lastLogHash = hash;
    }
    document.getElementById('stream-state').textContent = '● live ' + new Date().toLocaleTimeString();
    document.getElementById('stream-state').style.color = '#3fb950';
  } catch(e) {
    document.getElementById('stream-state').textContent = '○ ' + e;
    document.getElementById('stream-state').style.color = '#f85149';
  }
}
loadAll();
setInterval(loadAll, 2000);    // 2-second polling = effectively real-time
</script>
</body></html>
"""


@app.get("/stream")
async def stream():
    """Server-Sent Events: pushes new log lines as they're written."""
    async def gen():
        # Send last 30 lines first
        if LOG.exists():
            lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-30:]:
                yield f"data: {line}\n\n"

        # Then tail
        if not LOG.exists():
            yield "data: (log file not found)\n\n"
            await asyncio.sleep(2)

        # Watch file size + read new lines
        last_size = LOG.stat().st_size if LOG.exists() else 0
        while True:
            try:
                if LOG.exists():
                    cur_size = LOG.stat().st_size
                    if cur_size > last_size:
                        with LOG.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(last_size)
                            new_data = f.read()
                            for line in new_data.splitlines():
                                yield f"data: {line}\n\n"
                        last_size = cur_size
                    elif cur_size < last_size:
                        # Log was rotated/truncated
                        last_size = 0
                # Heartbeat every 15s to keep connection alive
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)
            except Exception as e:
                yield f"data: [stream error: {e}]\n\n"
                await asyncio.sleep(3)

    return StreamingResponse(gen(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/status")
def status():
    """Position + wallet status."""
    try:
        from tradingagents.binance.client import spot_client
        c = spot_client()
        positions = c.futures_position_information()
        open_pos = [p for p in positions if abs(float(p["positionAmt"])) > 0]
        bal_data = c.futures_account_balance()
        usdt_bal = next((a for a in bal_data if a["asset"] == "USDT"), {})

        out = {
            "wallet_balance": round(float(usdt_bal.get("balance", 0)), 4),
            "available": round(float(usdt_bal.get("availableBalance", 0)), 4),
            "position": None,
        }
        if open_pos:
            p = open_pos[0]
            qty = float(p["positionAmt"])
            out["position"] = {
                "symbol": p["symbol"],
                "side": "LONG" if qty > 0 else "SHORT",
                "qty": abs(qty),
                "entry_price": round(float(p["entryPrice"]), 6),
                "mark_price": round(float(p["markPrice"]), 6),
                "unrealized_pnl": round(float(p["unRealizedProfit"]), 4),
                "liquidation_price": round(float(p["liquidationPrice"]), 6),
            }
        return out
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/log", response_class=PlainTextResponse)
def log(n: int = 50):
    if not LOG.exists():
        return "(log not found)"
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


@app.get("/zec", response_class=PlainTextResponse)
def zec_log(n: int = 50):
    if not ZEC_LOG.exists():
        return "(ZEC monitor not running)"
    lines = ZEC_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


@app.get("/orders")
def orders():
    """Current futures orders."""
    try:
        from tradingagents.binance.client import spot_client
        c = spot_client()
        regular = c.futures_get_open_orders()
        try:
            algo = c._request_futures_api("get", "openAlgoOrders", True, data={})
        except Exception:
            algo = []
        return {"regular": regular, "algo": algo}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _tv_log(msg: str) -> None:
    TV_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    with TV_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


@app.post("/tv-webhook")
async def tv_webhook(req: Request):
    """Receives TradingView alert JSON, validates secret, executes trade.

    Expected JSON body:
      {
        "secret": "<TV_WEBHOOK_SECRET>",
        "action": "LONG" | "SHORT" | "CLOSE",
        "symbol": "ZECUSDT",                    // Binance USDT-M perpetual
        "leverage": 5,                          // optional, default 5
        "margin_usd": 1.5,                      // optional, capped at TV_WEBHOOK_MAX_MARGIN_USD
        "comment": "RSI oversold bounce"        // optional, logged for audit
      }
    """
    if not TV_SECRET:
        raise HTTPException(503, "TV_WEBHOOK_SECRET not configured")

    raw = await req.body()
    try:
        import json
        payload = json.loads(raw)
    except Exception:
        _tv_log(f"REJECT: invalid JSON. body={raw[:200]!r}")
        raise HTTPException(400, "invalid JSON")

    # Auth: constant-time secret compare
    got = str(payload.get("secret", ""))
    if not hmac.compare_digest(got, TV_SECRET):
        _tv_log(f"REJECT: bad secret. ip={req.client.host if req.client else '?'} payload_keys={list(payload.keys())}")
        raise HTTPException(401, "bad secret")

    action = str(payload.get("action", "")).upper()
    symbol = str(payload.get("symbol", "")).upper()
    if action not in ("LONG", "SHORT", "CLOSE"):
        _tv_log(f"REJECT: bad action={action!r}")
        raise HTTPException(400, "action must be LONG/SHORT/CLOSE")
    if not symbol.endswith("USDT"):
        _tv_log(f"REJECT: bad symbol={symbol!r}")
        raise HTTPException(400, "symbol must be Binance USDT-M perp like ZECUSDT")

    leverage = int(payload.get("leverage", TV_DEFAULT_LEV))
    margin = min(float(payload.get("margin_usd", TV_MAX_MARGIN)), TV_MAX_MARGIN)
    comment = str(payload.get("comment", ""))[:200]

    _tv_log(f"RECV: {action} {symbol} lev={leverage}x margin=${margin:.2f} comment={comment!r}")

    try:
        from tradingagents.binance import futures as bf
        from tradingagents.binance.client import spot_client

        if action == "CLOSE":
            c = spot_client()
            positions = c.futures_position_information(symbol=symbol)
            open_p = [p for p in positions if abs(float(p["positionAmt"])) > 0]
            if not open_p:
                _tv_log(f"CLOSE: no open position on {symbol}")
                return {"status": "noop", "reason": "no position open"}
            p = open_p[0]
            qty = abs(float(p["positionAmt"]))
            side = "SELL" if float(p["positionAmt"]) > 0 else "BUY"
            c.futures_create_order(symbol=symbol, side=side, type="MARKET",
                                    quantity=qty, reduceOnly="true")
            _tv_log(f"CLOSED {symbol} qty={qty}")
            return {"status": "closed", "symbol": symbol, "qty": qty}

        # LONG / SHORT
        c = spot_client()
        ticker = c.futures_symbol_ticker(symbol=symbol)
        mark_price = float(ticker["price"])
        if margin * leverage < 5.1:
            leverage = max(leverage, int(5.5 / margin) + 1)
        if action == "LONG":
            res = bf.open_long(symbol, margin, leverage=leverage, isolated=True)
        else:
            res = bf.open_short(symbol, margin, leverage=leverage, isolated=True)
        entry = res.avg_price if res.avg_price > 0 else mark_price
        SL_PCT, TP_PCT = 5.0, 10.0
        if action == "LONG":
            sl_price = entry * (1 - SL_PCT/100)
            tp_price = entry * (1 + TP_PCT/100)
        else:
            sl_price = entry * (1 + SL_PCT/100)
            tp_price = entry * (1 - TP_PCT/100)
        try: bf.place_stop_loss(symbol, sl_price, side_to_close=action)
        except Exception as e: _tv_log(f"  SL fail: {e}")
        try: bf.place_take_profit(symbol, tp_price, side_to_close=action)
        except Exception as e: _tv_log(f"  TP fail: {e}")
        _tv_log(f"OPENED {action} {symbol} qty={res.executed_qty} entry=${entry} SL=${sl_price:.6f} TP=${tp_price:.6f}")
        return {"status": "opened", "symbol": symbol, "action": action,
                 "qty": res.executed_qty, "entry": entry,
                 "sl": sl_price, "tp": tp_price}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _tv_log(f"ERROR executing {action} {symbol}: {err}")
        return JSONResponse({"status": "error", "error": err}, status_code=500)


@app.get("/tv-log", response_class=PlainTextResponse)
def tv_log(n: int = 50):
    if not TV_LOG.exists():
        return "(no webhooks received yet)"
    return "\n".join(TV_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
