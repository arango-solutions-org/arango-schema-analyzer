"""Vertex-Centric Index (VCI) detection (PRD §6.1 / §6.2).

ArangoDB query planners can serve a traversal directly from a persistent index
rooted at ``_from`` / ``_to`` when that index also carries the discriminator (and
sometimes denormalized) fields a filter touches — the classic *vertex-centric
index* optimization. Surfacing this lets downstream transpilers/planners emit a
VCI-aware traversal instead of a generic edge scan + filter.

Two complementary, deterministic signals are computed straight from the
snapshot (no extra DB round-trip):

* **Index-level** — persistent indexes on an edge collection whose first field
  is ``_from`` or ``_to`` plus one or more trailing discriminator fields
  (e.g. ``[_from, type]``, ``[_to, type, validFrom]``). Reports the
  participating discriminator fields, ``unique`` / ``sparse``, and the access
  pattern (``out-edge`` / ``in-edge`` / ``both``).
* **Denormalization** — edge attributes that duplicate properties of their
  endpoint vertex collections (denormalized onto the edge for VCI lookups).
  Reports the duplicated field and its source vertex collection(s).

When either signal fires for a relationship, the relationship's physical mapping
entry gains a ``vci`` block and ``vciCandidate: true`` *alongside* its existing
``DEDICATED_COLLECTION`` / ``GENERIC_WITH_TYPE`` style (the style is never
overwritten). A ``metadata.vci`` summary lists the relationships involved.

**Array-expanded fields are flagged, not advertised.** A persistent index stores one
entry per array element, and a traversal scans every entry under the ``_from`` /
``_to`` prefix -- so an edge index such as ``[_from, type, toLabels[*]]`` returns each
edge once *per element*: inflated counts, no error (measured on ArangoDB 3.12.9 at
20 labels per edge: 20x the rows at one hop, 400x at two). Such an index is still
reported, marked ``traversalSafe: false`` with a ``risk`` code, its ``[*]`` fields
listed under ``arrayExpandedFields`` rather than ``discriminatorFields``, and a
``metadata.vci.warnings`` entry. It never makes a relationship a ``vciCandidate`` on
its own, and ``accessPattern`` counts only traversal-safe indexes (it is omitted when
there are none). It remains correct for a pattern-match lookup (``FOR e IN edges FILTER
e._from == ...``), which is why r2g can provision it on request.
"""

from __future__ import annotations

import logging
from typing import Any

from .utils import SYSTEM_FIELDS, collections_by_name, observed_field_names

logger = logging.getLogger(__name__)

_EDGE_ROOTS = ("_from", "_to")
_ARRAY_EXPANSION_RISK = "ARRAY_EXPANSION_DUPLICATES_TRAVERSAL_ROWS"
_ARRAY_EXPANSION_MESSAGE = (
    "Edge index rooted at {root} contains array-expanded field(s) {fields}: a traversal "
    "that selects it returns each edge once per array element. Do not hint it in "
    "traversals; it is correct only for pattern-match lookups on the edge collection."
)


def _index_level_vci(collection: dict[str, Any]) -> list[dict[str, Any]]:
    """Persistent indexes rooted at _from/_to with trailing discriminator fields."""
    found: list[dict[str, Any]] = []
    for idx in collection.get("indexes", []) or []:
        if not isinstance(idx, dict) or idx.get("type") != "persistent":
            continue
        fields = idx.get("fields")
        if not isinstance(fields, list) or len(fields) < 2:
            continue
        root = fields[0]
        if root not in _EDGE_ROOTS:
            continue
        trailing = [f for f in fields[1:] if isinstance(f, str)]
        if not trailing:
            continue
        expanded = [f for f in trailing if "[*]" in f]
        entry: dict[str, Any] = {
            "root": root,
            "fields": [f for f in fields if isinstance(f, str)],
            "discriminatorFields": [f for f in trailing if f not in expanded],
            "unique": bool(idx.get("unique", False)),
            "sparse": bool(idx.get("sparse", False)),
        }
        if expanded:
            entry["traversalSafe"] = False
            entry["arrayExpandedFields"] = expanded
            entry["risk"] = _ARRAY_EXPANSION_RISK
        found.append(entry)
    return found


def _access_pattern(indexes: list[dict[str, Any]]) -> str:
    roots = {i["root"] for i in indexes}
    if "_from" in roots and "_to" in roots:
        return "both"
    if "_from" in roots:
        return "out-edge"
    return "in-edge"


def _denormalized_fields(
    edge_entry: dict[str, Any],
    endpoints: dict[str, Any],
    collections: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Edge fields that also appear on an endpoint vertex collection."""
    edge_fields = observed_field_names(edge_entry) - SYSTEM_FIELDS
    if not edge_fields:
        return []
    endpoint_names: list[str] = []
    for key in ("from_collections", "to_collections"):
        vals = endpoints.get(key)
        if isinstance(vals, list):
            endpoint_names.extend(v for v in vals if isinstance(v, str))

    by_field: dict[str, set[str]] = {}
    for cname in endpoint_names:
        vertex = collections.get(cname)
        if not isinstance(vertex, dict):
            continue
        shared = (observed_field_names(vertex) - SYSTEM_FIELDS) & edge_fields
        for f in shared:
            by_field.setdefault(f, set()).add(cname)

    return [{"field": f, "sourceCollections": sorted(srcs)} for f, srcs in sorted(by_field.items())]


def detect_vci(data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Annotate relationship mappings with VCI signals and return a summary.

    Returns ``None`` when there is nothing to report. Otherwise returns
    ``{"status": "ok", "relationships": [..candidates..]}`` -- plus ``warnings``
    when an edge index has array-expanded fields -- and mutates
    ``data["physicalMapping"]["relationships"][rt]`` in place, adding a ``vci``
    block to each relationship with a signal and ``vciCandidate: true`` to each
    one with a traversal-safe index or a denormalized field.
    """
    pm = data.get("physicalMapping")
    if not isinstance(pm, dict):
        return None
    rels = pm.get("relationships")
    if not isinstance(rels, dict) or not rels:
        return None

    collections = collections_by_name(snapshot)
    matched: list[str] = []
    warnings: list[dict[str, Any]] = []

    for rtype, mapping in rels.items():
        if not isinstance(mapping, dict):
            continue
        edge_name = mapping.get("edgeCollectionName")
        if not isinstance(edge_name, str) or edge_name not in collections:
            continue
        edge_entry = collections[edge_name]

        index_vci = _index_level_vci(edge_entry)
        raw_endpoints = edge_entry.get("edge_endpoints")
        endpoints = raw_endpoints if isinstance(raw_endpoints, dict) else {}
        denorm = _denormalized_fields(edge_entry, endpoints, collections)

        if not index_vci and not denorm:
            continue

        # An index a traversal cannot safely use is reported, never advertised.
        safe = [i for i in index_vci if i.get("traversalSafe") is not False]
        vci_block: dict[str, Any] = {}
        if index_vci:
            index_level: dict[str, Any] = {"indexes": index_vci}
            if safe:
                index_level["accessPattern"] = _access_pattern(safe)
            vci_block["indexLevel"] = index_level
        if denorm:
            vci_block["denormalization"] = {"duplicatedFields": denorm}

        mapping["vci"] = vci_block
        for i in index_vci:
            if i.get("traversalSafe") is False:
                warnings.append(
                    {
                        "relationship": rtype,
                        "edgeCollection": edge_name,
                        "fields": i["fields"],
                        "code": _ARRAY_EXPANSION_RISK,
                        "message": _ARRAY_EXPANSION_MESSAGE.format(
                            root=i["root"], fields=", ".join(i["arrayExpandedFields"])
                        ),
                    }
                )
        if denorm or safe:
            mapping["vciCandidate"] = True
            matched.append(rtype)

    if not matched and not warnings:
        return None

    summary: dict[str, Any] = {"status": "ok", "relationships": sorted(matched)}
    if warnings:
        summary["warnings"] = warnings
    return summary
