"""Annotated chart snapshot renderer for chart intelligence evidence."""
from __future__ import annotations

import hashlib
import os
import struct
import zlib
from pathlib import Path
from typing import Any

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    HAS_MATPLOTLIB = True
except Exception:  # pragma: no cover - exercised when optional dep missing
    HAS_MATPLOTLIB = False

    class _MatplotlibFallback:
        @staticmethod
        def get_backend() -> str:
            return "agg"

    matplotlib = _MatplotlibFallback()

from agent_data_contracts import CHART_MODEL_VERSION, SCHEMA_VERSION, validate_chart_contract
from atomic_state import canonical_json, write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
SNAPSHOT_DIR = ROOT / "state" / "chart" / "snapshots"
ALLOWED_EXTENSIONS = {".png", ".json"}
MAX_IMAGE_BYTES = 5_000_000


def stable_digest(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:24]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def bars_from_batch(candle_batch: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bar in candle_batch.get("bars") or []:
        if not isinstance(bar, dict) or bar.get("is_final") is not True:
            continue
        dt = parse_utc(bar.get("close_time") or bar.get("open_time"))
        if not dt:
            continue
        rows.append(
            {
                "dt": dt,
                "open": safe_float(bar.get("open")),
                "high": safe_float(bar.get("high")),
                "low": safe_float(bar.get("low")),
                "close": safe_float(bar.get("close")),
                "volume": safe_float(bar.get("volume")),
            }
        )
    rows.sort(key=lambda row: row["dt"])
    return rows


def safe_artifact_path(relative_path: str | Path) -> Path:
    candidate = (SNAPSHOT_DIR / relative_path).resolve()
    root = SNAPSHOT_DIR.resolve()
    if candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("unsupported_artifact_extension")
    if root != candidate and root not in candidate.parents:
        raise ValueError("path_traversal_blocked")
    return candidate


def overlay_payload(
    *,
    score: dict[str, Any] | None = None,
    risk_plan: dict[str, Any] | None = None,
    zone_bundle: dict[str, Any] | None = None,
    trendline_bundle: dict[str, Any] | None = None,
    structure_bundle: dict[str, Any] | None = None,
    liquidity_bundle: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    overlays: list[dict[str, Any]] = []
    point_ids: list[str] = []
    if isinstance(score, dict) and score.get("score_id"):
        point_ids.append(str(score["score_id"]))
        overlays.append({"overlay_id": str(score["score_id"]), "type": "score", "label": f"{score.get('tier')} {score.get('score')}"})
    if isinstance(risk_plan, dict) and risk_plan.get("risk_plan_id"):
        point_ids.append(str(risk_plan["risk_plan_id"]))
        overlays.append({"overlay_id": str(risk_plan["risk_plan_id"]), "type": "risk", "sl": risk_plan.get("sl"), "tp_ladder": risk_plan.get("tp_ladder") or []})
    structures = zone_bundle.get("structures") if isinstance(zone_bundle, dict) and isinstance(zone_bundle.get("structures"), dict) else {}
    for zone in structures.get("zones", []) if isinstance(structures.get("zones"), list) else []:
        overlays.append({"overlay_id": zone.get("zone_id"), "type": "zone", "zone_type": zone.get("zone_type"), "lower": zone.get("lower"), "upper": zone.get("upper")})
        if zone.get("zone_id"):
            point_ids.append(str(zone["zone_id"]))
    structures = trendline_bundle.get("structures") if isinstance(trendline_bundle, dict) and isinstance(trendline_bundle.get("structures"), dict) else {}
    for line in structures.get("trendlines", []) if isinstance(structures.get("trendlines"), list) else []:
        overlays.append({"overlay_id": line.get("line_id"), "type": "trendline", "line_type": line.get("line_type"), "slope": line.get("slope"), "intercept": line.get("intercept"), "start_index": line.get("start_index")})
        if line.get("line_id"):
            point_ids.append(str(line["line_id"]))
    structures = structure_bundle.get("structures") if isinstance(structure_bundle, dict) and isinstance(structure_bundle.get("structures"), dict) else {}
    for event in structures.get("structure_events", []) if isinstance(structures.get("structure_events"), list) else []:
        overlays.append({"overlay_id": event.get("reference_pivot_id") or event.get("event_type"), "type": "structure_event", **event})
    liquidity = liquidity_bundle.get("liquidity") if isinstance(liquidity_bundle, dict) and isinstance(liquidity_bundle.get("liquidity"), dict) else {}
    for event in liquidity.get("events", []) if isinstance(liquidity.get("events"), list) else []:
        overlays.append({"overlay_id": event.get("reference_id") or event.get("event_type"), "type": "liquidity_event", **event})
    return overlays, sorted(set(point_ids))


def write_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(pixels[y * width * 3 : (y + 1) * width * 3]) for y in range(height))
    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress(raw, 6))
    payload += chunk(b"IEND", b"")
    path.write_bytes(payload)


def draw_pixel(pixels: bytearray, width: int, height: int, x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= x < width and 0 <= y < height:
        idx = (y * width + x) * 3
        pixels[idx : idx + 3] = bytes(color)


def draw_line(pixels: bytearray, width: int, height: int, x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int]) -> None:
    dx = abs(x2 - x1)
    dy = -abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    err = dx + dy
    while True:
        draw_pixel(pixels, width, height, x1, y1, color)
        if x1 == x2 and y1 == y2:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x1 += sx
        if e2 <= dx:
            err += dx
            y1 += sy


def render_simple_png(path: Path, rows: list[dict[str, Any]], overlays: list[dict[str, Any]], warnings: list[str]) -> None:
    width, height = 1000, 620
    pixels = bytearray([248, 250, 252] * width * height)
    chart_top, chart_bottom = 30, 470
    left, right = 50, width - 30
    prices = [value for row in rows for value in (row["high"], row["low"])]
    if not prices:
        prices = [0.0, 1.0]
    pmin, pmax = min(prices), max(prices)
    if pmax == pmin:
        pmax += 1.0
    def px(idx: int) -> int:
        return left + int((right - left) * idx / max(1, len(rows) - 1))
    def py(price: float) -> int:
        return chart_bottom - int((chart_bottom - chart_top) * (price - pmin) / (pmax - pmin))
    for y in (chart_top, chart_bottom, 520):
        draw_line(pixels, width, height, left, y, right, y, (203, 213, 225))
    for idx, row in enumerate(rows):
        x = px(idx)
        color = (15, 159, 110) if row["close"] >= row["open"] else (220, 38, 38)
        draw_line(pixels, width, height, x, py(row["low"]), x, py(row["high"]), color)
        y_open, y_close = py(row["open"]), py(row["close"])
        for bx in range(x - 2, x + 3):
            draw_line(pixels, width, height, bx, min(y_open, y_close), bx, max(y_open, y_close), color)
        vol_h = int(min(80, row.get("volume", 0) / max(1.0, max(r.get("volume", 0) for r in rows)) * 80))
        draw_line(pixels, width, height, x, 600, x, 600 - vol_h, color)
    for overlay in overlays:
        if overlay.get("type") == "zone":
            lower = safe_float(overlay.get("lower"))
            upper = safe_float(overlay.get("upper"))
            color = (253, 230, 138) if overlay.get("zone_type") == "support" else (254, 202, 202)
            for y in range(max(chart_top, py(upper)), min(chart_bottom, py(lower)) + 1):
                draw_line(pixels, width, height, left, y, right, y, color)
        if overlay.get("type") == "risk" and overlay.get("sl"):
            y = py(safe_float(overlay.get("sl")))
            draw_line(pixels, width, height, left, y, right, y, (220, 38, 38))
    if warnings:
        for x in range(10, 260):
            for y in range(10, 28):
                draw_pixel(pixels, width, height, x, y, (254, 226, 226))
    write_png(path, width, height, pixels)


def render_snapshot(
    candle_batch: dict[str, Any],
    *,
    indicator_bundle: dict[str, Any] | None = None,
    score: dict[str, Any] | None = None,
    risk_plan: dict[str, Any] | None = None,
    zone_bundle: dict[str, Any] | None = None,
    trendline_bundle: dict[str, Any] | None = None,
    structure_bundle: dict[str, Any] | None = None,
    liquidity_bundle: dict[str, Any] | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    rows = bars_from_batch(candle_batch)
    warnings: list[str] = []
    if len(rows) < 2:
        warnings.append("missing_or_insufficient_candles")
    overlays, point_ids = overlay_payload(score=score, risk_plan=risk_plan, zone_bundle=zone_bundle, trendline_bundle=trendline_bundle, structure_bundle=structure_bundle, liquidity_bundle=liquidity_bundle)
    data_hash = stable_digest(
        "chart_snapshot_data",
        {
            "batch_id": candle_batch.get("batch_id"),
            "indicator_id": indicator_bundle.get("indicator_id") if isinstance(indicator_bundle, dict) else None,
            "score_id": score.get("score_id") if isinstance(score, dict) else None,
            "risk_plan_id": risk_plan.get("risk_plan_id") if isinstance(risk_plan, dict) else None,
            "overlays": overlays,
        },
    )
    snapshot_id = stable_digest("chart_snapshot", {"data_hash": data_hash, "symbol": candle_batch.get("symbol"), "timeframe": candle_batch.get("timeframe")})
    target_dir = output_dir or SNAPSHOT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    image_rel = Path(f"{snapshot_id}.png")
    meta_rel = Path(f"{snapshot_id}.json")
    image_path = (target_dir / image_rel).resolve()
    meta_path = (target_dir / meta_rel).resolve()
    if output_dir is None:
        image_path = safe_artifact_path(image_rel)
        meta_path = safe_artifact_path(meta_rel)
    if HAS_MATPLOTLIB:
        fig, (ax, vol_ax) = plt.subplots(2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [4, 1]})
    if HAS_MATPLOTLIB and rows:
        xs = [mdates.date2num(row["dt"]) for row in rows]
        width = (xs[1] - xs[0]) * 0.65 if len(xs) > 1 else 0.0005
        for x, row in zip(xs, rows):
            color = "#0f9f6e" if row["close"] >= row["open"] else "#dc2626"
            ax.plot([x, x], [row["low"], row["high"]], color=color, linewidth=0.8)
            bottom = min(row["open"], row["close"])
            height = max(abs(row["close"] - row["open"]), max(row["high"] - row["low"], 1e-9) * 0.03)
            ax.add_patch(Rectangle((x - width / 2, bottom), width, height, facecolor=color, edgecolor=color, alpha=0.9))
            vol_ax.bar(x, row["volume"], width=width, color=color, alpha=0.55)
        series = indicator_bundle.get("series") if isinstance(indicator_bundle, dict) and isinstance(indicator_bundle.get("series"), dict) else {}
        for name, color in (("ema20", "#f59e0b"), ("ema50", "#2563eb")):
            values = series.get(name) if isinstance(series.get(name), list) else []
            if values and len(values) >= len(xs):
                ax.plot(xs, values[-len(xs) :], color=color, linewidth=1.2, label=name.upper())
        for overlay in overlays:
            if overlay.get("type") == "zone":
                lower = safe_float(overlay.get("lower"))
                upper = safe_float(overlay.get("upper"))
                ax.axhspan(lower, upper, color="#fde68a" if overlay.get("zone_type") == "support" else "#fecaca", alpha=0.25)
            if overlay.get("type") == "risk":
                if overlay.get("sl"):
                    ax.axhline(safe_float(overlay["sl"]), color="#dc2626", linestyle="--", linewidth=1.1)
                for tp in overlay.get("tp_ladder") or []:
                    ax.axhline(safe_float(tp.get("price")), color="#16a34a", linestyle=":", linewidth=1.0)
            if overlay.get("type") == "trendline" and overlay.get("slope") is not None:
                start = int(safe_float(overlay.get("start_index"), 0))
                slope = safe_float(overlay.get("slope"))
                intercept = safe_float(overlay.get("intercept"))
                line_xs = list(range(start, len(xs)))
                if line_xs:
                    ax.plot([xs[i] for i in line_xs], [slope * i + intercept for i in line_xs], color="#7c3aed", linewidth=1.0)
        ax.legend(loc="upper left", fontsize=8)
    if HAS_MATPLOTLIB and warnings:
        ax.text(0.02, 0.95, "; ".join(warnings), transform=ax.transAxes, color="#b91c1c", fontsize=10, va="top")
    if HAS_MATPLOTLIB:
        title = f"{candle_batch.get('symbol')} {candle_batch.get('timeframe')} score={score.get('score') if isinstance(score, dict) else 'n/a'}"
        ax.set_title(title)
        ax.grid(True, alpha=0.25)
        vol_ax.grid(True, alpha=0.2)
        vol_ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(image_path, dpi=110)
        plt.close(fig)
    else:
        render_simple_png(image_path, rows, overlays, warnings)
    if image_path.stat().st_size > MAX_IMAGE_BYTES:
        image_path.unlink(missing_ok=True)
        warnings.append("image_size_limit_exceeded")
    try:
        image_display_path = str(image_path.relative_to(ROOT))
    except ValueError:
        image_display_path = str(image_path)
    try:
        meta_display_path = str(meta_path.relative_to(ROOT))
    except ValueError:
        meta_display_path = str(meta_path)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartSnapshot.v1",
        "snapshot_id": snapshot_id,
        "symbol": str(candle_batch.get("symbol") or "").upper(),
        "timeframe": candle_batch.get("timeframe"),
        "image_path": image_display_path,
        "metadata_path": meta_display_path,
        "data_hash": data_hash,
        "point_ids": point_ids,
        "overlays": overlays,
        "warnings": warnings,
        "source_ids": candle_batch.get("source_ids") or ["chart_snapshot_renderer"],
        "input_event_ids": sorted(set(list(candle_batch.get("input_event_ids") or []) + [str(candle_batch.get("batch_id") or "")])),
        "decision_cutoff": candle_batch.get("decision_cutoff"),
        "cutoff_proof": candle_batch.get("cutoff_proof") or {"ok": False, "errors": ["missing_cutoff_proof"]},
        "degradation_state": "partial" if warnings else candle_batch.get("degradation_state", "ok"),
        "created_at": utc_now(),
        "retention": {"policy": "png_prunable_metadata_preserved", "max_image_bytes": MAX_IMAGE_BYTES},
        "can_place_live_orders": False,
        "live_permission": False,
    }
    validation = validate_chart_contract("ChartSnapshot.v1", metadata)
    if not validation.ok:
        metadata["degradation_state"] = "quarantined"
        metadata["warnings"] = sorted(set(metadata["warnings"] + validation.errors))
    write_json_atomic(meta_path, metadata)
    return metadata


def prune_snapshot_images(max_png_files: int = 100, *, directory: Path | None = None) -> dict[str, Any]:
    directory = directory or SNAPSHOT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    pngs = sorted(directory.glob("*.png"), key=lambda path: path.stat().st_mtime, reverse=True)
    deleted: list[str] = []
    for path in pngs[max(0, int(max_png_files)) :]:
        path.unlink(missing_ok=True)
        deleted.append(path.name)
    return {"deleted": deleted, "metadata_preserved": [path.name for path in directory.glob("*.json")], "can_place_live_orders": False}
