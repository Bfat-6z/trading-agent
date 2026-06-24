import argparse
from pathlib import Path

import pytest

from multi_agent_research import build_jobs, run_research, synthesize_offline


def test_build_jobs_splits_waves_and_roles():
    jobs = build_jobs("roadmap", agents=25, wave_size=10)

    assert len(jobs) == 25
    assert jobs[0].index == 1
    assert jobs[0].wave == 1
    assert jobs[9].wave == 1
    assert jobs[10].wave == 2
    assert jobs[20].wave == 3
    assert {job.role_key for job in jobs} >= {"architecture", "risk", "redis_ops"}


def test_build_jobs_caps_runaway_agent_count():
    with pytest.raises(ValueError):
        build_jobs("roadmap", agents=101, wave_size=10)


def test_dry_run_writes_reports_and_summary(tmp_path: Path):
    args = argparse.Namespace(
        topic="test development roadmap",
        agents=6,
        wave_size=3,
        workers=3,
        timeout_sec=10,
        wave_pause_sec=0.0,
        out_dir=str(tmp_path),
        dry_run=True,
        no_llm_summary=True,
        provider=None,
        model=None,
        skip_llm_probe=False,
        fallback_to_dry_run=True,
    )

    result = run_research(args)

    assert result["agents"] == 6
    assert result["ok"] == 6
    assert result["failed"] == 0
    assert Path(result["summary_path"]).exists()
    assert Path(result["jsonl_path"]).exists()
    assert len(list(tmp_path.glob("*-agent-*-ok.md"))) == 6
    assert "Best Development Direction" in Path(result["summary_path"]).read_text(encoding="utf-8")


def test_synthesize_offline_keeps_safety_boundary():
    jobs = build_jobs("roadmap", agents=1, wave_size=1)
    result = argparse.Namespace(
        index=1,
        wave=1,
        role_key=jobs[0].role_key,
        role_name=jobs[0].role_name,
        ok=True,
        elapsed_sec=0.0,
        report="ok",
        error="",
    )

    summary = synthesize_offline("roadmap", [result])

    assert "never for direct order placement" in summary
    assert "SL/TP" in summary
