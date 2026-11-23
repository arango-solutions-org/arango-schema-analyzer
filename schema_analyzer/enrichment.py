"""Post-inference enrichment pipeline.

After the LLM (or deterministic baseline) produces a ``{conceptualSchema,
physicalMapping, metadata}`` payload, a sequence of deterministic, snapshot-only
passes annotate it: collection-name allowlisting, reconciliation backfill,
sharding profile, shard families, VCI, RDF topology, multitenancy, tenant scope,
and per-relationship statistics.

These functions were extracted from ``analyzer.py`` so the orchestration class
stays focused on the LLM/cache/result lifecycle while the enrichment steps live
together as a coherent, independently-testable unit. Every function mutates the
``data`` dict in place and is a safe no-op when its signal is absent. ``analyzer``
re-exports them, so ``from schema_analyzer.analyzer import _apply_*`` continues
to work.
"""

from __future__ import annotations

import logging
from typing import Any

from .arango_products import detect_arango_products
from .graph_membership import compute_graph_membership
from .graphrag import detect_graphrag
from .list_values import apply_list_values
from .multitenancy import classify_multitenancy
from .nested_fields import apply_field_locations
from .rdf_topology import detect_rdf_topology
from .reconcile import reconcile_physical_mapping, strip_unknown_collection_names
from .shard_families import detect_shard_families
from .sharding_profile import classify_sharding_profile
from .statistics import STATISTICS_STATUS_SKIPPED_NO_DB, compute_statistics
from .tenant_scope import annotate_tenant_scope
from .vci import detect_vci

logger = logging.getLogger(__name__)


def _metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Return ``data["metadata"]`` as a dict, creating/repairing it if needed."""
    meta = data.setdefault("metadata", {})
    if not isinstance(meta, dict):
        meta = {}
        data["metadata"] = meta
    return meta


def _arango_product_for(snapshot: dict) -> tuple[dict | None, str]:
    """The ``(arango_product, arango_product_status)`` metadata pair for a snapshot:
    the report block and ``'ok'`` when any product is detected, ``(None, 'none')`` otherwise."""
    report = detect_arango_products(snapshot)
    if report.is_empty:
        return None, "none"
    return report.to_dict(), "ok"


def _apply_collection_name_allowlist(
    data: dict[str, Any],
    snapshot: dict[str, Any],
    warnings: list[str],
) -> None:
    """
    Strip any LLM-supplied ``collectionName`` / ``edgeCollectionName``
    that does not name a real collection in ``snapshot``. Each strip
    appends a warning so the caller can audit what was discarded.

    Runs BEFORE :func:`_apply_reconciliation` so that stripped entries
    become eligible for deterministic baseline backfill in the same pass.
    """
    msgs = strip_unknown_collection_names(data, snapshot)
    if msgs:
        warnings.extend(msgs)
        for m in msgs:
            logger.warning("Collection-name allowlist: %s", m)


def _apply_reconciliation(
    data: dict[str, Any],
    snapshot: dict[str, Any],
    warnings: list[str],
) -> None:
    """
    Run post-LLM collection-coverage reconciliation and fold the summary
    into ``data["metadata"]`` + the caller-owned warnings list.

    No-op (no metadata mutation, no warning appended) when the LLM output
    already covers every snapshot collection.
    """
    summary = reconcile_physical_mapping(data, snapshot)
    if summary is None:
        return

    meta = _metadata(data)
    meta["reconciliation"] = summary

    backfilled = summary.get("backfilled_collections") or []
    warning_msg = (
        f"LLM physical mapping omitted {len(backfilled)} "
        f"snapshot collection{'s' if len(backfilled) != 1 else ''}; "
        f"backfilled from baseline: {', '.join(backfilled)}"
    )
    warnings.append(warning_msg)
    logger.info(
        "Reconciliation: backfilled %d missing collection(s) from baseline: %s",
        len(backfilled),
        backfilled,
    )


def _apply_nested_fields(data: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Settle field vs path for every property mapping and stamp ``metadata.nestedFields``.

    Issue #33. A no-op (nothing written) unless the snapshot looked inside nested
    objects (``nestedFieldDepth`` > 0), so default output stays byte-identical.
    """
    report = apply_field_locations(data, snapshot)
    if report is not None:
        _metadata(data)["nestedFields"] = report


def _apply_list_values(data: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Mark ``multiValued`` on each property mapping whose attribute the sample holds
    as a list (CDF #220). Runs after :func:`_apply_nested_fields`; writes nothing for a
    database without lists."""
    apply_list_values(data, snapshot)


def _apply_sharding_profile(
    data: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    """Classify the snapshot by sharding pattern and stamp
    ``metadata.shardingProfile`` + ``metadata.shardingProfileStatus``.

    Always safe to call — a snapshot too minimal to classify (no
    collections, pre-0.x snapshot without the ``database`` block, etc.)
    results in a no-op; nothing is written. Matches the contract used
    by :func:`_apply_reconciliation` and :func:`_apply_tenant_scope`
    for features that don't apply to every graph.
    """
    profile = classify_sharding_profile(snapshot)
    if profile is None:
        return
    meta = _metadata(data)
    meta["shardingProfile"] = profile
    meta["shardingProfileStatus"] = profile.get("status")


def _apply_shard_families(data: dict[str, Any]) -> None:
    """Detect shard families across ``data["physicalMapping"]["entities"]``
    and stamp ``data["physicalMapping"]["shardFamilies"]``.

    Always safe to call. Writes nothing (preserves the prior physical
    mapping byte-for-byte) when the input has no usable entity dict —
    consumers can then distinguish "didn't run" from "ran, found
    none" (the latter writes an explicit empty list).
    """
    families = detect_shard_families(data)
    if families is None:
        return
    pm = data.get("physicalMapping")
    if not isinstance(pm, dict):
        return
    pm["shardFamilies"] = families


def _apply_vci(
    data: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    """Detect vertex-centric-index patterns and annotate relationship mappings
    with a ``vci`` block + ``metadata.vci`` summary.

    Always safe to call; a no-op when there are no relationship mappings or no
    VCI signals are present.
    """
    summary = detect_vci(data, snapshot)
    if summary is None:
        return
    _metadata(data)["vci"] = summary


def _apply_rdf_topology(
    data: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    """Detect RDF topology (TRIPLE style) and stamp ``metadata.rdfTopology``.

    Always safe to call; a no-op when the snapshot has no collections.
    """
    block = detect_rdf_topology(data, snapshot)
    if block is None:
        return
    _metadata(data)["rdfTopology"] = block


def _apply_graphrag(
    data: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    """Detect a GraphRAG topology (chunks/entities/similarity/mention edges)
    and stamp ``metadata.graphRag`` + per-mapping ``graphRagRole`` annotations.

    Always safe to call; a no-op when the snapshot has no collections.
    """
    block = detect_graphrag(data, snapshot)
    if block is None:
        return
    _metadata(data)["graphRag"] = block


def _apply_graph_membership(
    data: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    """Annotate physical-mapping entries with named-graph membership and stamp
    ``metadata.graphMembership``.

    Always safe to call; a no-op when the snapshot has no named graphs
    (graphless / LPG databases stay byte-identical).
    """
    block = compute_graph_membership(data, snapshot)
    if block is None:
        return
    _metadata(data)["graphMembership"] = block


def _apply_multitenancy(
    data: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    """Classify the snapshot by multitenancy pattern and stamp
    ``metadata.multitenancy`` + ``metadata.multitenancyStatus``.

    Must run *after* :func:`_apply_sharding_profile` so the
    disjoint-smartgraph branch can consume the sharding profile.
    Always safe to call; a no-op when the snapshot has no user
    collections.
    """
    sharding = (data.get("metadata") or {}).get("shardingProfile")
    block = classify_multitenancy(data, snapshot, sharding_profile=sharding)
    if block is None:
        return
    meta = _metadata(data)
    meta["multitenancy"] = block
    meta["multitenancyStatus"] = block.get("status")


def _apply_tenant_scope(data: dict[str, Any]) -> None:
    """Annotate ``physicalMapping.entities[*].tenantScope`` and stamp a
    ``metadata.tenantScopeReport`` summary.

    No-op (and no metadata block) when no tenant root is detected,
    matching :func:`_apply_reconciliation`'s contract for graphs that
    don't need the feature. Always safe to call after reconciliation.
    """
    summary = annotate_tenant_scope(data)
    if summary is None:
        return
    _metadata(data)["tenantScopeReport"] = summary
    logger.info(
        "Tenant scope: root=%s denorm=%d traversal=%d global=%d",
        summary.get("tenantEntity"),
        summary.get("denormScopedCount", 0),
        summary.get("traversalScopedCount", 0),
        summary.get("globalCount", 0),
    )


def _apply_statistics(
    db: Any,
    data: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    """
    Run the per-relationship statistics pass (issue #3) and stamp its
    output onto ``data["metadata"]``.

    * When ``db`` is ``None`` or ``compute_statistics`` returns ``None``
      we set ``metadata.statistics_status = "skipped_no_db"`` and leave
      ``metadata.statistics`` absent — this is the documented snapshot-
      only contract.
    * Otherwise ``metadata.statistics`` carries the full block and
      ``metadata.statistics_status`` mirrors the inner ``status`` field
      so consumers can branch on a single top-level key.

    AQL errors on individual collections are already absorbed inside
    ``compute_statistics`` (they surface as ``status="partial"``); this
    wrapper logs + swallows any other unexpected failure so statistics
    never break the analysis as a whole.
    """
    meta = _metadata(data)

    if db is None:
        meta["statistics_status"] = STATISTICS_STATUS_SKIPPED_NO_DB
        return

    try:
        block = compute_statistics(
            db,
            snapshot,
            data.get("physicalMapping") or {},
            data.get("conceptualSchema"),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("statistics computation failed: %s", exc)
        meta["statistics_status"] = STATISTICS_STATUS_SKIPPED_NO_DB
        return

    if block is None:
        meta["statistics_status"] = STATISTICS_STATUS_SKIPPED_NO_DB
        return

    meta["statistics"] = block
    meta["statistics_status"] = block.get("status")
