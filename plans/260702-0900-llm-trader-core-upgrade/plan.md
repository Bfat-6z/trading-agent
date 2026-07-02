# LLM Trader Core Upgrade — rút đầy đủ lõi kỹ thuật từ 4 repo vibe-trading

Date: 2026-07-02. Owner: user (rules cứng). Executor: Claude.
Sources audited (all SAFE): HKUDS/Vibe-Trading, VibeTradingLabs/vibetrading,
spyderweb47/Vibe-Trade, hopit-ai/india-trade-cli. We REIMPLEMENT natively —
never install/run their code (exec() caveats in 2 repos).

## Owner rules (bất biến, enforce trong code)
- Size 5–10% equity/lệnh; leverage CHỈ x5 hoặc x10; tần suất cao (loop 300s).
- PAPER-ONLY: không sửa live_guard, không set ALLOW_LIVE_ORDERS, không gọi
  futures_create_order. llm_trader có account riêng ($100).

## Full extraction checklist (KHÔNG THIẾU MỤC NÀO)
| # | Kỹ thuật | Nguồn | Đích |
|---|---|---|---|
| 1 | Forced liquidation @ maintenance margin, liq-price isolated, pessimistic liq-before-SL | HKUDS engines/crypto.py | llm_trader_risk.py |
| 2 | Funding fee per 8h while open | HKUDS crypto.py + VTL static_sandbox | llm_trader_risk.py |
| 3 | Per-coin fee tier + slippage entry&exit | VTL cost realism; fix hopit's zero-fee gap | llm_trader_risk.py |
| 4 | Fail-closed pre-trade caps: total margin, max concurrent, daily-loss breaker | HKUDS live/enforcement.py + hopit risk_limits | llm_trader_risk.py |
| 5 | Metrics single-source: expectancy, win rate, profit factor, per-trade Sharpe, maxDD, streaks | VTL calculator.py + spyder metrics.py | llm_trader_scorecard.py |
| 6 | Bootstrap CI (mean R) | HKUDS validation.py, hopit backtest_advanced | llm_trader_scorecard.py |
| 7 | Permutation/sign-flip p-value (edge vs luck) | HKUDS Monte-Carlo permutation | llm_trader_scorecard.py |
| 8 | Benchmark comparison (BTC buy-hold excess) | HKUDS information_ratio; hopit vs-buy-hold alpha | scorecard (series injected by integrator) |
| 9 | Honest verdict ladder (INSUFFICIENT/NEGATIVE/INCONCLUSIVE/PROMISING; never "proven") | our discipline | llm_trader_scorecard.py |
| 10 | Persistent learning: aggregate stats by symbol/regime/hour/side/leverage from ALL closed trades | HKUDS memory/persistent.py (RAG-lite) | llm_trader_memory.py |
| 11 | Distilled data-lessons (counts, not AI self-eval) + recent-trades detail w/ rationale-vs-outcome | HKUDS typed notes; hopit RAG-inject | llm_trader_memory.py |
| 12 | Contextual (non-blanket-ban) framing preserved in prompt | owner requirement | integration |
| 13 | Closed-bars-only decisions (drop incomplete last kline) | VTL time-gating; hopit shift(1) | integration (build_context) |
| 14 | Automated outcome capture (already exists — keep) | fixes hopit's manual gap | llm_trader.py |
| 15 | Scorecard/caps surfaced to LLM prompt (capacity awareness) | HKUDS mandate visibility | integration (decide) |
| 16 | Dashboard: llm_trader panel (equity, WR, verdict, positions+liq px) | — | horizon_data.py + index.html |
| 17 | Tests: liq math, ordering, funding sign, caps fail-closed, scorecard determinism+rules, memory grouping, no-live-order static check | — | tests/ |
| Not applicable | Walk-forward/overfitting_ratio (backtest-only concept; llm_trader is forward-only → scorecard IS its out-of-sample) · backtest/live parity (no backtester here) | documented honestly | — |

## Module API contracts (integrator phụ thuộc — KHÔNG đổi signature)

### llm_trader_risk.py (pure functions, no I/O, no network)
- `mmr_for(symbol: str) -> float` — 0.005 BTC/ETH, else 0.01 (pessimistic).
- `liquidation_price(entry: float, leverage: int, side: str, mmr: float) -> float`
  LONG: entry*(1 - 1/lev + mmr); SHORT: entry*(1 + 1/lev - mmr).
- `exit_check(bar: dict, side: str, liq_px: float, sl: float, tp: float) -> tuple[float, str] | None`
  bar has high/low floats. Pessimistic order: liquidation → sl → tp. Returns (exit_px, reason) or None.
- `funding_cost(side: str, qty: float, entry_px: float, events: list[tuple[int, float]], t0_ms: int, t1_ms: int) -> float`
  events=(ts_ms, rate). Charge each event with t0<ts<=t1: LONG pays +rate*qty*entry_px, SHORT pays -rate*... (negative=receive). Returns total cost (positive=cost).
- `trade_costs(entry: float, exit_px: float, qty: float, quote_vol: float) -> dict`
  Uses paper_cost_model liquidity_tier(quote_vol)+fill_bps+TAKER_FEE_RATE. Returns {fee, slip_bps, tier}. Slippage handled by integrator via fill prices; fee = taker both legs.
- `net_pnl(side, entry, exit_px, qty, margin, fee, funding, liquidated: bool) -> float`
  gross ± ; net = gross - fee - funding; if liquidated → net = -margin (floor); always net >= -margin (isolated).
- `can_open(new_margin: float, equity: float, open_positions: list[dict], max_total_margin_pct=60.0, max_concurrent=4) -> tuple[bool, str]`
- `daily_breaker(closed: list[dict], equity_day_start: float, now_ms: int, max_daily_loss_pct=15.0) -> tuple[bool, str]`
  True = TRADING BLOCKED today (UTC day of now_ms). Fail-closed on malformed rows.

### llm_trader_scorecard.py (pure, deterministic seed=7)
- `basic_metrics(closed: list[dict]) -> dict` — uses r & net keys; n, wins, win_rate, mean_r, expectancy_usd, profit_factor, sharpe_trade, max_dd_usd, max_win_streak, max_loss_streak, liq_count.
- `bootstrap_ci(rs: list[float], iters=5000, alpha=0.05, seed=7) -> tuple[float, float]`
- `permutation_pvalue(rs: list[float], iters=5000, seed=7) -> float` — one-sided sign-flip: P(mean_flipped >= mean_obs).
- `verdict(metrics: dict, ci: tuple, pvalue: float, min_trades=30) -> dict {code, detail}`
  codes: INSUFFICIENT_DATA | NEGATIVE | INCONCLUSIVE | PROMISING (ci_low>0 AND p<0.05 AND n>=min_trades). NEVER "edge proven".
- `scorecard(closed: list[dict], benchmark: dict | None = None) -> dict` — benchmark optional {btc_ret_pct, agent_ret_pct, excess_pct} computed by integrator.

### llm_trader_memory.py (pure)
- `aggregate_stats(closed: list[dict]) -> dict` — groups: by_symbol, by_regime, by_hour_bucket (0-5/6-11/12-17/18-23), by_side, by_leverage, by_symbol_side. Each {n, wins, win_rate, mean_r, total_net}; only n>=2 groups.
- `distill_lessons(stats: dict, min_n=3, max_lines=12) -> list[str]` — data-phrased ("SOLUSDT SHORT: 1W/4L, mean -0.52R"), sorted by evidence weight |mean_r|*n. NO prescriptive "never do X" (contextual, not blanket-ban).
- `recent_trades(closed: list[dict], k=10) -> list[dict]` — symbol/side/regime/hour/R/reason + rationale ≤120 chars.
- `build_memory_context(closed: list[dict]) -> dict` — {"stats": trimmed, "lessons": [...], "recent": [...]} compact (≤ ~2000 chars JSON when 100 trades).

## Integration (Claude làm sau khi modules xong; file llm_trader.py)
- build_context: fetch limit+1, DROP incomplete last bar; capture per-symbol quote_volume for fee tier.
- decide prompt: + scorecard summary + capacity (free margin, open/max, breaker) + memory context (replaces 8-row relevant_lessons).
- open_positions: enforce can_open + daily_breaker (fail-closed); store liq_px, mmr, quote_vol, day-start equity.
- resolve: exit_check per closed bar (liq→sl→tp), funding via of.fetch_funding_series adapted to events list, per-coin fees, net_pnl floor; write fees/funding/liq into closed rec; recompute+persist scorecard.json; heartbeat carries verdict.
- horizon_data.py + horizon-ui/index.html: llm_trader panel (REAL numbers, honest labels).

## Acceptance criteria
1. All new unit tests pass + existing suite not broken.
2. Liquidation: LONG x10 mmr1% → liq at −9.0% from entry; bar touching liq&sl → reason="liquidation"; net == −margin.
3. Funding sign: LONG pays positive rate; SHORT receives.
4. can_open: 4 open or margin>60% → (False, reason). daily_breaker blocks after −15% day.
5. Scorecard deterministic (same input → same CI/p). All-positive rs → p < 0.01. n=5 → INSUFFICIENT_DATA.
6. Prompt context ≤ reasonable size; lessons contain counts, no "never".
7. Static safety test: llm_trader* files contain no futures_create_order / no ALLOW_LIVE_ORDERS assignment.
8. Loop restarted with new code; dashboard shows llm_trader panel with real state.
