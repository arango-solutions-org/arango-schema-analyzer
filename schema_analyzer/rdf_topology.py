"""RDF-topology (RPT) detection and the TRIPLE mapping style (PRD §6.1/§6.2).

Some ArangoDB schemas store data as RDF triples rather than as a native
property graph: a ``_triples`` collection with ``subject``/``predicate``/
``object`` documents, and/or ``rdf:type`` assertion edges. Such schemas need a
``TRIPLE`` mapping treatment — a transpiler must rewrite triple patterns into
filters over the subject/predicate/object columns instead of native traversal.

This module detects the topology deterministically from the snapshot (names +
observed fields + sampled predicate values) and emits:

* ``metadata.rdfTopology`` — a classification block with the triple
  collections, the ``rdf:type`` edges, and the evidence that drove it.
* on each affected physical-mapping entry, ``tripleCandidate: true`` and a
  ``triple`` block (the detected signature), *alongside* the existing style.

No DB round-trip beyond the snapshot; no LLM dependency.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .defaults import (
    RDF_PREDICATE_FIELD_NAMES,
    RDF_TRIPLE_COLLECTION_NAME_PATTERNS,
    RDF_TRIPLE_FIELD_SIGNATURES,
    RDF_TYPE_PREDICATE_VALUES,
)
from .utils import SYSTEM_FIELDS, observed_field_names

logger = logging.getLogger(__name__)

_NAME_PATTERNS = tuple(re.compile(p) for p in RDF_TRIPLE_COLLECTION_NAME_PATTERNS)


def _name_matches_triple(name: str) -> bool:
    return any(p.search(name) for p in _NAME_PATTERNS)


def _field_signature(fields: set[str]) -> frozenset[str] | None:
    bare = fields - SYSTEM_FIELDS
    for sig in RDF_TRIPLE_FIELD_SIGNATURES:
        if sig <= bare:
            return sig
    return None


def _type_predicate_values(entry: dict[str, Any]) -> list[str]:
    """Sampled values of any predicate-like field that denote ``rdf:type``."""
    svc = entry.get("sample_field_value_counts")
    if not isinstance(svc, dict):
        return []
    found: set[str] = set()
    for field, items in svc.items():
        if field not in RDF_PREDICATE_FIELD_NAMES or not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                val = item.get("value")
                if isinstance(val, str) and val.lower() in {v.lower() for v in RDF_TYPE_PREDICATE_VALUES}:
                    found.add(val)
    return sorted(found)


def detect_rdf_topology(data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Classify the snapshot's RDF topology and annotate matching mappings.

    Returns ``None`` when the snapshot has no collections to classify.
    Otherwise returns a ``metadata.rdfTopology`` block and mutates matching
    ``physicalMapping`` entries in place.
    """
    collections = snapshot.get("collections")
    if not isinstance(collections, list) or not collections:
        return None

    triple_collections: list[dict[str, Any]] = []
    type_edges: list[dict[str, Any]] = []
    triple_collection_names: set[str] = set()

    for entry in collections:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        name_match = _name_matches_triple(name)
        # Skip system collections (leading underscore) unless the name itself
        # matches a triple pattern (e.g. a literally-named ``_triples``).
        if name.startswith("_") and not name_match:
            continue
        fields = observed_field_names(entry)
        sig = _field_signature(fields)
        if sig is not None or name_match:
            triple_collections.append(
                {
                    "collection": name,
                    "signature": "_".join(sorted(sig)) if sig else "name_match",
                    "fields": sorted(fields - SYSTEM_FIELDS),
                }
            )
            triple_collection_names.add(name)

        type_vals = _type_predicate_values(entry)
        if type_vals:
            type_edges.append({"collection": name, "typeValues": type_vals})

    is_rdf = bool(triple_collections or type_edges)
    if not is_rdf:
        return {"status": "ok", "isRdfTopology": False}

    _annotate_mappings(data, triple_collection_names)

    evidence: list[str] = []
    if triple_collections:
        evidence.append(f"{len(triple_collections)} triple collection(s)")
    if type_edges:
        evidence.append(f"{len(type_edges)} rdf:type edge collection(s)")

    return {
        "status": "ok",
        "isRdfTopology": True,
        "tripleCollections": triple_collections,
        "typeEdges": type_edges,
        "evidence": evidence,
    }


def _annotate_mappings(data: dict[str, Any], triple_collection_names: set[str]) -> None:
    pm = data.get("physicalMapping")
    if not isinstance(pm, dict):
        return
    entities = pm.get("entities")
    if isinstance(entities, dict):
        for mapping in entities.values():
            if isinstance(mapping, dict) and mapping.get("collectionName") in triple_collection_names:
                mapping["tripleCandidate"] = True
                mapping["triple"] = {"style": "TRIPLE"}
    relationships = pm.get("relationships")
    if isinstance(relationships, dict):
        for mapping in relationships.values():
            if isinstance(mapping, dict) and mapping.get("edgeCollectionName") in triple_collection_names:
                mapping["tripleCandidate"] = True
                mapping["triple"] = {"style": "TRIPLE"}
