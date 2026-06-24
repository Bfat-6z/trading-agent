from pathlib import Path

import agent_health_monitor as ahm
import atomic_state
import backup_restore as br
import decision_explainer as de
import dependency_auditor as da
import quota_monitor as qm


def test_agent_health_monitor_marks_stale_missing(tmp_path: Path):
    latest = tmp_path / "health.json"
    history = tmp_path / "health.jsonl"
    result = ahm.evaluate_agent_health([{"name": "x", "heartbeat_file": str(tmp_path / "missing_hb.json"), "latest_file": str(tmp_path / "missing_latest.json")}], output_path=latest, history_path=history)

    assert result["status"] == "degraded"
    assert result["incident_count"] == 1


def test_decision_explainer_finds_post_trade_review(monkeypatch, tmp_path: Path):
    memory_dir = tmp_path / "agent_memory"
    memory_dir.mkdir()
    monkeypatch.setattr(de, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(de, "STATE_DIR", tmp_path)
    monkeypatch.setattr(de, "SOURCES", [("post_trade_review", memory_dir / "post_trade_reviews.jsonl", ["review_id", "trade_id"])])
    atomic_state.append_jsonl(memory_dir / "post_trade_reviews.jsonl", {"review_id": "r1", "trade_id": "t1", "classification": "bad_win"})

    result = de.explain_decision("t1")

    assert result["match_count"] == 1
    assert result["reasons"][0]["reason"] == "bad_win"


def test_backup_and_restore_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(br, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(br, "BACKUP_MANIFESTS", tmp_path / "manifests")
    source = tmp_path / "state.json"
    source.write_text("original", encoding="utf-8")
    manifest = br.backup_files([source])
    source.write_text("changed", encoding="utf-8")
    restored = br.restore_backup(Path(manifest["files"][0]["backup_path"]), source)

    assert restored["ok"] is True
    assert source.read_text(encoding="utf-8") == "original"


def test_dependency_auditor_reports_missing_package(tmp_path: Path):
    result = da.audit_dependencies(["definitely-not-installed-package-xyz"], output_path=tmp_path / "deps.json")

    assert result["ok"] is False
    assert "definitely-not-installed-package-xyz" in result["missing"]


def test_quota_monitor_blocks_exhausted_source(tmp_path: Path):
    result = qm.evaluate_quota("binance", used=100, limit=100, output_path=tmp_path / "quota.json")

    assert result["status"] == "blocked"
    assert "quota_exhausted" in result["errors"]
