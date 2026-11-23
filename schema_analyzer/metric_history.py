"""Quality-metric history for trend lines across analysis runs (PRD §3.12.3).

The analyzer stamps per-run quality signals (`healthScore`, `qualityMetrics`,
`confidence`, fingerprints, provenance). This module distills each run into a
compact **snapshot** and maintains an append-only **history** so consumers can
chart trends (is health improving release-over-release? did a schema change tank
grounding?) without re-reading whole `AnalysisResult` payloads.

Pure and deterministic except for the optional filesystem store. Snapshots draw
timestamps/ids straight from the analysis metadata (no clock of its own), so the
same analysis always yields the same snapshot. Accepts an `AnalysisResult` model
or an already-serialized dict (snake_case or camelCase metadata keys).
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from .utils import meta_get as _meta_get
from .utils import normalize_analysis_dict

HISTORY_SCHEMA_VERSION = 1


def metric_snapshot(analysis: Any) -> dict[str, Any]:
    """Distill one analysis into a compact, JSON-safe metric snapshot."""
    data = normalize_analysis_dict(analysis)
    meta = data.get("metadata")
    meta = meta if isinstance(meta, dict) else {}

    raw_quality = _meta_get(meta, "qualityMetrics", "quality_metrics")
    quality: dict[str, Any] = raw_quality if isinstance(raw_quality, dict) else {}
    raw_structural = quality.get("structural")
    structural: dict[str, Any] = raw_structural if isinstance(raw_structural, dict) else {}
    raw_grounding = quality.get("grounding")
    grounding: dict[str, Any] = raw_grounding if isinstance(raw_grounding, dict) else {}
    raw_gold = quality.get("gold")
    gold: dict[str, Any] = raw_gold if isinstance(raw_gold, dict) else {}

    return {
        "runId": _meta_get(meta, "runId", "run_id"),
        "timestamp": _meta_get(meta, "analysisCompletedAt", "analysis_completed_at", "timestamp"),
        "fingerprint": _meta_get(meta, "physicalSchemaFingerprint", "physical_schema_fingerprint"),
        "confidence": _meta_get(meta, "confidence"),
        "healthScore": _meta_get(meta, "healthScore", "health_score"),
        "usedBaseline": _meta_get(meta, "usedBaseline", "used_baseline"),
        "provider": _meta_get(meta, "provider"),
        "model": _meta_get(meta, "model"),
        "orphanEntityRatio": structural.get("orphanEntityRatio"),
        "danglingRelationshipRatio": structural.get("danglingRelationshipRatio"),
        "mappingGroundingRatio": grounding.get("mappingGroundingRatio"),
        "goldOverlap": gold.get("overlap"),
    }


def append_to_history(
    history: list[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    max_entries: int | None = None,
) -> list[dict[str, Any]]:
    """Return a new history list with ``snapshot`` appended (optionally capped).

    When ``max_entries`` is set, only the most recent ``max_entries`` snapshots
    are retained. Does not mutate the input list.
    """
    out = [*history, snapshot]
    if max_entries is not None and max_entries > 0 and len(out) > max_entries:
        out = out[-max_entries:]
    return out


def _series(history: list[dict[str, Any]], key: str) -> list[float]:
    return [float(s[key]) for s in history if isinstance(s.get(key), (int, float))]


def summarize_history(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize a metric history into trend statistics.

    Reports run count, the first/last timestamp, and for ``healthScore`` /
    ``confidence`` / ``goldOverlap`` the min, max, mean, latest value, and the
    delta (latest − earliest). Empty/absent series report ``None`` rather than
    raising.
    """
    if not history:
        return {"runCount": 0}

    def _stats(key: str) -> dict[str, Any]:
        vals = _series(history, key)
        if not vals:
            return {"min": None, "max": None, "mean": None, "latest": None, "delta": None}
        return {
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
            "mean": round(sum(vals) / len(vals), 4),
            "latest": round(vals[-1], 4),
            "delta": round(vals[-1] - vals[0], 4),
        }

    return {
        "runCount": len(history),
        "firstTimestamp": history[0].get("timestamp"),
        "lastTimestamp": history[-1].get("timestamp"),
        "healthScore": _stats("healthScore"),
        "confidence": _stats("confidence"),
        "goldOverlap": _stats("goldOverlap"),
    }


# --------------------------------------------------------------------------
# Optional filesystem-backed store
# --------------------------------------------------------------------------


def load_history(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load a history file. Missing or corrupt files return an empty history."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text("utf-8"))
    except Exception:
        return []
    if isinstance(raw, dict) and isinstance(raw.get("history"), list):
        return [s for s in raw["history"] if isinstance(s, dict)]
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    return []


def save_history(path: str | os.PathLike[str], history: list[dict[str, Any]]) -> None:
    """Write the history to ``path`` as ``{schemaVersion, history}`` JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schemaVersion": HISTORY_SCHEMA_VERSION, "history": history}
    p.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):  # pragma: no cover — non-POSIX / permission quirk
        os.chmod(p, 0o600)


def record_metrics(
    path: str | os.PathLike[str],
    analysis: Any,
    *,
    max_entries: int | None = None,
) -> dict[str, Any]:
    """Load history at ``path``, append a snapshot of ``analysis``, save, and
    return the snapshot. Convenience for the common track-over-time loop."""
    snapshot = metric_snapshot(analysis)
    history = append_to_history(load_history(path), snapshot, max_entries=max_entries)
    save_history(path, history)
    return snapshot
