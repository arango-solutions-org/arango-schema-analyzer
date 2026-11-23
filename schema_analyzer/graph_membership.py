"""Named-graph membership annotation (PRD §3.1 / named-graph scoping).

A database can host several ArangoDB named graphs, share collections across
them, and keep collections outside any graph (loose collections, LPG
discriminator collections). This pass labels *which* named graph(s) each
conceptual entity / relationship belongs to — derived deterministically from
the snapshot's ``graphs_detailed`` (edge definitions + orphan collections) —
without restricting analysis to a single graph.

It complements the optional ``graph_scope`` snapshot filter (which narrows the
collection set up front): membership labels whatever is in view; scope narrows
the view. Membership is many-to-many — a collection may belong to multiple
graphs — so annotations are lists, plus an explicit ``ungraphed`` bucket.

Emits ``metadata.graphMembership`` and tags ``physicalMapping`` entries with a
``graphs: [names]`` field. Snapshot-only, no DB I/O. Returns ``None`` (no
annotation) when the snapshot has no named graphs, preserving byte-identical
output for graphless/LPG databases.
"""

from __future__ import annotations

import logging
from typing import Any

from .utils import graph_collections

logger = logging.getLogger(__name__)


def _graph_collections(snapshot: dict[str, Any]) -> dict[str, dict[str, set[str]]]:
    """Return ``{graph_name: {"vertex": {...}, "edge": {...}}}`` from the snapshot."""
    out: dict[str, dict[str, set[str]]] = {}
    for g in snapshot.get("graphs_detailed") or []:
        if not isinstance(g, dict) or not isinstance(g.get("name"), str) or not g["name"]:
            continue
        vertices, edges = graph_collections(g)
        out[g["name"]] = {"vertex": vertices, "edge": edges}
    return out


def compute_graph_membership(data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Annotate physical-mapping entries with named-graph membership and return
    a ``metadata.graphMembership`` summary. ``None`` when there are no named
    graphs (nothing is mutated)."""
    graphs = _graph_collections(snapshot)
    if not graphs:
        return None

    # collection -> sorted graph names (vertex side and edge side separately).
    vertex_to_graphs: dict[str, list[str]] = {}
    edge_to_graphs: dict[str, list[str]] = {}
    for gname in sorted(graphs):
        for vcol in graphs[gname]["vertex"]:
            vertex_to_graphs.setdefault(vcol, []).append(gname)
        for ecol in graphs[gname]["edge"]:
            edge_to_graphs.setdefault(ecol, []).append(gname)

    raw_pm = data.get("physicalMapping")
    pm = raw_pm if isinstance(raw_pm, dict) else {}
    raw_pm_entities = pm.get("entities")
    pm_entities: dict[str, Any] = raw_pm_entities if isinstance(raw_pm_entities, dict) else {}
    raw_pm_rels = pm.get("relationships")
    pm_rels: dict[str, Any] = raw_pm_rels if isinstance(raw_pm_rels, dict) else {}

    per_graph: dict[str, dict[str, list[str]]] = {
        gname: {"entities": [], "relationships": []} for gname in sorted(graphs)
    }
    ungraphed_entities: list[str] = []
    ungraphed_relationships: list[str] = []

    for ent_name, entry in pm_entities.items():
        if not isinstance(entry, dict):
            continue
        col = entry.get("collectionName")
        names = vertex_to_graphs.get(col) if isinstance(col, str) else None
        if names:
            entry["graphs"] = sorted(names)
            for gname in names:
                per_graph[gname]["entities"].append(ent_name)
        else:
            ungraphed_entities.append(ent_name)

    for rel_type, entry in pm_rels.items():
        if not isinstance(entry, dict):
            continue
        col = entry.get("edgeCollectionName")
        names = edge_to_graphs.get(col) if isinstance(col, str) else None
        if names:
            entry["graphs"] = sorted(names)
            for gname in names:
                per_graph[gname]["relationships"].append(rel_type)
        else:
            ungraphed_relationships.append(rel_type)

    summary_graphs: dict[str, Any] = {}
    for gname in sorted(graphs):
        summary_graphs[gname] = {
            "entities": sorted(per_graph[gname]["entities"]),
            "relationships": sorted(per_graph[gname]["relationships"]),
            "vertexCollections": sorted(graphs[gname]["vertex"]),
            "edgeCollections": sorted(graphs[gname]["edge"]),
        }

    return {
        "status": "ok",
        "graphCount": len(graphs),
        "graphs": summary_graphs,
        "ungraphed": {
            "entities": sorted(ungraphed_entities),
            "relationships": sorted(ungraphed_relationships),
        },
    }
