"""Sharding-pattern classification for the physical snapshot.

Closes the ``Sharding-pattern detection`` roadmap bullet added to
``docs/PRD.md`` §6.2 in commit ``b3d4744`` (2026-04-20). Emits a
``metadata.shardingProfile`` block that classifies the deployment
style once per analysis and carries the per-graph and per-collection
evidence downstream consumers (transpilers, NL→Cypher pipelines,
EXPLAIN-plan validators) need to reason about shard boundaries
without re-deriving the classification from raw snapshot fields.

Five exclusive styles are recognised, ordered most-to-least specific:

* ``OneShard`` — database-level ``sharding == "single"``. Every
  collection shares a shard; cross-collection traversal never
  crosses DBServers. Smart / disjoint graphs cannot coexist with
  OneShard by construction, so this classification wins outright
  when the database property is present.
* ``DisjointSmartGraph`` — at least one named graph has
  ``isSmart == true`` AND ``isDisjoint == true``. The canonical
  ArangoDB multi-tenant pattern: traversal across the disjoint
  attribute is forbidden at the storage layer.
* ``SmartGraph`` — at least one named graph has ``isSmart == true``
  but no graph is disjoint. Vertex collections share the smart
  attribute as their shard key; edge traversals are locality-aware.
* ``SatelliteGraph`` — every user collection in the snapshot has
  ``replicationFactor == "satellite"`` (or ``isSatellite == true``).
  Typical of meta-graph / ontology / reference-data databases where
  every collection is co-located with every shard of every other
  database that joins against it.
* ``Sharded`` (default) — none of the above; standard hash-sharded
  collections. The PRD's fall-through.

Classification is deterministic and snapshot-only. No additional DB
round-trip beyond what :func:`schema_analyzer.snapshot.snapshot_physical_schema`
already makes. Missing fields (older ArangoDB versions, restricted
users whose ``db.properties()`` call returned partial data) degrade
to ``Sharded`` with ``status == "degraded"`` and a human-readable
``statusReason``.

The module is a pure function over the snapshot dict; it has no
dependency on :mod:`schema_analyzer.analyzer` and is safe to import
from tests and from alternative tooling that builds the snapshot
independently.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from .utils import graph_collections

logger = logging.getLogger(__name__)

ShardingStyle = Literal[
    "OneShard",
    "DisjointSmartGraph",
    "SmartGraph",
    "SatelliteGraph",
    "Sharded",
]

ShardingStatus = Literal["ok", "degraded"]

CollectionKind = Literal[
    "smartgraph",
    "satellite",
    "regular",
    "system",
]

# Fields from ``col.properties()`` that carry shard topology. Kept
# in a constant so the snapshot augmentation step (see
# :mod:`schema_analyzer.snapshot`) and this classifier agree on the
# name list.
_SHARD_PROPERTY_KEYS = (
    "numberOfShards",
    "shardKeys",
    "shardingStrategy",
    "replicationFactor",
    "writeConcern",
    "minReplicationFactor",
    "distributeShardsLike",
    "smartGraphAttribute",
    "isSmart",
    "isDisjoint",
    "isSatellite",
    "isSystem",
)


def _get_props(collection_entry: dict[str, Any]) -> dict[str, Any]:
    """Return the raw ``col.properties()`` sub-dict for a snapshot entry.

    Snapshot entries store the raw python-arango response under the
    ``properties`` key (see :func:`schema_analyzer.snapshot.snapshot_physical_schema`
    Phase 1). Tolerates a missing / non-dict entry by returning an
    empty dict so the caller doesn't have to.
    """
    props = collection_entry.get("properties")
    return props if isinstance(props, dict) else {}


def _classify_collection_kind(props: dict[str, Any]) -> CollectionKind:
    """Classify one collection by its ``col.properties()`` dict.

    Precedence matches the upstream PRD §6.2 bullet-3 rules:

    1. ``isSystem == true`` → ``"system"``.
    2. ``replicationFactor == "satellite"`` (or ``isSatellite == true``)
       → ``"satellite"``.
    3. ``isSmart == true`` → ``"smartgraph"`` (caller decides
       disjoint-vs-non-disjoint downstream from the graphs list).
    4. Otherwise → ``"regular"``.
    """
    if props.get("isSystem") is True:
        return "system"
    rep = props.get("replicationFactor")
    if rep == "satellite" or props.get("isSatellite") is True:
        return "satellite"
    if props.get("isSmart") is True:
        return "smartgraph"
    return "regular"


def _graph_evidence(
    graph_entry: dict[str, Any],
) -> dict[str, Any] | None:
    """Distil one named-graph snapshot entry into the evidence shape.

    Returns ``None`` when the entry has no name — nothing useful for
    downstream consumers. When the graph is not smart, we still emit
    its edge-definition collections so Satellite / Sharded consumers
    can trace which collections belong to which graph.
    """
    name = graph_entry.get("name")
    if not isinstance(name, str) or not name:
        return None

    vertex_cols, edge_cols = graph_collections(graph_entry)

    ev: dict[str, Any] = {
        "name": name,
        "vertexCollections": sorted(vertex_cols),
        "edgeCollections": sorted(edge_cols),
    }
    for k in ("isSmart", "isDisjoint", "smartGraphAttribute", "isSatellite"):
        if k in graph_entry:
            ev[k] = graph_entry[k]
    return ev


def _build_collections_block(
    snapshot: dict[str, Any],
    *,
    graph_by_collection: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Per-collection shard-evidence block keyed by collection name.

    The block is always populated (even for OneShard / single-server
    databases) so downstream consumers have a uniform interface for
    reading sharding hints without having to branch on style. Omits
    system collections — they carry no information a consumer can act
    on.
    """
    out: dict[str, dict[str, Any]] = {}
    for entry in snapshot.get("collections") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        props = _get_props(entry)
        kind = _classify_collection_kind(props)
        if kind == "system":
            continue

        block: dict[str, Any] = {"kind": kind}
        for key in _SHARD_PROPERTY_KEYS:
            if key in ("isSystem",):
                continue
            if key in props and props[key] is not None:
                block[key] = props[key]

        graph_name = graph_by_collection.get(name)
        if graph_name:
            block["graphName"] = graph_name

        out[name] = block
    return out


def _graph_membership_index(
    graphs: list[dict[str, Any]],
) -> dict[str, str]:
    """Map ``collection name → first graph name`` for quick lookup.

    Deterministic: when a collection appears in multiple graphs the
    lexicographically first graph name wins (matches
    ``_summarize_graph_props`` sorting already applied upstream).
    """
    out: dict[str, str] = {}
    for g in sorted(graphs, key=lambda x: str(x.get("name") or "")):
        name = g.get("name")
        if not isinstance(name, str) or not name:
            continue
        vertices, edges = graph_collections(g)
        for col in vertices | edges:
            out.setdefault(col, name)
    return out


def _graph_is_smart(g: dict[str, Any]) -> bool:
    return g.get("isSmart") is True


def _graph_is_disjoint(g: dict[str, Any]) -> bool:
    return g.get("isSmart") is True and g.get("isDisjoint") is True


def _pick_style(
    *,
    database_sharding: str | None,
    graphs: list[dict[str, Any]],
    collections: dict[str, dict[str, Any]],
) -> ShardingStyle:
    """Choose the primary classifier given the assembled evidence."""
    if database_sharding == "single":
        return "OneShard"
    if any(_graph_is_disjoint(g) for g in graphs):
        return "DisjointSmartGraph"
    if any(_graph_is_smart(g) for g in graphs):
        return "SmartGraph"
    # SatelliteGraph: every non-system user collection is a satellite
    # AND there is at least one such collection (avoid classifying an
    # empty database as Satellite).
    kinds = [b.get("kind") for b in collections.values()]
    non_empty = bool(kinds)
    all_sat = non_empty and all(k == "satellite" for k in kinds)
    if all_sat:
        return "SatelliteGraph"
    return "Sharded"


def _pick_one_shard_leader(
    collections: dict[str, dict[str, Any]],
) -> str | None:
    """Heuristic: pick the OneShard leader as the collection every
    other user collection is ``distributeShardsLike``'d from.

    When the database is genuinely OneShard the server typically elects
    a single leader (often ``_users`` or ``_graphs``; for user-data-only
    OneShard databases, the first alphabetically is a reasonable
    anchor). Returns ``None`` when no single leader emerges — callers
    tolerate the absence.
    """
    leaders: set[str] = set()
    for block in collections.values():
        lead = block.get("distributeShardsLike")
        if isinstance(lead, str) and lead:
            leaders.add(lead)
    if len(leaders) == 1:
        return next(iter(leaders))
    return None


def classify_sharding_profile(
    snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    """Classify the snapshot by sharding pattern and return the
    ``metadata.shardingProfile`` block.

    Parameters
    ----------
    snapshot:
        The raw output of
        :func:`schema_analyzer.snapshot.snapshot_physical_schema`. Must
        carry ``collections`` (list of per-collection entries) and
        ``graphs_detailed`` (list of per-graph entries) to be useful.
        Missing keys degrade to ``Sharded`` / ``degraded`` rather than
        raising — this function must never break an analysis.

    Returns
    -------
    dict | None
        The ``shardingProfile`` block, or ``None`` when the snapshot
        is so minimal that classification is meaningless (no
        collections array at all, or the snapshot is not a dict).
        Callers should treat ``None`` as "omit the block from
        metadata", matching the contract used by
        :func:`schema_analyzer.tenant_scope.annotate_tenant_scope`
        and :func:`schema_analyzer.reconcile.reconcile_physical_mapping`.
    """
    if not isinstance(snapshot, dict):
        return None
    collections_raw = snapshot.get("collections")
    if not isinstance(collections_raw, list):
        return None

    db_block = snapshot.get("database")
    database_sharding: str | None = None
    database_block: dict[str, Any] = {}
    if isinstance(db_block, dict):
        raw_sharding = db_block.get("sharding")
        if isinstance(raw_sharding, str):
            database_sharding = raw_sharding
        for k in ("name", "sharding", "replicationFactor", "writeConcern"):
            if k in db_block and db_block[k] is not None:
                database_block[k] = db_block[k]

    graphs_raw = snapshot.get("graphs_detailed")
    graphs: list[dict[str, Any]] = (
        [g for g in graphs_raw if isinstance(g, dict) and not g.get("error")] if isinstance(graphs_raw, list) else []
    )

    graph_by_collection = _graph_membership_index(graphs)
    collections = _build_collections_block(
        snapshot,
        graph_by_collection=graph_by_collection,
    )

    degraded_reasons: list[str] = []
    graphs_probe_failed = isinstance(graphs_raw, list) and any(
        isinstance(g, dict) and g.get("error") for g in graphs_raw
    )
    if graphs_probe_failed:
        degraded_reasons.append("at least one graph properties probe failed")
    if "graphs_error" in snapshot:
        degraded_reasons.append(
            f"graph enumeration failed: {snapshot.get('graphs_error')}",
        )
    if not collections:
        degraded_reasons.append("no user collections in snapshot")

    style = _pick_style(
        database_sharding=database_sharding,
        graphs=graphs,
        collections=collections,
    )

    graph_evidence = [ev for ev in (_graph_evidence(g) for g in graphs) if ev is not None]

    satellite_collections = sorted(name for name, block in collections.items() if block.get("kind") == "satellite")

    collection_kind_counts = {
        "smartgraph": sum(1 for b in collections.values() if b.get("kind") == "smartgraph"),
        "satellite": sum(1 for b in collections.values() if b.get("kind") == "satellite"),
        "regular": sum(1 for b in collections.values() if b.get("kind") == "regular"),
    }

    block: dict[str, Any] = {
        "style": style,
        "status": "degraded" if degraded_reasons else "ok",
        "database": database_block,
        "collectionKindCounts": collection_kind_counts,
        "graphs": graph_evidence,
        "satelliteCollections": satellite_collections,
        "collections": collections,
    }
    if degraded_reasons:
        block["statusReason"] = "; ".join(degraded_reasons)
    if style == "OneShard":
        leader = _pick_one_shard_leader(collections)
        if leader:
            block["oneShardLeader"] = leader

    logger.info(
        "Sharding profile: style=%s graphs=%d smart=%d disjoint=%d satellites=%d regular=%d status=%s",
        style,
        len(graph_evidence),
        sum(1 for g in graph_evidence if g.get("isSmart")),
        sum(1 for g in graph_evidence if g.get("isDisjoint")),
        collection_kind_counts["satellite"],
        collection_kind_counts["regular"],
        block["status"],
    )

    return block


__all__ = [
    "CollectionKind",
    "ShardingStatus",
    "ShardingStyle",
    "classify_sharding_profile",
]
