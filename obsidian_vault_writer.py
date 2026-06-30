"""Obsidian-style readable mirror for trading-agent memory and skills.

The vault is documentation only. It never mutates matcher, ranker, risk, or
skill state from Markdown.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, canonical_json, read_json, read_jsonl, write_json_atomic
from data_trust import prepare_llm_egress
from event_store import append_event_envelope, safe_append_event
from setup_skill_library import load_library
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
DEFAULT_VAULT_ROOT = STATE_DIR / "obsidian_vault_public"
MANIFEST_NAME = "artifact_manifest.json"
IMPORT_HISTORY_NAME = "human_feedback_imports.jsonl"
CONFLICT_HISTORY_NAME = "generated_conflicts.jsonl"

GENERATED_BY = "obsidian_vault_writer"
GENERATED_MARKER = "<!-- generated_by: obsidian_vault_writer; mirror_only: true -->"
EXPORT_MODES = {"public_redacted", "private"}
CLOUD_SYNC_PARTS = {
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "icloud",
    "box",
    "mega",
    "syncthing",
}
CONTROL_CHARS = "".join(chr(i) for i in range(32) if chr(i) not in "\n\t")
CONTROL_RE = re.compile("[%s]" % re.escape(CONTROL_CHARS))
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"ASIA[0-9A-Z]{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{16,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[^'\"\s]{8,}"),
    re.compile(r"(?i)BINANCE[_A-Z0-9]*\s*[:=]\s*['\"]?[^'\"\s]{8,}"),
    re.compile(r"SENTINEL[_A-Z0-9-]*SECRET[_A-Z0-9-]*", re.IGNORECASE),
    re.compile(r"(?<!sha256:)\b[A-Za-z0-9_\-]{48,}\b"),
]
SENSITIVE_SOURCE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "wallet.dat",
}
SENSITIVE_SOURCE_SUFFIXES = {".env", ".key", ".pem", ".p12", ".pfx", ".kdbx", ".sqlite", ".db"}
INJECTION_REPLACEMENTS = (
    ("```", "'''"),
    ("[[", "[ ["),
    ("]]", "] ]"),
    ("![", "! ["),
    ("{{", "{ {"),
    ("}}", "} }"),
    ("<%", "< %"),
    ("%>", "% >"),
)


class VaultExportError(ValueError):
    pass


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_payload(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = CONTROL_RE.sub("", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf" or ch in "\n\t")


def redact_secrets(text: str) -> tuple[str, int]:
    redacted = normalize_text(text)
    count = 0
    for pattern in SECRET_PATTERNS:
        redacted, hits = pattern.subn("[REDACTED_SECRET]", redacted)
        count += hits
    return redacted, count


def sanitize_markdown(value: Any, *, quote: bool = False) -> str:
    text, _ = redact_secrets(normalize_text(value))
    for old, new in INJECTION_REPLACEMENTS:
        text = text.replace(old, new)
    lines = text.splitlines() or [""]
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip().lower()
        if stripped in {"---", "..."}:
            line = "- - -"
        if stripped.startswith("dataview") or stripped.startswith("table ") or stripped.startswith("list "):
            line = "`" + line + "`"
        cleaned.append("> " + line if quote else line)
    return "\n".join(cleaned)


def stable_slug(value: Any, fallback: str = "note") -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if not text:
        text = f"{fallback}-{hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:10]}"
    return text[:80].strip("-") or fallback


def add_days(ts: str, days: int) -> str:
    parsed = parse_utc(ts) or datetime.now(timezone.utc)
    return (parsed + timedelta(days=days)).isoformat(timespec="seconds")


def yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text, _ = redact_secrets(normalize_text(value))
    return json.dumps(sanitize_markdown(text), ensure_ascii=False)


def render_frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key in sorted(fields):
        value = fields[key]
        if isinstance(value, list):
            lines.append(f"{key}:")
            if value:
                for item in value:
                    lines.append(f"  - {yaml_scalar(item)}")
            else:
                lines.append("  []")
        elif isinstance(value, dict):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def render_note(frontmatter: dict[str, Any], body: str) -> str:
    text = render_frontmatter(frontmatter) + GENERATED_MARKER + "\n\n" + body.rstrip() + "\n"
    return unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")


def write_utf8_lf(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(normalized)
    tmp.replace(path)
    return sha256_text(normalized)


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

def safe_rel(path: Path, root: Path = ROOT) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.name

def source_seq(path: Path) -> int:
    if not path.exists():
        return 0
    if path.suffix.lower() == ".jsonl":
        return len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
    return 1

def source_meta(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "source_path": safe_rel(path),
            "source_file_sha256": None,
            "source_file_size": 0,
            "source_file_mtime": None,
            "as_of_seq": 0,
            "source_snapshot_hash": digest_payload({"path": safe_rel(path), "missing": True}),
        }
    stat = path.stat()
    sha = file_sha256(path)
    meta = {
        "source_path": safe_rel(path),
        "source_file_sha256": sha,
        "source_file_size": stat.st_size,
        "source_file_mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        "as_of_seq": source_seq(path),
    }
    meta["source_snapshot_hash"] = digest_payload(meta)
    return meta

def validate_source_path(path: Path, *, allow_tmp: bool = False) -> list[str]:
    errors: list[str] = []
    resolved = path.resolve()
    name = resolved.name.lower()
    suffixes = {item.lower() for item in resolved.suffixes}
    parts = {part.lower() for part in resolved.parts}
    if name in SENSITIVE_SOURCE_NAMES or suffixes & SENSITIVE_SOURCE_SUFFIXES:
        errors.append(f"sensitive_source_path:{name}")
    if {"secrets", "wallets", ".ssh"} & parts:
        errors.append("sensitive_source_directory")
    if not allow_tmp:
        try:
            resolved.relative_to(MEMORY_DIR.resolve())
        except Exception:
            errors.append("source_path_outside_agent_memory")
    return sorted(set(errors))

def validate_source_paths(paths: Iterable[Path], *, allow_tmp: bool = False) -> None:
    errors: list[str] = []
    for path in paths:
        errors.extend(validate_source_path(path, allow_tmp=allow_tmp))
    if errors:
        raise VaultExportError(";".join(sorted(set(errors))))

def vault_redact_payload(payload: Any) -> Any:
    return prepare_llm_egress(payload, "obsidian_vault_public_export")["payload"]

def append_vault_event(event_type: str, payload: dict[str, Any], *, event_db_path: Path | None = None, correlation_id: str | None = None) -> dict[str, Any]:
    safe_append_event("obsidian_vault_writer", event_type, payload, ts=str(payload.get("detected_at") or payload.get("imported_at") or payload.get("deleted_at") or utc_now()))
    if event_db_path is None:
        return {"ok": True, "inserted": False, "event_db_path": None}
    try:
        return append_event_envelope(
            event_type,
            payload,
            "obsidian_vault_writer" if event_type.startswith("vault.") else "human_feedback_ledger",
            "obsidian_vault_writer",
            correlation_id or str(payload.get("conflict_id") or payload.get("feedback_id") or payload.get("tombstone_id") or payload.get("quarantine_id") or digest_payload(payload)),
            db_path=event_db_path,
            sequence=str(payload.get("path") or payload.get("feedback_id") or payload.get("source_digest") or digest_payload(payload)),
        )
    except Exception as exc:
        return {"ok": False, "inserted": False, "errors": [str(exc)], "can_place_live_orders": False}

def evidence_key_candidates(row: dict[str, Any], source_hint: str | None = None) -> list[str]:
    source = str(source_hint or row.get("_memory_source_type") or row.get("source_type") or row.get("source") or "")
    keys = []
    for field, prefix in (
        ("evidence_id", source or "evidence"),
        ("review_id", "post_trade_review"),
        ("episode_id", "episode"),
        ("replay_id", "counterfactual"),
        ("exam_id", "daily_exam"),
        ("trade_id", "trade"),
        ("signal_id", "signal"),
        ("memory_id", "promoted_memory"),
        ("candidate_id", "memory_candidate"),
    ):
        value = row.get(field)
        if value:
            keys.append(str(value))
            keys.append(f"{prefix}:{value}")
    return sorted(set(keys))

def collect_evidence_index(paths: Iterable[Path], embedded_rows: Iterable[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in read_jsonl(path):
            if not isinstance(row, dict):
                continue
            source_hint = path.stem.replace("_history", "").replace("post_trade_reviews", "post_trade_review").replace("counterfactual_replays", "counterfactual")
            for key in evidence_key_candidates(row, source_hint):
                index[key] = {"path": safe_rel(path), "payload_hash": digest_payload(row), "row": row}
    for row in embedded_rows or []:
        if not isinstance(row, dict):
            continue
        for evidence in row.get("evidence", []) if isinstance(row.get("evidence"), list) else []:
            if isinstance(evidence, dict):
                for key in evidence_key_candidates(evidence, str(evidence.get("source_type") or "evidence")):
                    index[key] = {"path": "embedded", "payload_hash": digest_payload(evidence), "row": evidence}
    return index

def unresolved_evidence_errors(evidence_ids: Iterable[str], evidence_index: dict[str, dict[str, Any]]) -> list[str]:
    return [f"unresolved_evidence_id:{item}" for item in sorted({str(item) for item in evidence_ids if item and str(item) not in evidence_index})]

def short_hash(value: Any) -> str:
    return digest_payload(value)[:24]

def sanitize_import_errors(errors: Iterable[str]) -> list[str]:
    sanitized: list[str] = []
    for error in errors:
        text = str(error)
        if ":" in text:
            prefix, value = text.split(":", 1)
            sanitized.append(f"{prefix}_hash:{short_hash(value)}")
        else:
            sanitized.append(text)
    return sorted(set(sanitized))


def ensure_inside(root: Path, path: Path) -> Path:
    root_resolved = root.resolve()
    target = path.resolve()
    if root_resolved != target and root_resolved not in target.parents:
        raise VaultExportError(f"path_outside_vault:{target}")
    return target


def has_git_ancestor(path: Path) -> bool:
    current = path.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return True
    return False


def path_has_cloud_sync(path: Path) -> bool:
    parts = {part.lower() for part in path.resolve().parts}
    return bool(parts & CLOUD_SYNC_PARTS) or any(any(marker in part for marker in CLOUD_SYNC_PARTS) for part in parts)


def validate_vault_path(vault_root: Path, export_mode: str = "public_redacted") -> list[str]:
    if export_mode not in EXPORT_MODES:
        return [f"unsupported_export_mode:{export_mode}"]
    errors: list[str] = []
    if export_mode == "private":
        if has_git_ancestor(vault_root):
            errors.append("private_vault_inside_git_tree")
        if path_has_cloud_sync(vault_root):
            errors.append("private_vault_inside_cloud_sync_path")
        errors.append("private_vault_encryption_not_configured")
    return errors


def base_frontmatter(
    *,
    artifact_type: str,
    source_payload: Any,
    source_ids: Iterable[str],
    generated_at: str,
    export_mode: str,
    as_of_seq: int | None = None,
    evidence_ids: Iterable[str] | None = None,
    source_snapshot_hash: str | None = None,
    source_file_sha256: str | None = None,
    source_path: str | None = None,
    stale_after: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    source_digest = digest_payload(source_payload)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated": True,
        "generated_by": GENERATED_BY,
        "generated_at": generated_at,
        "artifact_type": artifact_type,
        "source_digest": source_digest,
        "source_snapshot_hash": source_snapshot_hash or source_digest,
        "source_file_sha256": source_file_sha256,
        "source_path": source_path,
        "source_ids": sorted({str(item) for item in source_ids if item}),
        "evidence_ids": sorted({str(item) for item in (evidence_ids or []) if item}),
        "as_of_seq": as_of_seq if as_of_seq is not None else 0,
        "classification": "public_redacted" if export_mode == "public_redacted" else "private",
        "stale_after": stale_after or add_days(generated_at, 1),
        "expires_at": expires_at or add_days(generated_at, 30),
        "mirror_only": True,
        "can_mutate_runtime": False,
        "can_place_live_orders": False,
    }


def collect_skill_evidence(skill: dict[str, Any]) -> list[str]:
    stats = skill.get("stats") if isinstance(skill.get("stats"), dict) else {}
    recent = stats.get("recent") if isinstance(stats.get("recent"), list) else []
    ids = [str(row.get("evidence_id")) for row in recent if isinstance(row, dict) and row.get("evidence_id")]
    metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
    for patch in metadata.get("paper_shadow_patches", []) if isinstance(metadata.get("paper_shadow_patches"), list) else []:
        if isinstance(patch, dict):
            ids.extend(str(item) for item in patch.get("evidence_ids", []) if item)
    return sorted(set(ids))


def render_skill_markdown(skill: dict[str, Any], generated_at: str, export_mode: str, source: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    setup_id = str(skill.get("setup_id") or "unknown")
    evidence_ids = collect_skill_evidence(skill)
    source = source or {}
    frontmatter = base_frontmatter(
        artifact_type="setup_skill_contract",
        source_payload=skill,
        source_ids=[setup_id, str(skill.get("setup_contract_id") or "")],
        evidence_ids=evidence_ids,
        generated_at=generated_at,
        export_mode=export_mode,
        as_of_seq=source.get("as_of_seq"),
        source_snapshot_hash=source.get("source_snapshot_hash"),
        source_file_sha256=source.get("source_file_sha256"),
        source_path=source.get("source_path"),
    )
    frontmatter.update(
        {
            "setup_id": setup_id,
            "setup_version": skill.get("setup_version") or skill.get("version"),
            "setup_contract_hash": skill.get("setup_contract_hash"),
            "matcher_version": skill.get("matcher_version"),
            "ranker_version": skill.get("ranker_version"),
            "risk_version": skill.get("risk_version"),
        }
    )
    stats = skill.get("stats") if isinstance(skill.get("stats"), dict) else {}
    body = "\n".join(
        [
            f"# Skill: {sanitize_markdown(skill.get('name') or setup_id)}",
            "",
            "Vault này chỉ là mirror đọc được. Runtime dùng `setup_skills.json`, không dùng Markdown này để đặt lệnh.",
            "",
            "## Hợp đồng",
            f"- `setup_id`: `{sanitize_markdown(setup_id)}`",
            f"- `setup_contract_hash`: `{sanitize_markdown(skill.get('setup_contract_hash') or '')}`",
            f"- `matcher_version`: `{sanitize_markdown(skill.get('matcher_version') or '')}`",
            f"- `ranker_version`: `{sanitize_markdown(skill.get('ranker_version') or '')}`",
            f"- `risk_version`: `{sanitize_markdown(skill.get('risk_version') or '')}`",
            "",
            "## Mô tả",
            sanitize_markdown(skill.get("description") or ""),
            "",
            "## Điều kiện dùng",
            "\n".join(f"- {sanitize_markdown(item)}" for item in skill.get("prerequisites", []) if item) or "- none",
            "",
            "## Điều kiện cấm",
            "\n".join(f"- {sanitize_markdown(item)}" for item in skill.get("invalidations", []) if item) or "- none",
            "",
            "## Entry / SL / TP",
            f"- Entry: {sanitize_markdown(skill.get('entry_pattern') or '')}",
            f"- SL: {sanitize_markdown(skill.get('stop_template') or '')}",
            f"- TP: {sanitize_markdown(skill.get('target_template') or '')}",
            "",
            "## Stats",
            f"- Trades: `{int(stats.get('trades') or 0)}`",
            f"- Win rate: `{float(stats.get('win_rate') or 0.0):.4f}`",
            f"- Expectancy: `{float(stats.get('expectancy') or 0.0):.8f}`",
            f"- Net: `{float(stats.get('net') or 0.0):+.8f}`",
            "",
            "## Evidence IDs",
            "\n".join(f"- `{sanitize_markdown(item)}`" for item in evidence_ids) or "- none",
        ]
    )
    return render_note(frontmatter, body), frontmatter


def memory_expiry(memory: dict[str, Any], generated_at: str) -> tuple[str, str]:
    promoted = str(memory.get("promoted_at") or memory.get("memory_promoted_at") or generated_at)
    ttl_days = int(memory.get("ttl_days") or 30)
    return add_days(promoted, min(1, ttl_days)), add_days(promoted, ttl_days)

def render_memory_markdown(memory: dict[str, Any], generated_at: str, export_mode: str, source: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    memory_id = str(memory.get("memory_id") or memory.get("candidate_id") or "memory")
    evidence_ids = [str(item) for item in memory.get("evidence_ids", []) if item] if isinstance(memory.get("evidence_ids"), list) else []
    source = source or {}
    stale_after, expires_at = memory_expiry(memory, generated_at)
    frontmatter = base_frontmatter(
        artifact_type="promoted_memory",
        source_payload=memory,
        source_ids=[memory_id],
        evidence_ids=evidence_ids,
        generated_at=generated_at,
        export_mode=export_mode,
        as_of_seq=source.get("as_of_seq"),
        source_snapshot_hash=source.get("source_snapshot_hash"),
        source_file_sha256=source.get("source_file_sha256"),
        source_path=source.get("source_path"),
        stale_after=stale_after,
        expires_at=expires_at,
    )
    frontmatter.update(
        {
            "memory_id": memory_id,
            "memory_kind": memory.get("kind"),
            "confidence_score": memory.get("confidence_score"),
            "promoted_at": memory.get("promoted_at") or memory.get("memory_promoted_at"),
        }
    )
    body = "\n".join(
        [
            f"# Memory: {sanitize_markdown(memory_id)}",
            "",
            "## Lesson",
            sanitize_markdown(memory.get("text") or memory.get("claim") or ""),
            "",
            "## Evidence IDs",
            "\n".join(f"- `{sanitize_markdown(item)}`" for item in evidence_ids) or "- none",
            "",
            "## Metrics",
            f"- Recall count: `{int(memory.get('recall_count') or 0)}`",
            f"- Unique contexts: `{int(memory.get('unique_contexts') or 0)}`",
            f"- Trade samples: `{int(memory.get('trade_samples') or 0)}`",
            f"- Contradictions: `{int(memory.get('contradiction_count') or 0)}`",
        ]
    )
    return render_note(frontmatter, body), frontmatter


def render_dont_do_markdown(rule: dict[str, Any], generated_at: str, export_mode: str, source: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    rule_id = str(rule.get("rule_id") or digest_payload(rule)[7:23])
    evidence_ids = [str(item) for item in rule.get("evidence_ids", []) if item] if isinstance(rule.get("evidence_ids"), list) else []
    source = source or {}
    frontmatter = base_frontmatter(
        artifact_type="dont_do_rule",
        source_payload=rule,
        source_ids=[rule_id],
        evidence_ids=evidence_ids,
        generated_at=generated_at,
        export_mode=export_mode,
        as_of_seq=source.get("as_of_seq"),
        source_snapshot_hash=source.get("source_snapshot_hash"),
        source_file_sha256=source.get("source_file_sha256"),
        source_path=source.get("source_path"),
        expires_at=rule.get("expires_at") or add_days(generated_at, 30),
    )
    frontmatter.update({"rule_id": rule_id, "severity": rule.get("severity"), "scope": rule.get("scope")})
    body = "\n".join(
        [
            f"# DONT_DO: {sanitize_markdown(rule_id)}",
            "",
            "## Điều cấm",
            sanitize_markdown(rule.get("condition") or ""),
            "",
            "## Bằng chứng",
            f"- Evidence count: `{int(rule.get('evidence_count') or 0)}`",
            f"- Counter evidence: `{int(rule.get('counter_evidence_count') or 0)}`",
            "\n".join(f"- `{sanitize_markdown(item)}`" for item in evidence_ids) or "- Evidence IDs: none",
        ]
    )
    return render_note(frontmatter, body), frontmatter


def render_daily_markdown(payload: dict[str, Any], generated_at: str, export_mode: str, source: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    day = (parse_utc(str(payload.get("updated_at") or payload.get("ts") or generated_at)) or datetime.now(timezone.utc)).date().isoformat()
    source = source or {}
    frontmatter = base_frontmatter(
        artifact_type="daily_report",
        source_payload=payload,
        source_ids=[f"daily:{day}"],
        generated_at=generated_at,
        export_mode=export_mode,
        as_of_seq=source.get("as_of_seq"),
        source_snapshot_hash=source.get("source_snapshot_hash"),
        source_file_sha256=source.get("source_file_sha256"),
        source_path=source.get("source_path"),
    )
    frontmatter["report_day"] = day
    body = "\n".join(
        [
            f"# Báo cáo ngày {day}",
            "",
            "## Tóm tắt",
            sanitize_markdown(payload.get("summary") or payload.get("status") or "Không có summary."),
            "",
            "## Snapshot",
            sanitize_markdown(json.dumps(vault_redact_payload(payload), ensure_ascii=False, indent=2, sort_keys=True), quote=True),
        ]
    )
    return render_note(frontmatter, body), frontmatter


def redact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        clean = {}
        for key, value in payload.items():
            key_l = str(key).lower()
            if any(token in key_l for token in ("secret", "token", "api_key", "apikey", "password")):
                clean[key] = "[REDACTED_SECRET]"
            else:
                clean[key] = redact_payload(value)
        return clean
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, str):
        return redact_secrets(payload)[0]
    return payload


def evidence_errors(row: dict[str, Any]) -> list[str]:
    evidence_ids = [str(item) for item in row.get("evidence_ids", []) if item] if isinstance(row.get("evidence_ids"), list) else []
    if not evidence_ids:
        return ["missing_evidence_ids"]
    records = row.get("evidence") if isinstance(row.get("evidence"), list) else []
    record_ids = {str(item.get("evidence_id")) for item in records if isinstance(item, dict) and item.get("evidence_id")}
    if records and not set(evidence_ids).issubset(record_ids):
        return [f"broken_evidence_id:{item}" for item in sorted(set(evidence_ids) - record_ids)]
    return []


def previous_manifest(vault_root: Path) -> dict[str, Any]:
    manifest = read_json(vault_root / MANIFEST_NAME, default={})
    if not isinstance(manifest, dict) or not manifest:
        return {}
    recorded = manifest.get("artifact_manifest_hash")
    calculated = digest_payload({k: v for k, v in manifest.items() if k != "artifact_manifest_hash"})
    if recorded != calculated:
        return {"manifest_invalid": True, "recorded_hash": recorded, "calculated_hash": calculated}
    return manifest


def previous_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    if manifest.get("manifest_invalid"):
        return {}
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else []
    return {str(row.get("path")): str(row.get("file_sha256")) for row in artifacts if row.get("path") and row.get("file_sha256")}


def quarantine_generated_conflict(vault_root: Path, rel_path: str, current_text: str, expected_hash: str | None, current_hash: str, generated_at: str, event_db_path: Path | None = None) -> dict[str, Any]:
    row = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "vault.generated_conflict",
        "conflict_id": "vaultconf_" + digest_payload({"path": rel_path, "current_sha256": current_hash})[7:27],
        "detected_at": generated_at,
        "path": rel_path,
        "expected_sha256": expected_hash,
        "current_sha256": current_hash,
        "status": "quarantined",
        "can_mutate_runtime": False,
    }
    conflict_path = vault_root / "quarantine" / CONFLICT_HISTORY_NAME
    append_jsonl(conflict_path, {**row, "body": sanitize_markdown(current_text, quote=True)})
    row["event_result"] = append_vault_event("vault.generated_conflict", row, event_db_path=event_db_path)
    return row


def write_generated_note(
    vault_root: Path,
    rel_path: str,
    text: str,
    frontmatter: dict[str, Any],
    manifest_hashes: dict[str, str],
    generated_at: str,
    event_db_path: Path | None = None,
) -> dict[str, Any]:
    target = ensure_inside(vault_root, vault_root / rel_path)
    conflict = None
    if target.exists() and rel_path in manifest_hashes:
        current_hash = file_sha256(target)
        if current_hash != manifest_hashes[rel_path]:
            current_text = target.read_text(encoding="utf-8", errors="ignore")
            conflict = quarantine_generated_conflict(vault_root, rel_path, current_text, manifest_hashes[rel_path], current_hash, generated_at, event_db_path=event_db_path)
    elif target.exists():
        current_text = target.read_text(encoding="utf-8", errors="ignore")
        if GENERATED_MARKER in current_text:
            conflict = quarantine_generated_conflict(vault_root, rel_path, current_text, None, file_sha256(target), generated_at, event_db_path=event_db_path)
    file_hash = write_utf8_lf(target, text)
    return {
        "path": rel_path.replace("\\", "/"),
        "file_sha256": file_hash,
        "source_digest": frontmatter.get("source_digest"),
        "artifact_type": frontmatter.get("artifact_type"),
        "source_ids": frontmatter.get("source_ids") or [],
        "evidence_ids": frontmatter.get("evidence_ids") or [],
        "conflict": conflict,
    }


def cleanup_orphan_generated(vault_root: Path, expected_paths: set[str], manifest_hashes: dict[str, str], generated_at: str, event_db_path: Path | None = None) -> list[dict[str, Any]]:
    deleted: list[dict[str, Any]] = []
    for rel_path, old_hash in sorted(manifest_hashes.items()):
        rel_norm = str(rel_path).replace("\\", "/")
        if rel_norm.startswith("../") or "/../" in rel_norm or Path(rel_norm).is_absolute():
            continue
        if rel_path in expected_paths:
            continue
        path = ensure_inside(vault_root, vault_root / rel_norm)
        if not path.exists() or path.suffix.lower() != ".md":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if GENERATED_MARKER not in text:
            continue
        tombstone = {
            "schema_version": SCHEMA_VERSION,
            "event_type": "vault.generated_orphan_deleted",
            "tombstone_id": "vaultts_" + digest_payload({"path": rel_norm, "old_sha256": old_hash})[7:27],
            "deleted_at": generated_at,
            "path": rel_norm,
            "old_sha256": old_hash,
        }
        append_jsonl(vault_root / "quarantine" / "tombstones.jsonl", tombstone)
        tombstone["event_result"] = append_vault_event("vault.generated_orphan_deleted", tombstone, event_db_path=event_db_path)
        path.unlink()
        deleted.append(tombstone)
    return deleted


def assert_bundle_secret_scan(vault_root: Path, artifacts: list[dict[str, Any]] | None = None) -> list[str]:
    errors: list[str] = []
    candidate_paths = []
    if artifacts is not None:
        candidate_paths.extend(vault_root / str(artifact.get("path") or "") for artifact in artifacts)
    candidate_paths.extend(path for path in vault_root.rglob("*") if path.is_file() and "inbox" not in path.relative_to(vault_root).parts)
    seen: set[Path] = set()
    for path in candidate_paths:
        try:
            path = ensure_inside(vault_root, path)
        except Exception:
            continue
        if path in seen or not path.exists():
            continue
        seen.add(path)
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"secret_scan_failed:{path.relative_to(vault_root).as_posix()}")
                break
    return errors


def quarantine_export(vault_root: Path, row: dict[str, Any], event_db_path: Path | None = None, filename: str = "export_quarantine.jsonl") -> dict[str, Any]:
    row = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "vault.memory_quarantined",
        "quarantine_id": row.get("quarantine_id") or "vaultq_" + digest_payload(row)[7:27],
        "reason": row.get("reason") or ";".join(row.get("errors", [])),
        "can_mutate_runtime": False,
        "can_place_live_orders": False,
        **row,
    }
    append_jsonl(vault_root / "quarantine" / filename, row)
    row["event_result"] = append_vault_event("vault.memory_quarantined", row, event_db_path=event_db_path)
    return row


def export_skill_notes(
    vault_root: Path,
    library_path: Path,
    generated_at: str,
    export_mode: str,
    manifest_hashes: dict[str, str],
    evidence_index: dict[str, dict[str, Any]],
    event_db_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    library = load_library(library_path)
    source = source_meta(library_path)
    artifacts: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for setup_id, skill in sorted((library.get("skills") or {}).items()):
        if not isinstance(skill, dict):
            continue
        evidence_ids = collect_skill_evidence(skill)
        errors = unresolved_evidence_errors(evidence_ids, evidence_index)
        if errors:
            q = quarantine_export(vault_root, {"quarantined_at": generated_at, "source_type": "setup_skill", "source_id": str(setup_id), "errors": errors, "source_digest": digest_payload(skill)}, event_db_path, "skill_export_quarantine.jsonl")
            quarantined.append(q)
            continue
        note, fm = render_skill_markdown(skill, generated_at, export_mode, source)
        version = stable_slug(skill.get("setup_version") or skill.get("version") or "v1", "v1")
        rel_path = f"skills/{stable_slug(setup_id, 'skill')}-{version}.md"
        artifacts.append(write_generated_note(vault_root, rel_path, note, fm, manifest_hashes, generated_at, event_db_path=event_db_path))
    return artifacts, quarantined


def export_memory_notes(
    vault_root: Path,
    promoted_path: Path,
    generated_at: str,
    export_mode: str,
    manifest_hashes: dict[str, str],
    evidence_index: dict[str, dict[str, Any]],
    event_db_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    artifacts: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    source = source_meta(promoted_path)
    for row in read_jsonl(promoted_path):
        if not isinstance(row, dict):
            continue
        ids = [str(item) for item in row.get("evidence_ids", []) if item] if isinstance(row.get("evidence_ids"), list) else []
        errors = evidence_errors(row) + unresolved_evidence_errors(ids, evidence_index)
        if errors:
            q = quarantine_export(vault_root, {"quarantined_at": generated_at, "source_type": "promoted_memory", "memory_id": row.get("memory_id"), "errors": errors, "source_digest": digest_payload(row)}, event_db_path, "memory_export_quarantine.jsonl")
            quarantined.append(q)
            continue
        note, fm = render_memory_markdown(row, generated_at, export_mode, source)
        rel_path = f"memory/{stable_slug(row.get('memory_id') or row.get('candidate_id'), 'memory')}.md"
        artifacts.append(write_generated_note(vault_root, rel_path, note, fm, manifest_hashes, generated_at, event_db_path=event_db_path))
    return artifacts, quarantined


def export_dont_do_notes(
    vault_root: Path,
    dont_do_path: Path,
    generated_at: str,
    export_mode: str,
    manifest_hashes: dict[str, str],
    evidence_index: dict[str, dict[str, Any]],
    event_db_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = read_json(dont_do_path, default={"rules": []})
    artifacts: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    source = source_meta(dont_do_path)
    for rule in payload.get("rules", []) if isinstance(payload.get("rules"), list) else []:
        if not isinstance(rule, dict):
            continue
        evidence_ids = [str(item) for item in rule.get("evidence_ids", []) if item] if isinstance(rule.get("evidence_ids"), list) else []
        errors = ["missing_evidence_ids"] if not evidence_ids else []
        errors.extend(unresolved_evidence_errors(evidence_ids, evidence_index))
        if errors:
            q = quarantine_export(vault_root, {"quarantined_at": generated_at, "source_type": "dont_do_rule", "rule_id": rule.get("rule_id"), "errors": errors, "source_digest": digest_payload(rule)}, event_db_path, "dont_do_export_quarantine.jsonl")
            quarantined.append(q)
            continue
        note, fm = render_dont_do_markdown(rule, generated_at, export_mode, source)
        rel_path = f"dont_do/{stable_slug(rule.get('rule_id'), 'dont-do')}.md"
        artifacts.append(write_generated_note(vault_root, rel_path, note, fm, manifest_hashes, generated_at, event_db_path=event_db_path))
    return artifacts, quarantined


def export_daily_note(vault_root: Path, daily_path: Path, generated_at: str, export_mode: str, manifest_hashes: dict[str, str], event_db_path: Path | None = None) -> list[dict[str, Any]]:
    payload = read_json(daily_path, default={})
    if not isinstance(payload, dict) or not payload:
        return []
    note, fm = render_daily_markdown(payload, generated_at, export_mode, source_meta(daily_path))
    rel_path = f"daily/{fm['report_day']}.md"
    return [write_generated_note(vault_root, rel_path, note, fm, manifest_hashes, generated_at, event_db_path=event_db_path)]


def export_experiment_note(vault_root: Path, experiments_path: Path, generated_at: str, export_mode: str, manifest_hashes: dict[str, str], event_db_path: Path | None = None) -> list[dict[str, Any]]:
    rows = read_jsonl(experiments_path, limit=100)
    if not rows:
        return []
    source = source_meta(experiments_path)
    public_payload = {"rows": vault_redact_payload(rows[-100:]), "count": len(rows[-100:])}
    raw_payload = {"rows": rows[-100:], "count": len(rows[-100:])}
    fm = base_frontmatter(
        artifact_type="experiment_rollup",
        source_payload=raw_payload,
        source_ids=["experiments:latest"],
        generated_at=generated_at,
        export_mode=export_mode,
        as_of_seq=source.get("as_of_seq"),
        source_snapshot_hash=source.get("source_snapshot_hash"),
        source_file_sha256=source.get("source_file_sha256"),
        source_path=source.get("source_path"),
    )
    fm["redacted_export_hash"] = digest_payload(public_payload)
    body = "\n".join(["# Experiments", "", f"- Rows: `{len(rows[-100:])}`", "", "## Latest", sanitize_markdown(json.dumps(public_payload, ensure_ascii=False, indent=2, sort_keys=True), quote=True)])
    return [write_generated_note(vault_root, "experiments/latest.md", render_note(fm, body), fm, manifest_hashes, generated_at, event_db_path=event_db_path)]


def sync_vault(
    *,
    vault_root: Path = DEFAULT_VAULT_ROOT,
    export_mode: str = "public_redacted",
    library_path: Path = MEMORY_DIR / "setup_skills.json",
    promoted_path: Path = MEMORY_DIR / "memory_promoted.jsonl",
    dont_do_path: Path = MEMORY_DIR / "dont_do_memory.json",
    daily_path: Path = MEMORY_DIR / "daily_exam_latest.json",
    experiments_path: Path = MEMORY_DIR / "experiments.jsonl",
    evidence_paths: list[Path] | None = None,
    event_db_path: Path | None = None,
    allow_external_source_paths_for_tests: bool = False,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or utc_now()
    vault_root = vault_root.resolve()
    path_errors = validate_vault_path(vault_root, export_mode=export_mode)
    if path_errors:
        raise VaultExportError(";".join(path_errors))
    source_paths = [library_path, promoted_path, dont_do_path, daily_path, experiments_path]
    if evidence_paths is None:
        parent = promoted_path.parent
        evidence_paths = [
            promoted_path,
            parent / "post_trade_reviews.jsonl",
            parent / "counterfactual_replays.jsonl",
            parent / "counterfactual_replay_history.jsonl",
            parent / "episodes.jsonl",
            parent / "daily_exam_history.jsonl",
        ]
    validate_source_paths([*source_paths, *evidence_paths], allow_tmp=allow_external_source_paths_for_tests)
    for folder in ("daily", "trades", "skills", "dont_do", "experiments", "source_notes", "memory", "inbox", "quarantine"):
        (vault_root / folder).mkdir(parents=True, exist_ok=True)

    old_manifest = previous_manifest(vault_root)
    old_hashes = previous_hashes(old_manifest)
    artifacts: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    manifest_trust_errors = ["previous_manifest_hash_invalid"] if old_manifest.get("manifest_invalid") else []
    promoted_rows = read_jsonl(promoted_path)
    evidence_index = collect_evidence_index(evidence_paths, embedded_rows=promoted_rows)

    skill_artifacts, skill_quarantine = export_skill_notes(vault_root, library_path, generated_at, export_mode, old_hashes, evidence_index, event_db_path=event_db_path)
    artifacts.extend(skill_artifacts)
    quarantined.extend(skill_quarantine)
    memory_artifacts, memory_quarantine = export_memory_notes(vault_root, promoted_path, generated_at, export_mode, old_hashes, evidence_index, event_db_path=event_db_path)
    artifacts.extend(memory_artifacts)
    quarantined.extend(memory_quarantine)
    dont_do_artifacts, dont_do_quarantine = export_dont_do_notes(vault_root, dont_do_path, generated_at, export_mode, old_hashes, evidence_index, event_db_path=event_db_path)
    artifacts.extend(dont_do_artifacts)
    quarantined.extend(dont_do_quarantine)
    artifacts.extend(export_daily_note(vault_root, daily_path, generated_at, export_mode, old_hashes, event_db_path=event_db_path))
    artifacts.extend(export_experiment_note(vault_root, experiments_path, generated_at, export_mode, old_hashes, event_db_path=event_db_path))

    expected_paths = {str(row["path"]) for row in artifacts}
    tombstones = cleanup_orphan_generated(vault_root, expected_paths, old_hashes, generated_at, event_db_path=event_db_path)
    errors = [item for row in quarantined for item in row.get("errors", [])] + manifest_trust_errors
    errors.extend(assert_bundle_secret_scan(vault_root, artifacts))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "generated_by": GENERATED_BY,
        "export_mode": export_mode,
        "vault_root": safe_rel(vault_root) if export_mode == "public_redacted" else str(vault_root),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "quarantine_count": len(quarantined),
        "quarantined": quarantined,
        "tombstones": tombstones,
        "ok": not errors,
        "errors": sorted(set(errors)),
        "can_mutate_runtime": False,
        "can_place_live_orders": False,
    }
    manifest["artifact_manifest_hash"] = digest_payload({k: v for k, v in manifest.items() if k != "artifact_manifest_hash"})
    write_json_atomic(vault_root / MANIFEST_NAME, manifest)
    post_write_errors = assert_bundle_secret_scan(vault_root)
    if post_write_errors:
        manifest["errors"] = sorted(set(manifest["errors"] + post_write_errors))
        manifest["ok"] = False
        manifest["artifact_manifest_hash"] = digest_payload({k: v for k, v in manifest.items() if k != "artifact_manifest_hash"})
        write_json_atomic(vault_root / MANIFEST_NAME, manifest)
    return manifest


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end = normalized.find("\n---\n", 4)
    if end < 0:
        return {}, normalized
    raw = normalized[4:end]
    body = normalized[end + 5 :]
    fields: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            fields.setdefault(current_key, []).append(line[4:].strip().strip('"'))
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            current_key = key.strip()
            value = value.strip()
            if value == "":
                fields[current_key] = []
            elif value in {"true", "false"}:
                fields[current_key] = value == "true"
            elif value == "null":
                fields[current_key] = None
            else:
                try:
                    fields[current_key] = json.loads(value)
                except Exception:
                    fields[current_key] = value.strip('"')
    return fields, body


def import_human_note(
    note_path: Path,
    *,
    vault_root: Path,
    allowed_evidence_ids: Iterable[str] | None = None,
    event_db_path: Path | None = None,
    imported_at: str | None = None,
) -> dict[str, Any]:
    imported_at = imported_at or utc_now()
    vault_root = vault_root.resolve()
    note_path = ensure_inside(vault_root, note_path)
    inbox_root = (vault_root / "inbox").resolve()
    if inbox_root != note_path.resolve().parent and inbox_root not in note_path.resolve().parents:
        raise VaultExportError("human_import_outside_inbox")
    text = note_path.read_text(encoding="utf-8", errors="ignore")
    frontmatter, body = parse_frontmatter(text)
    evidence_refs = frontmatter.get("evidence_refs") or frontmatter.get("evidence_ids") or []
    if isinstance(evidence_refs, str):
        evidence_refs = [evidence_refs]
    evidence_refs = [str(item) for item in evidence_refs if item]
    allowed = {str(item) for item in (allowed_evidence_ids or []) if item}
    errors: list[str] = []
    if GENERATED_MARKER in text:
        errors.append("generated_note_cannot_be_imported")
    if allowed and not set(evidence_refs).issubset(allowed):
        errors.extend(f"invalid_evidence_ref:{item}" for item in sorted(set(evidence_refs) - allowed))
    if evidence_refs and allowed_evidence_ids is None:
        errors.append("unverified_evidence_refs")
    expiry = frontmatter.get("expires_at")
    stale_after = frontmatter.get("stale_after")
    expiry_dt = parse_utc(str(expiry)) if expiry else None
    stale_dt = parse_utc(str(stale_after)) if stale_after else None
    now_dt = parse_utc(imported_at)
    if stale_dt and now_dt and stale_dt <= now_dt:
        errors.append("stale_human_feedback")
    if expiry_dt and now_dt and expiry_dt <= now_dt:
        errors.append("stale_human_feedback")
    note_hash = file_sha256(note_path)
    feedback_id = "feedback_" + digest_payload({"note_hash": note_hash, "evidence_refs": evidence_refs})[7:27]
    evidence_ref_hashes = [short_hash(item) for item in evidence_refs]
    sanitized_errors = sanitize_import_errors(errors)
    row = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "human_feedback.imported",
        "feedback_id": feedback_id,
        "source_id": "obsidian_vault_inbox",
        "imported_at": imported_at,
        "signer_hash": short_hash(frontmatter.get("signer") or "unknown"),
        "scope_hash": short_hash(frontmatter.get("scope") or "operator_note"),
        "note_path": safe_rel(note_path, vault_root),
        "note_hash": note_hash,
        "taint_class": "operator_feedback",
        "approval_id_hash": short_hash(frontmatter.get("approval_id")) if frontmatter.get("approval_id") else None,
        "expiry_hash": short_hash(expiry) if expiry else None,
        "stale_after_hash": short_hash(stale_after) if stale_after else None,
        "evidence_ref_hashes": evidence_ref_hashes,
        "evidence_ref_count": len(evidence_ref_hashes),
        "status": "quarantined",
        "errors": sanitized_errors,
        "sanitized_frontmatter": {"frontmatter_hash": digest_payload(redact_payload(frontmatter)), "keys": sorted(str(key) for key in frontmatter)},
        "sanitized_body": sanitize_markdown(body, quote=True),
        "can_mutate_runtime": False,
        "can_place_live_orders": False,
    }
    append_jsonl(vault_root / "quarantine" / IMPORT_HISTORY_NAME, row)
    row["event_result"] = append_vault_event(
        "human_feedback.imported",
        {
            "feedback_id": feedback_id,
            "source_id": "obsidian_vault_inbox",
            "note_hash": note_hash,
            "status": "quarantined",
            "taint_class": "operator_feedback",
            "evidence_ref_hashes": evidence_ref_hashes,
            "evidence_ref_count": len(evidence_ref_hashes),
            "errors": row["errors"],
        },
        event_db_path=event_db_path,
        correlation_id=feedback_id,
    )
    return row


def parse_args(argv: Iterable[str] | None = None) -> Any:
    import argparse

    parser = argparse.ArgumentParser(description="Export Obsidian mirror vault")
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--export-mode", choices=sorted(EXPORT_MODES), default="public_redacted")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--import-note")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.import_note:
        result = import_human_note(Path(args.import_note), vault_root=Path(args.vault_root))
    else:
        result = sync_vault(vault_root=Path(args.vault_root), export_mode=args.export_mode)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
