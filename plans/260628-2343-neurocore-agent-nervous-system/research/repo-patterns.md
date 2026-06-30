# Repo Pattern Research

## Sources

- Hermes Agent: https://github.com/NousResearch/hermes-agent
- Hermes self-evolution: https://github.com/NousResearch/hermes-agent-self-evolution
- OpenClaw Auto-Dream: https://github.com/LeoYeAI/openclaw-auto-dream
- OpenClaw Dreaming docs: https://docs.openclaw.ai/concepts/dreaming
- Obsidian Skills: https://github.com/kepano/obsidian-skills
- LangGraph: https://github.com/langchain-ai/langgraph
- AutoGen: https://github.com/microsoft/autogen
- CrewAI: https://github.com/crewAIInc/crewAI
- CAMEL: https://github.com/camel-ai/camel
- mem0: https://github.com/mem0ai/mem0
- Letta: https://github.com/letta-ai/letta
- vectorbt: https://github.com/polakowo/vectorbt
- Qlib: https://github.com/microsoft/qlib
- NautilusTrader: https://github.com/nautechsystems/nautilus_trader
- Freqtrade: https://github.com/freqtrade/freqtrade
- Hummingbot: https://github.com/hummingbot/hummingbot
- OpenBB: https://github.com/OpenBB-finance/OpenBB
- Langfuse: https://github.com/langfuse/langfuse
- Opik: https://github.com/comet-ml/opik
- AgentOps: https://github.com/AgentOps-AI/agentops
- promptfoo: https://github.com/promptfoo/promptfoo
- MCP: https://modelcontextprotocol.io/docs/getting-started/intro

## Pattern Mapping

| Pattern | Repo examples | Apply here |
| --- | --- | --- |
| Durable state graph | LangGraph, NautilusTrader | Keep stdlib scripts, but formalize workflow state and event ids. |
| Skill memory | Hermes, Obsidian Skills | Turn setup skills into versioned procedural contracts. |
| Sleep/dream memory | OpenClaw | Light ingest, REM theme extraction, Deep evidence-gated promotion. |
| High-throughput research | vectorbt, Qlib, Freqtrade hyperopt | Experiment Swarm with variant hashes and OOS validation. |
| Realistic dry run | Freqtrade, Hummingbot, NautilusTrader | Paper simulator must include fee, funding, slippage, filters, liquidation. |
| Agent observability | Langfuse, Opik, AgentOps | Trace every LLM/tool/risk/memory step with run ids and quality gates. |
| Eval regression | promptfoo | Prompt/council/sanitizer red-team fixtures in CI. |
| Tool boundary | MCP | Read-only tools for market state, memory search, paper proposals. |

## Hard Rule

Borrow architecture patterns only. Do not replace the current repo with a framework. The current system is already stdlib-heavy and test-rich; the next version should standardize it, not restart it.
