# TradingAgents Crypto — Base Chain Meme Trading Bot

Multi-agent LLM trading bot for Base chain memes, forked from [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents).

## Status

Phase 0-5 in progress. See `C:\Users\ACER\.claude\plans\tao-ch-n-b-nh-ng-zippy-penguin.md` for full plan.

## Quick Start

```cmd
cd E:\keo-moi-mail\trading-agent
venv\Scripts\activate

# 1. Setup (one-time)
copy .env.example .env
notepad .env                    # fill in API keys
python secrets/generate_wallet.py
# fund bot wallet with ~$5 ETH on Base from main wallet

# 2. Verify
trading-agent check-balance

# 3. Analyze a token
trading-agent analyze 0xa82138d538cf6e465d0b6915b0d072b1e6910f7d

# 4. Execute (if BUY signal)
trading-agent buy 0xa82138d538cf6e465d0b6915b0d072b1e6910f7d

# 5. Monitor
trading-agent status

# Emergency
trading-agent kill              # halt all trading
trading-agent close-all         # exit all positions
```

## Architecture

8 LLM agents debate via LangGraph:
- Market Analyst (OHLCV + indicators)
- Sentiment Analyst (social + news)
- News Analyst (CryptoPanic)
- On-chain Analyst (holders, contract, honeypot)
- Liquidity Analyst (pool depth, slippage projection)
- Bull/Bear Researchers (debate)
- Risk Debaters (3 personalities)
- Trader + Portfolio Manager (final decision)

Execution via Aerodrome (primary) + Uniswap V3 (fallback) on Base.

## Safety

- Dedicated bot wallet (NOT main wallet)
- Honeypot check (GoPlus) required before any buy
- Hard SL -20% + TP1 +30% + TP2 +60% auto-managed
- Daily loss circuit breaker
- Kill switch file
- KOL heuristic filter

## Cost

~$0.50/day Anthropic API + negligible gas on Base.
