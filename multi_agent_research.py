"""Parallel multi-agent research orchestrator for this trading-agent repo.

The script is read/research-only. It never places orders. It fans out many
independent research jobs in waves, stores one markdown report per job, then
builds a summary roadmap.

Default command for a safe smoke run:
    python multi_agent_research.py --agents 50 --wave-size 10 --workers 10 --dry-run

Remove --dry-run only when you intentionally want to spend LLM calls through
the provider configured in .env.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CRYPTO_SRC = ROOT / "tradingagents_crypto_src"
if str(CRYPTO_SRC) not in sys.path:
    sys.path.insert(0, str(CRYPTO_SRC))

load_dotenv(ROOT / ".env")

ROLE_TEMPLATES = [
    ("architecture", "System architect", "module boundaries, orchestration, state machines, fault isolation"),
    ("data_edge", "Market data edge researcher", "order book, funding, open interest, liquidation, websocket latency"),
    ("risk", "Risk manager", "bankroll math, leverage limits, drawdown stops, tiny account survival"),
    ("execution", "Execution engineer", "Binance futures orders, reduce-only safety, stale algo cleanup, slippage"),
    ("evaluation", "Quant evaluation analyst", "paper logs, walk-forward tests, metrics, false-positive review"),
    ("market_regime", "Market regime analyst", "trend/range filters, BTC beta, volatility regime, no-trade zones"),
    ("news", "News and catalyst researcher", "macro, politics, CPI/FOMC, listings, hacks, token unlocks"),
    ("sentiment", "X/social intelligence engineer", "X ingestion, anti-shill filters, KOL dump risk, source quality"),
    ("redis_ops", "Redis/streaming architect", "queues, pub/sub, time-series cache, worker coordination"),
    ("security", "Security reviewer", "secret hygiene, API permissions, kill switch, tamper-proof logs"),
    ("ops", "24/7 operations engineer", "watchdogs, restart policy, alerting, dashboards, degraded modes"),
    ("product", "Operator workflow designer", "human approval, report clarity, control panel, audit trail"),
]

OFFLINE_RECS = {
    "architecture": [
        "Split the system into research, signal, risk, execution, and monitoring services.",
        "Keep LLM outputs advisory; deterministic gates decide whether an order is allowed.",
        "Persist every agent decision as JSONL so failed trades can be replayed.",
    ],
    "data_edge": [
        "Add websocket ingestion for mark price, bookTicker, aggTrade, funding, and open interest.",
        "Rank symbols by fresh quote volume, spread, volatility, and taker imbalance before LLM review.",
        "Cache multi-timeframe candles so many agents do not hammer Binance endpoints.",
    ],
    "risk": [
        "For a tiny balance, max loss per attempt must be defined in USDT, not emotion or leverage.",
        "Block live entries after two losses, stale algos, missing SL/TP, or abnormal spread.",
        "Track true breakeven including entry fee, exit fee, and expected slippage.",
    ],
    "execution": [
        "Use one live position max until logs prove positive expectancy.",
        "Place reduce-only SL/TP immediately after fill and verify position/order state after every API call.",
        "Use market orders only for ultra-short scalp; otherwise prefer limit/post-only experiments in paper.",
    ],
    "evaluation": [
        "Define A+ pure as a replayable checklist, not an LLM feeling.",
        "Measure win rate, average win/loss, fees, max adverse excursion, and time in trade.",
        "Promote a strategy to live only after forward paper trades beat fees over enough samples.",
    ],
    "market_regime": [
        "Add BTC/ETH regime vetoes so alt scalps do not fight broad market impulse.",
        "Separate breakout continuation from exhaustion fade; they need opposite entry criteria.",
        "No-trade during chop when EMA stack, RSI, and taker flow disagree.",
    ],
    "news": [
        "Use news as veto/catalyst context, not as a naked entry signal.",
        "Track scheduled macro events and pause high leverage around major releases.",
        "Add token unlock, exchange listing, hack/exploit, and regulatory alert feeds.",
    ],
    "sentiment": [
        "Ingest X/social only through source scoring and spam filters.",
        "Treat sudden KOL pile-on as exit-liquidity risk unless confirmed by volume structure.",
        "Store sentiment snapshots with timestamps so claims can be audited after the trade.",
    ],
    "redis_ops": [
        "Use Redis streams for market ticks, signal candidates, research tasks, and execution intents.",
        "Put idempotency keys on execution intents to prevent duplicate entries after restart.",
        "Keep hot state in Redis but write immutable trade/audit logs to disk or database.",
    ],
    "security": [
        "Restrict API keys to futures trading only, no withdrawal permission.",
        "Never print .env values in reports or logs.",
        "Add a kill-switch file and emergency close script with dry-run preview.",
    ],
    "ops": [
        "Run scanner, researcher, executor, and monitor as separate supervised processes.",
        "Alert on open position without SL, API errors, websocket stale time, and drawdown limit hits.",
        "Dashboard should show current regime, candidate queue, live risk state, and last veto reason.",
    ],
    "product": [
        "Operator UI should default to observe/paper, with live toggles requiring explicit acknowledgement.",
        "Reports should say why a trade was rejected as clearly as why it was accepted.",
        "Add one-command nightly research summary with next engineering priorities.",
    ],
}


@dataclass(frozen=True)
class AgentJob:
    index: int
    wave: int
    role_key: str
    role_name: str
    focus: str
    topic: str


@dataclass
class AgentResult:
    index: int
    wave: int
    role_key: str
    role_name: str
    ok: bool
    elapsed_sec: float
    report: str
    error: str = ""


def utc_slug() -> str:
    return datetime.now(timezone.utc).strftime("%y%m%d-%H%M%S")


def slugify(value: str, max_len: int = 64) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return (value or "research")[:max_len].strip("-")


def build_jobs(topic: str, agents: int, wave_size: int) -> list[AgentJob]:
    if agents < 1:
        raise ValueError("agents must be >= 1")
    if agents > 100:
        raise ValueError("agents must be <= 100")
    if wave_size < 1:
        raise ValueError("wave_size must be >= 1")

    jobs: list[AgentJob] = []
    for zero_idx in range(agents):
        role_key, role_name, base_focus = ROLE_TEMPLATES[zero_idx % len(ROLE_TEMPLATES)]
        pass_no = zero_idx // len(ROLE_TEMPLATES) + 1
        focus = base_focus if pass_no == 1 else f"{base_focus}; pass {pass_no}: find missed gaps"
        jobs.append(
            AgentJob(
                index=zero_idx + 1,
                wave=zero_idx // wave_size + 1,
                role_key=role_key,
                role_name=role_name,
                focus=focus,
                topic=topic,
            )
        )
    return jobs


def project_context() -> str:
    return dedent(
        """
        Project context:
        - Python crypto/futures trading-agent workspace.
        - Multi-agent LLM pipeline: tradingagents_crypto_src/tradingagents/crypto/agents.py.
        - Binance snapshot adapter: tradingagents_crypto_src/tradingagents/binance/data.py.
        - Existing scanner: scan_and_analyze.py.
        - 24/7 scalp engine: scalp_autotrader.py.
        - Default scalp engine mode is PAPER; live requires --live --i-understand-risk.
        - Safety rules: no naked futures position, always SL/TP, stop after repeated losses,
          avoid XPLUSDT and LINKUSDT until stale reduce-only algos are manually cleared.
        - LLM layer should produce research, bias, candidate whitelists, and risk vetoes.
          Mechanical execution should remain deterministic with circuit breakers.
        - Account is tiny; high leverage is survivable only if entries are rare, confirmed,
          and losses are capped before fees destroy expectancy.
        """
    ).strip()


def offline_report(job: AgentJob) -> str:
    recs = OFFLINE_RECS.get(job.role_key, ["Collect evidence before changing live execution."])
    bullets = "\n".join(f"- {item}" for item in recs)
    return dedent(
        f"""
        # Agent {job.index}: {job.role_name}

        Focus: {job.focus}

        ## Findings
        {bullets}

        ## Recommended Next Build Step
        Convert this angle into a measurable gate, queue, metric, or alert before allowing it to affect live orders.

        ## Risk Note
        This agent may only write research, vetoes, candidate scores, or configs consumed by deterministic execution code.
        """
    ).strip()


def llm_report(job: AgentJob) -> str:
    from tradingagents.crypto.agents import _call_llm

    system = (
        "You are one independent research agent in a large parallel trading-system review. "
        "Be concrete, skeptical, and implementation-oriented. Do not recommend blind all-in trading. "
        "Your output must be a concise markdown report."
    )
    user = f"""{project_context()}

Research topic: {job.topic}
Your role: {job.role_name}
Your focus: {job.focus}
Agent number: {job.index}
Wave: {job.wave}

Return markdown with these sections:
1. Key findings
2. What to build next
3. Data needed
4. Risk / failure mode
5. Acceptance test

Keep it under 700 words and make every recommendation testable."""
    return _call_llm(system, user, max_tokens=1200, temperature=0.25)


def apply_llm_overrides(args: argparse.Namespace) -> None:
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider
    if args.model:
        os.environ["TRADINGAGENTS_QUICK_THINK_LLM"] = args.model
        os.environ["TRADINGAGENTS_DEEP_THINK_LLM"] = args.model
        os.environ["TRADINGAGENTS_JUDGE_LLM"] = args.model


def probe_llm() -> tuple[bool, str]:
    """Return whether the configured LLM can answer a tiny request."""
    try:
        from tradingagents.crypto import agents

        agents._client = None
        agents._provider = None
        provider = agents._detect_provider()
        model = agents._resolve_model(provider, "quick")
        text = agents._call_llm("Answer with one word.", "Say OK.", max_tokens=20, temperature=0)
        if not text.strip():
            return False, f"provider={provider} model={model}: empty response"
        return True, f"provider={provider} model={model}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:300]}"


def run_job(job: AgentJob, dry_run: bool) -> AgentResult:
    start = time.time()
    try:
        report = offline_report(job) if dry_run else llm_report(job)
        return AgentResult(job.index, job.wave, job.role_key, job.role_name, True, time.time() - start, report.strip())
    except Exception as exc:
        return AgentResult(
            job.index,
            job.wave,
            job.role_key,
            job.role_name,
            False,
            time.time() - start,
            "",
            f"{type(exc).__name__}: {str(exc)[:500]}",
        )


def write_agent_report(out_dir: Path, run_id: str, result: AgentResult) -> Path:
    status = "ok" if result.ok else "failed"
    path = out_dir / f"{run_id}-agent-{result.index:03d}-{result.role_key}-{status}.md"
    body = result.report if result.ok else f"# Agent {result.index} failed\n\n{result.error}\n"
    path.write_text(body + "\n", encoding="utf-8")
    return path


def append_jsonl(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")


def synthesize_offline(topic: str, results: list[AgentResult]) -> str:
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    by_role: dict[str, int] = {}
    for result in ok:
        by_role[result.role_key] = by_role.get(result.role_key, 0) + 1
    role_lines = "\n".join(f"- {role}: {count} agent(s)" for role, count in sorted(by_role.items())) or "- none"
    return f"""# Multi-Agent Research Summary

Topic: {topic}

## Run Stats
- Completed agents: {len(ok)}
- Failed agents: {len(failed)}
- Role coverage:
{role_lines}

## Best Development Direction
1. Make the system evidence-first: market data ingestion, deterministic A+ pure gates, paper metrics, then live execution.
2. Use many LLM agents for research, vetoes, scenario review, and roadmap generation, but never for direct order placement.
3. Add Redis streams or an equivalent queue before scaling: market_tick -> candidate_signal -> research_report -> risk_veto -> execution_intent -> monitor_event.
4. Upgrade data edge before leverage: websocket order book, taker flow, funding, open interest, BTC/ETH regime, macro/news calendar, X/source scoring.
5. Treat the current tiny balance as validation capital. The win condition is clean logs and positive expectancy after fees, not forcing trades.

## Immediate Build Queue
- P0: Add a queue/state layer for candidate signals and agent reports.
- P0: Add hard preflight checks for stale algos, open SL/TP, max loss, spread, and scheduled news risk.
- P1: Add websocket market collector and feature cache.
- P1: Add replay/backtest over JSONL paper/live logs.
- P1: Add X/news ingestion with source scoring and catalyst vetoes.
- P2: Build a dashboard showing regime, candidates, vetoes, open risk, and monitor status.

## Non-Negotiable Safety Boundary
Research agents can produce recommendations and configs. scalp_autotrader.py or a future executor must remain the only component allowed to place orders, and only with SL/TP plus circuit breakers.
""".strip()


def synthesize_with_llm(topic: str, results: list[AgentResult]) -> str:
    from tradingagents.crypto.agents import _call_llm

    packed = []
    for result in results:
        if not result.ok:
            packed.append(f"Agent {result.index} {result.role_key} FAILED: {result.error}")
        else:
            packed.append(f"Agent {result.index} {result.role_key}:\n{result.report[:1800]}")
    system = "You are the lead engineer synthesizing many independent research agents into a practical roadmap."
    user = f"""Topic: {topic}

Agent reports:
{chr(10).join(packed)}

Write one markdown summary with:
- executive summary
- converged recommendations
- contradictions or tradeoffs
- prioritized implementation roadmap P0/P1/P2
- acceptance tests
- safety boundary for live futures trading

Be direct and practical."""
    return _call_llm(system, user, max_tokens=2200, temperature=0.2)


def run_research(args: argparse.Namespace) -> dict:
    apply_llm_overrides(args)
    if not args.dry_run and not args.skip_llm_probe:
        ok, detail = probe_llm()
        if ok:
            print(f"LLM probe OK: {detail}", flush=True)
        elif args.fallback_to_dry_run:
            print(f"LLM probe failed, switching to --dry-run fallback: {detail}", flush=True)
            args.dry_run = True
            args.no_llm_summary = True
        else:
            raise RuntimeError(f"LLM probe failed: {detail}")

    run_id = f"{utc_slug()}-{slugify(args.topic, 32)}"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"{run_id}.jsonl"

    jobs = build_jobs(args.topic, args.agents, args.wave_size)
    results: list[AgentResult] = []
    total_waves = max(job.wave for job in jobs)

    print(f"Run {run_id}: {len(jobs)} agents, {total_waves} wave(s), dry_run={args.dry_run}", flush=True)
    for wave in range(1, total_waves + 1):
        wave_jobs = [job for job in jobs if job.wave == wave]
        print(f"Wave {wave}/{total_waves}: launching {len(wave_jobs)} agents", flush=True)
        with ThreadPoolExecutor(max_workers=min(args.workers, len(wave_jobs))) as executor:
            future_to_job = {executor.submit(run_job, job, args.dry_run): job for job in wave_jobs}
            for future in as_completed(future_to_job):
                job = future_to_job[future]
                try:
                    result = future.result(timeout=args.timeout_sec)
                except Exception as exc:
                    result = AgentResult(
                        job.index,
                        job.wave,
                        job.role_key,
                        job.role_name,
                        False,
                        0.0,
                        "",
                        f"future_error: {type(exc).__name__}: {str(exc)[:500]}",
                    )
                results.append(result)
                report_path = write_agent_report(out_dir, run_id, result)
                append_jsonl(jsonl_path, {"run_id": run_id, "report_path": str(report_path), **asdict(result)})
                state = "OK" if result.ok else "FAIL"
                print(f"  [{state}] agent {result.index:03d} wave={result.wave} role={result.role_key} {result.elapsed_sec:.1f}s", flush=True)
        if args.wave_pause_sec > 0 and wave < total_waves:
            time.sleep(args.wave_pause_sec)

    results.sort(key=lambda result: result.index)
    if args.dry_run or args.no_llm_summary:
        summary = synthesize_offline(args.topic, results)
    else:
        try:
            summary = synthesize_with_llm(args.topic, results)
        except Exception as exc:
            summary = synthesize_offline(args.topic, results)
            summary += f"\n\n## LLM Summary Fallback\nSynthesis LLM failed: {type(exc).__name__}: {str(exc)[:300]}\n"

    summary_path = out_dir / f"{run_id}-summary.md"
    summary_path.write_text(summary.strip() + "\n", encoding="utf-8")
    print(f"Summary written: {summary_path}", flush=True)
    return {
        "run_id": run_id,
        "summary_path": str(summary_path),
        "jsonl_path": str(jsonl_path),
        "agents": len(jobs),
        "ok": sum(1 for result in results if result.ok),
        "failed": sum(1 for result in results if not result.ok),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run many parallel research agents in waves.")
    parser.add_argument("--topic", default="best development direction for a 24/7 AI futures trading agent")
    parser.add_argument("--agents", type=int, default=50, help="number of research agents, max 100")
    parser.add_argument("--wave-size", type=int, default=10, help="agents per wave")
    parser.add_argument("--workers", type=int, default=10, help="parallel workers inside each wave")
    parser.add_argument("--timeout-sec", type=int, default=120)
    parser.add_argument("--wave-pause-sec", type=float, default=0.0)
    parser.add_argument("--out-dir", default=str(ROOT / "plans" / "reports"))
    parser.add_argument("--dry-run", action="store_true", help="do not call LLM; produce deterministic offline reports")
    parser.add_argument("--no-llm-summary", action="store_true", help="summarize without an extra LLM call")
    parser.add_argument("--provider", choices=["9router", "openrouter", "anthropic", "openai", "custom"], help="override LLM_PROVIDER for this run")
    parser.add_argument("--model", help="override quick/deep/judge model for this run")
    parser.add_argument("--skip-llm-probe", action="store_true", help="start jobs without the small preflight LLM request")
    parser.add_argument("--fallback-to-dry-run", action=argparse.BooleanOptionalAction, default=True, help="fallback to offline reports if LLM auth/provider probe fails")
    args = parser.parse_args(argv)
    if args.agents > 100:
        parser.error("--agents is capped at 100 to avoid accidental runaway spend")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.wave_size < 1:
        parser.error("--wave-size must be >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_research(args)
    print(json.dumps(result, indent=2, ensure_ascii=True), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
