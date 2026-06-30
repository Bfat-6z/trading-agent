import json
from pathlib import Path

import pytest

import event_store as es
import obsidian_vault_writer as ovw
from atomic_state import append_jsonl, read_json
from setup_skill_library import default_library, load_library


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def one_skill_library(evidence_id: str = "post_trade_review:review_1", description: str = "Clean setup") -> dict:
    library = default_library()
    skill = library["skills"]["momentum_continuation"]
    skill["description"] = description
    skill["stats"]["recent"] = [{"evidence_id": evidence_id, "net": 1.0}]
    library["skills"] = {"momentum_continuation": skill}
    return library


def sync_fixture(tmp_path: Path, *, library: dict | None = None, memory_rows: list[dict] | None = None, dont_do: dict | None = None) -> tuple[Path, dict]:
    data = tmp_path / "data"
    library_path = data / "setup_skills.json"
    promoted_path = data / "memory_promoted.jsonl"
    dont_do_path = data / "dont_do_memory.json"
    daily_path = data / "daily_exam_latest.json"
    experiments_path = data / "experiments.jsonl"
    library = library or one_skill_library()
    write_json(library_path, library)
    review_ids = []
    for skill in (library.get("skills") or {}).values():
        stats = skill.get("stats") if isinstance(skill, dict) and isinstance(skill.get("stats"), dict) else {}
        for row in stats.get("recent", []) if isinstance(stats.get("recent"), list) else []:
            eid = str(row.get("evidence_id") or "")
            if eid.startswith("post_trade_review:"):
                review_ids.append(eid.split(":", 1)[1])
    for review_id in sorted(set(review_ids)):
        append_jsonl(data / "post_trade_reviews.jsonl", {"review_id": review_id, "summary": "objective review", "outcome_known_at": "2026-06-28T00:00:00+00:00"})
    for row in memory_rows or []:
        append_jsonl(promoted_path, row)
    write_json(dont_do_path, dont_do or {"rules": []})
    vault = tmp_path / "vault"
    manifest = ovw.sync_vault(
        vault_root=vault,
        export_mode="public_redacted",
        library_path=library_path,
        promoted_path=promoted_path,
        dont_do_path=dont_do_path,
        daily_path=daily_path,
        experiments_path=experiments_path,
        allow_external_source_paths_for_tests=True,
        generated_at="2026-06-29T00:00:00+00:00",
    )
    return vault, manifest


def test_sync_exports_skill_contract_with_digest_evidence_and_lf(tmp_path: Path):
    evidence_id = "post_trade_review:review_1"
    vault, manifest = sync_fixture(tmp_path, library=one_skill_library(evidence_id=evidence_id))

    skill_path = vault / "skills" / "momentum-continuation-1-0-0.md"
    text = skill_path.read_text(encoding="utf-8")
    raw = skill_path.read_bytes()

    source_library = load_library(tmp_path / "data" / "setup_skills.json")
    expected_digest = ovw.digest_payload(source_library["skills"]["momentum_continuation"])
    assert manifest["ok"] is True
    assert skill_path.exists()
    assert f'source_digest: "{expected_digest}"' in text
    assert "as_of_seq: 1" in text
    assert 'source_file_sha256: "sha256:' in text
    assert 'source_snapshot_hash: "sha256:' in text
    assert evidence_id in text
    assert ovw.GENERATED_MARKER in text
    assert b"\r\n" not in raw
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert manifest["can_mutate_runtime"] is False
    assert manifest["can_place_live_orders"] is False


def test_vault_writer_never_reads_env(tmp_path: Path, monkeypatch):
    original = Path.read_text

    def guard(self: Path, *args, **kwargs):
        if self.name == ".env":
            raise AssertionError(".env was read")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guard)

    vault, manifest = sync_fixture(tmp_path)

    assert manifest["ok"] is True
    assert (vault / ovw.MANIFEST_NAME).exists()


def test_source_path_env_argument_is_refused_before_read(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=sk-should-not-read", encoding="utf-8")

    with pytest.raises(ovw.VaultExportError) as err:
        ovw.sync_vault(
            vault_root=tmp_path / "vault",
            library_path=env_path,
            promoted_path=tmp_path / "memory_promoted.jsonl",
            dont_do_path=tmp_path / "dont_do.json",
            daily_path=tmp_path / "daily.json",
            experiments_path=tmp_path / "experiments.jsonl",
            allow_external_source_paths_for_tests=True,
        )

    assert "sensitive_source_path:.env" in str(err.value)


def test_bundle_secret_scan_redacts_sentinels(tmp_path: Path):
    library = one_skill_library()
    secret_skill = dict(library["skills"]["momentum_continuation"])
    secret_skill["setup_id"] = "secret_setup"
    secret_skill["setup_contract_id"] = "secret_setup.contract.v1"
    secret_skill["name"] = "Secret Setup"
    secret_skill["description"] = "Never leak sk-secretabcd1234 or SENTINEL_SECRET_DO_NOT_LEAK"
    library["skills"]["secret_setup"] = secret_skill
    vault, manifest = sync_fixture(tmp_path, library=library)
    rendered = (vault / "skills" / "secret-setup-1-0-0.md").read_text(encoding="utf-8")

    assert manifest["ok"] is True
    assert "sk-secretabcd1234" not in rendered
    assert "SENTINEL_SECRET_DO_NOT_LEAK" not in rendered
    assert "[REDACTED_SECRET]" in rendered


def test_private_vault_refuses_git_tree(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    with pytest.raises(ovw.VaultExportError) as err:
        ovw.sync_vault(vault_root=repo / "vault", export_mode="private")

    assert "private_vault_inside_git_tree" in str(err.value)


def test_generated_skill_edit_creates_conflict_and_does_not_mutate_runtime(tmp_path: Path):
    library = one_skill_library()
    vault, first = sync_fixture(tmp_path, library=library)
    skill_path = vault / "skills" / "momentum-continuation-1-0-0.md"
    skill_path.write_text(skill_path.read_text(encoding="utf-8") + "\nHUMAN EDIT tries to change risk_version.\n", encoding="utf-8")

    data = tmp_path / "data"
    second = ovw.sync_vault(
        vault_root=vault,
        export_mode="public_redacted",
        library_path=data / "setup_skills.json",
        promoted_path=data / "memory_promoted.jsonl",
        dont_do_path=data / "dont_do_memory.json",
        daily_path=data / "daily_exam_latest.json",
        experiments_path=data / "experiments.jsonl",
        allow_external_source_paths_for_tests=True,
        generated_at="2026-06-29T00:05:00+00:00",
    )

    runtime = read_json(data / "setup_skills.json")
    conflicts = (vault / "quarantine" / ovw.CONFLICT_HISTORY_NAME).read_text(encoding="utf-8")
    skill_artifact = next(row for row in second["artifacts"] if row["path"] == "skills/momentum-continuation-1-0-0.md")

    assert first["artifacts"][0]["file_sha256"] != ovw.file_sha256(skill_path)
    assert "vault.generated_conflict" in conflicts
    assert skill_artifact["conflict"]["status"] == "quarantined"
    assert runtime == library


def test_generated_conflict_emits_registered_event(tmp_path: Path):
    library = one_skill_library()
    vault, _ = sync_fixture(tmp_path, library=library)
    skill_path = vault / "skills" / "momentum-continuation-1-0-0.md"
    skill_path.write_text(skill_path.read_text(encoding="utf-8") + "\nmanual drift\n", encoding="utf-8")
    bus = tmp_path / "bus.db"

    ovw.sync_vault(
        vault_root=vault,
        export_mode="public_redacted",
        library_path=tmp_path / "data" / "setup_skills.json",
        promoted_path=tmp_path / "data" / "memory_promoted.jsonl",
        dont_do_path=tmp_path / "data" / "dont_do_memory.json",
        daily_path=tmp_path / "data" / "daily_exam_latest.json",
        experiments_path=tmp_path / "data" / "experiments.jsonl",
        event_db_path=bus,
        allow_external_source_paths_for_tests=True,
        generated_at="2026-06-29T00:05:00+00:00",
    )

    replay = es.replay_events(db_path=bus, event_types=["vault.generated_conflict"])
    assert replay["count"] == 1
    assert replay["events"][0]["payload"]["path"] == "skills/momentum-continuation-1-0-0.md"


def test_human_note_import_is_quarantined_sanitized_and_untrusted(tmp_path: Path):
    vault, _ = sync_fixture(tmp_path)
    note = vault / "inbox" / "operator-note.md"
    note.write_text(
        """---
signer: "human:test"
scope: "skill_feedback"
evidence_refs:
  - "good_evidence"
  - "bad_evidence"
expires_at: "2026-07-01T00:00:00+00:00"
---
[[prompt injection]]
```python
print("own runtime")
```
dataview TABLE FROM "secrets"
SENTINEL_SECRET_DO_NOT_LEAK
""",
        encoding="utf-8",
    )

    row = ovw.import_human_note(
        note,
        vault_root=vault,
        allowed_evidence_ids=["good_evidence"],
        event_db_path=tmp_path / "feedback_bus.db",
        imported_at="2026-06-29T00:00:00+00:00",
    )

    rendered = json.dumps(row, ensure_ascii=True)
    replay = es.replay_events(db_path=tmp_path / "feedback_bus.db", event_types=["human_feedback.imported"])
    assert row["status"] == "quarantined"
    assert row["feedback_id"].startswith("feedback_")
    assert row["source_id"] == "obsidian_vault_inbox"
    assert replay["count"] == 1
    assert row["can_mutate_runtime"] is False
    assert any(error.startswith("invalid_evidence_ref_hash:") for error in row["errors"])
    assert "bad_evidence" not in rendered
    assert "human:test" not in rendered
    assert "skill_feedback" not in rendered
    assert "[[" not in row["sanitized_body"]
    assert "```" not in row["sanitized_body"]
    assert "SENTINEL_SECRET_DO_NOT_LEAK" not in rendered


def test_human_import_rejects_generated_notes_and_outside_inbox(tmp_path: Path):
    vault, _ = sync_fixture(tmp_path)
    generated = vault / "skills" / "momentum-continuation-1-0-0.md"

    with pytest.raises(ovw.VaultExportError):
        ovw.import_human_note(generated, vault_root=vault)


def test_unicode_controls_are_stripped_from_import(tmp_path: Path):
    vault, _ = sync_fixture(tmp_path)
    note = vault / "inbox" / "unicode.md"
    note.write_text("---\nsigner: \"human:test\"\n---\nabc\u202edef\u200bghi", encoding="utf-8")

    row = ovw.import_human_note(note, vault_root=vault, imported_at="2026-06-29T00:00:00+00:00")

    assert "\u202e" not in row["sanitized_body"]
    assert "\u200b" not in row["sanitized_body"]


def test_stale_human_note_remains_quarantined(tmp_path: Path):
    vault, _ = sync_fixture(tmp_path)
    note = vault / "inbox" / "old-note.md"
    note.write_text(
        """---
signer: "human:test"
expires_at: "2026-01-01T00:00:00+00:00"
---
old feedback
""",
        encoding="utf-8",
    )

    row = ovw.import_human_note(note, vault_root=vault, imported_at="2026-06-29T00:00:00+00:00")

    assert row["status"] == "quarantined"
    assert "stale_human_feedback" in row["errors"]


def test_stale_after_rejects_even_when_expires_future(tmp_path: Path):
    vault, _ = sync_fixture(tmp_path)
    note = vault / "inbox" / "stale-but-not-expired.md"
    note.write_text(
        """---
signer: "human:test"
stale_after: "2026-01-01T00:00:00+00:00"
expires_at: "2027-01-01T00:00:00+00:00"
---
stale feedback
""",
        encoding="utf-8",
    )

    row = ovw.import_human_note(note, vault_root=vault, imported_at="2026-06-29T00:00:00+00:00")

    assert "stale_human_feedback" in row["errors"]


def test_broken_memory_evidence_is_quarantined_and_manifest_fails(tmp_path: Path):
    memory = {
        "memory_id": "m_broken",
        "text": "avoid chase",
        "evidence_ids": ["missing_id"],
        "evidence": [{"evidence_id": "present_id", "payload_hash": "sha256:x"}],
    }

    vault, manifest = sync_fixture(tmp_path, memory_rows=[memory])

    assert manifest["ok"] is False
    assert "broken_evidence_id:missing_id" in manifest["errors"]
    assert not (vault / "memory" / "m-broken.md").exists()
    assert "m_broken" in (vault / "quarantine" / "memory_export_quarantine.jsonl").read_text(encoding="utf-8")


def test_memory_note_export_requires_evidence_ids(tmp_path: Path):
    memory = {"memory_id": "m_no_evidence", "text": "lesson without objective refs"}

    vault, manifest = sync_fixture(tmp_path, memory_rows=[memory])

    assert manifest["ok"] is False
    assert "missing_evidence_ids" in manifest["errors"]
    assert not (vault / "memory" / "m-no-evidence.md").exists()


def test_orphan_generated_memory_note_is_deleted_with_tombstone(tmp_path: Path):
    memory = {
        "memory_id": "m1",
        "text": "valid memory",
        "evidence_ids": ["e1"],
        "evidence": [{"evidence_id": "e1", "payload_hash": "sha256:x"}],
    }
    vault, _ = sync_fixture(tmp_path, memory_rows=[memory])
    assert (vault / "memory" / "m1.md").exists()

    (tmp_path / "data" / "memory_promoted.jsonl").write_text("", encoding="utf-8")
    manifest = ovw.sync_vault(
        vault_root=vault,
        export_mode="public_redacted",
        library_path=tmp_path / "data" / "setup_skills.json",
        promoted_path=tmp_path / "data" / "memory_promoted.jsonl",
        dont_do_path=tmp_path / "data" / "dont_do_memory.json",
        daily_path=tmp_path / "data" / "daily_exam_latest.json",
        experiments_path=tmp_path / "data" / "experiments.jsonl",
        allow_external_source_paths_for_tests=True,
        generated_at="2026-06-29T00:10:00+00:00",
    )

    assert not (vault / "memory" / "m1.md").exists()
    assert manifest["tombstones"]
    assert "vault.generated_orphan_deleted" in (vault / "quarantine" / "tombstones.jsonl").read_text(encoding="utf-8")


def test_tampered_manifest_does_not_suppress_generated_conflict(tmp_path: Path):
    vault, manifest = sync_fixture(tmp_path)
    skill_path = vault / "skills" / "momentum-continuation-1-0-0.md"
    skill_path.write_text(skill_path.read_text(encoding="utf-8") + "\nmanual drift\n", encoding="utf-8")
    manifest_path = vault / ovw.MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for artifact in payload["artifacts"]:
        if artifact["path"] == "skills/momentum-continuation-1-0-0.md":
            artifact["file_sha256"] = ovw.file_sha256(skill_path)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    result = ovw.sync_vault(
        vault_root=vault,
        export_mode="public_redacted",
        library_path=tmp_path / "data" / "setup_skills.json",
        promoted_path=tmp_path / "data" / "memory_promoted.jsonl",
        dont_do_path=tmp_path / "data" / "dont_do_memory.json",
        daily_path=tmp_path / "data" / "daily_exam_latest.json",
        experiments_path=tmp_path / "data" / "experiments.jsonl",
        allow_external_source_paths_for_tests=True,
        generated_at="2026-06-29T00:20:00+00:00",
    )

    assert "previous_manifest_hash_invalid" in result["errors"]
    assert "vault.generated_conflict" in (vault / "quarantine" / ovw.CONFLICT_HISTORY_NAME).read_text(encoding="utf-8")


def test_dont_do_without_evidence_lineage_is_quarantined(tmp_path: Path):
    dont_do = {"rules": [{"rule_id": "dd1", "condition": "avoid chase", "scope": "global", "severity": "high", "evidence_count": 2}]}

    vault, manifest = sync_fixture(tmp_path, dont_do=dont_do)

    assert manifest["ok"] is False
    assert "missing_evidence_ids" in manifest["errors"]
    assert not (vault / "dont_do" / "dd1.md").exists()


def test_export_sanitizes_markdown_injection_from_skill_text(tmp_path: Path):
    library = one_skill_library(description='[[inject]] ```code``` dataview TABLE FROM "x"')

    vault, manifest = sync_fixture(tmp_path, library=library)
    text = (vault / "skills" / "momentum-continuation-1-0-0.md").read_text(encoding="utf-8")

    assert manifest["ok"] is True
    assert "[[" not in text
    assert "```code```" not in text
    assert 'dataview TABLE FROM "x"' not in text
