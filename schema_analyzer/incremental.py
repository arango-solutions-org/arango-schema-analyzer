"""Incremental re-analysis and change-state detection (PRD §3.13.3).

A full ``analyze_physical_schema`` snapshots every collection, detects type
discriminators, samples documents, and (optionally) calls an LLM. For large or
frequently-polled databases that is wasteful when nothing — or only row counts —
has changed. This module implements the PRD §3.13.3 change-state contract on top
of the cheap ``fingerprint_physical_shape`` / ``fingerprint_physical_counts``
probes so a consumer can decide whether a refresh is even warranted:

* ``unchanged``      — shape *and* counts match the prior run → reuse it.
* ``stats_changed``  — shape matches, counts differ → recompute only statistics,
                       preserving the cached conceptual schema + physical mapping.
* ``shape_changed``  — shape differs → a full re-analysis is required.
* ``no_cache``       — no prior fingerprints supplied.

``AgenticSchemaAnalyzer.analyze_incremental`` wires these together. All probes
are deterministic and snapshot-free (they hit only ``indexes()`` / ``count()``),
so the ``stats_changed`` path skips the analyzer, OWL regeneration, discriminator
detection, and sampling — exactly the work §3.13.3 says to avoid.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from .provenance import compute_valid_time, prior_valid_from, stamp_temporal_provenance
from .snapshot import fingerprint_physical_counts, fingerprint_physical_shape
from .statistics import STATISTICS_STATUS_SKIPPED_NO_DB, compute_statistics
from .types import AnalysisMetadata, AnalysisResult, now_iso
from .utils import normalize_analysis_dict

if TYPE_CHECKING:
    from collections.abc import Iterable

    from arango.database import StandardDatabase

logger = logging.getLogger(__name__)

CHANGE_UNCHANGED = "unchanged"
CHANGE_STATS_CHANGED = "stats_changed"
CHANGE_SHAPE_CHANGED = "shape_changed"
CHANGE_NO_CACHE = "no_cache"


def assess_change_state(
    db: StandardDatabase,
    *,
    prior_shape: str | None = None,
    prior_counts: str | None = None,
    exclude_collections: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Derive the four-valued change state by comparing cheap fingerprints.

    Returns ``{"status", "shapeFingerprint", "countsFingerprint"}``. Pass the
    prior run's ``metadata.shapeFingerprint`` / ``countsFingerprint``; omitting
    either yields ``no_cache``.
    """
    shape = fingerprint_physical_shape(db, exclude_collections=exclude_collections)
    counts = fingerprint_physical_counts(db, exclude_collections=exclude_collections)

    if not prior_shape or not prior_counts:
        status = CHANGE_NO_CACHE
    elif shape != prior_shape:
        status = CHANGE_SHAPE_CHANGED
    elif counts != prior_counts:
        status = CHANGE_STATS_CHANGED
    else:
        status = CHANGE_UNCHANGED

    return {"status": status, "shapeFingerprint": shape, "countsFingerprint": counts}


def _minimal_snapshot(db: StandardDatabase) -> dict[str, Any]:
    """Cheap ``{collections: [{name, type}]}`` from ``db.collections()`` — no
    discriminator detection, no sampling. Enough for ``compute_statistics`` to
    map entities/relationships to collection row counts."""
    info = db.collections()
    entries: list[dict[str, Any]] = []
    items = info if isinstance(info, list) else []
    for c in items:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not isinstance(name, str) or not name or name.startswith("_"):
            continue
        entries.append({"name": name, "type": "edge" if c.get("type") == 3 else "document"})
    return {"collections": entries}


def coerce_prior(prior: AnalysisResult | dict[str, Any]) -> AnalysisResult:
    """Coerce a prior analysis (model or serialized dict, snake or camel) into an
    ``AnalysisResult`` so its conceptual schema / mapping / metadata are usable."""
    if isinstance(prior, AnalysisResult):
        return prior
    data = normalize_analysis_dict(prior)
    raw_meta = data.get("metadata")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_cs = data.get("conceptualSchema")
    raw_pm = data.get("physicalMapping")
    conceptual = raw_cs if isinstance(raw_cs, dict) else {"entities": [], "relationships": [], "properties": []}
    physical = raw_pm if isinstance(raw_pm, dict) else {"entities": {}, "relationships": {}}
    if meta.get("confidence") is not None and meta.get("timestamp"):
        metadata = AnalysisMetadata(**meta)
    else:
        metadata = AnalysisMetadata(
            confidence=0.0,
            timestamp=now_iso(),
            analyzed_collection_counts={"documentCollections": 0, "edgeCollections": 0},
            detected_patterns=[],
        )
    return AnalysisResult(conceptual_schema=conceptual, physical_mapping=physical, metadata=metadata)


def revalidated_copy(pr: AnalysisResult) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Carry ``pr`` forward when the shape fingerprint matched the live database.

    The match just revalidated every element (PRD §3.13.2), so the copies are
    re-stamped, and valid time is carried back along the unbroken chain
    (fingerprint-continuity, §3.13.5). Copies, so the caller's ``prior`` is
    never mutated in place. Returns ``(conceptual, physical, metadata_update)``.
    """
    completed = now_iso()
    conceptual = copy.deepcopy(pr.conceptual_schema)
    physical = copy.deepcopy(pr.physical_mapping)
    stamp_temporal_provenance({"conceptualSchema": conceptual, "physicalMapping": physical}, now=completed)
    valid_time, valid_time_source, predecessor = compute_valid_time(
        completed_at=completed,
        current_shape_fingerprint=pr.metadata.shape_fingerprint,
        prior_shape_fingerprint=pr.metadata.shape_fingerprint,
        prior_valid_from=prior_valid_from(pr.metadata),
    )
    update: dict[str, Any] = {
        "analysis_completed_at": completed,
        "transaction_time": completed,
        "valid_time": valid_time,
        "valid_time_source": valid_time_source,
        "predecessor_fingerprint": predecessor,
    }
    return conceptual, physical, update


def refresh_statistics(
    db: StandardDatabase,
    prior: AnalysisResult | dict[str, Any],
    *,
    exclude_collections: Iterable[str] | None = None,
) -> AnalysisResult:
    """Stats-only refresh: preserve the prior conceptual schema + physical
    mapping, recompute just the statistics block against ``db``, and update the
    counts fingerprint. Skips snapshotting/discriminator/sampling/LLM.

    ``exclude_collections`` must match what the change state was assessed with, so
    the stored counts fingerprint is the one the next run compares against.
    """
    pr = coerce_prior(prior)
    snapshot = _minimal_snapshot(db)
    try:
        stats = compute_statistics(db, snapshot, pr.physical_mapping, pr.conceptual_schema)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("stats-only refresh failed to compute statistics: %s", exc)
        stats = None

    # Counts moved but shape did not, so the schema definition is unchanged.
    conceptual, physical, update = revalidated_copy(pr)
    update["counts_fingerprint"] = fingerprint_physical_counts(db, exclude_collections=exclude_collections)
    update["incremental_refresh"] = "stats_only"
    update["cache_hit"] = False
    if stats is not None:
        update["statistics"] = stats
        update["statistics_status"] = stats.get("status")
    else:
        update["statistics_status"] = STATISTICS_STATUS_SKIPPED_NO_DB

    return AnalysisResult(
        conceptual_schema=conceptual,
        physical_mapping=physical,
        metadata=pr.metadata.model_copy(update=update),
    )
