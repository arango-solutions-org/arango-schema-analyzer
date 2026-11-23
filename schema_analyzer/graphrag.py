"""GraphRAG template detection (PRD §6.2).

Recognizes the *generic* topology of a retrieval-augmented-generation graph —
text **chunks** carrying embeddings, extracted **entities**, vector
**similarity** edges, and chunk→entity **mention** edges — regardless of which
tool produced it. This complements ``arango_products.py``, which keys off
ArangoDB's branded *Autograph* naming convention; this detector fires on any
schema that structurally looks like GraphRAG.

Deterministic and snapshot-only (collection names + observed fields + index
types; no samples, no LLM, no DB I/O). Emits ``metadata.graphRag`` and tags the
participating physical-mapping entries with a ``graphRagRole``
(``chunk`` / ``entity``) and edges with ``similarity`` / ``mention``.

``isGraphRag`` is only asserted when **two or more** signal categories fire, so
a lone ``embedding`` field or a collection literally named ``entities`` does not
trip a false positive.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .defaults import (
    GRAPHRAG_CHUNK_NAME_PATTERNS,
    GRAPHRAG_EMBEDDING_FIELDS,
    GRAPHRAG_ENTITY_NAME_PATTERNS,
    GRAPHRAG_MENTION_EDGE_NAME_PATTERNS,
    GRAPHRAG_SIMILARITY_EDGE_FIELDS,
    GRAPHRAG_SIMILARITY_EDGE_NAME_PATTERNS,
    GRAPHRAG_TEXT_FIELDS,
    GRAPHRAG_VECTOR_INDEX_TYPES,
)
from .utils import observed_field_names

logger = logging.getLogger(__name__)

_CHUNK_NAME_RE = tuple(re.compile(p) for p in GRAPHRAG_CHUNK_NAME_PATTERNS)
_ENTITY_NAME_RE = tuple(re.compile(p) for p in GRAPHRAG_ENTITY_NAME_PATTERNS)
_SIM_NAME_RE = tuple(re.compile(p) for p in GRAPHRAG_SIMILARITY_EDGE_NAME_PATTERNS)
_MENTION_NAME_RE = tuple(re.compile(p) for p in GRAPHRAG_MENTION_EDGE_NAME_PATTERNS)


def _matches(name: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(p.search(name) for p in patterns)


def _has_vector_index(entry: dict[str, Any]) -> list[str]:
    """Return the field set of any vector / embedding index on the collection."""
    fields: list[str] = []
    for idx in entry.get("indexes", []) or []:
        if not isinstance(idx, dict):
            continue
        idx_fields = [f for f in (idx.get("fields") or []) if isinstance(f, str)]
        if str(idx.get("type", "")).lower() in GRAPHRAG_VECTOR_INDEX_TYPES:
            fields.extend(idx_fields)
        elif any(f.lower() in GRAPHRAG_EMBEDDING_FIELDS for f in idx_fields):
            fields.extend(f for f in idx_fields if f.lower() in GRAPHRAG_EMBEDDING_FIELDS)
    return sorted(set(fields))


def _is_chunk_collection(name: str, fields: set[str]) -> bool:
    lower = {f.lower() for f in fields}
    has_embedding = bool(lower & GRAPHRAG_EMBEDDING_FIELDS)
    has_text = bool(lower & GRAPHRAG_TEXT_FIELDS)
    # Strong: an embedding field. Otherwise require a chunk-like name + text.
    if has_embedding:
        return True
    return _matches(name, _CHUNK_NAME_RE) and has_text


def _is_entity_collection(name: str, fields: set[str]) -> bool:
    if _matches(name, _ENTITY_NAME_RE):
        return True
    lower = {f.lower() for f in fields}
    # Heuristic entity shape: a name plus a type/description discriminator.
    return "name" in lower and bool(lower & {"type", "description", "entity_type", "category"})


def _edge_role(name: str, fields: set[str]) -> str | None:
    lower = {f.lower() for f in fields}
    if _matches(name, _SIM_NAME_RE) or bool(lower & GRAPHRAG_SIMILARITY_EDGE_FIELDS):
        return "similarity"
    if _matches(name, _MENTION_NAME_RE):
        return "mention"
    return None


def detect_graphrag(data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Classify the snapshot's GraphRAG topology and annotate matching mappings.

    Returns ``None`` when the snapshot has no collections to classify. Otherwise
    returns a ``metadata.graphRag`` block and (when GraphRAG is detected) tags
    physical-mapping entries with ``graphRagRole`` in place.
    """
    collections = snapshot.get("collections")
    if not isinstance(collections, list) or not collections:
        return None

    chunk_collections: list[str] = []
    entity_collections: list[str] = []
    similarity_edges: list[str] = []
    mention_edges: list[str] = []
    vector_indexes: list[dict[str, Any]] = []

    for entry in collections:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        if name.startswith("_"):
            continue
        fields = observed_field_names(entry)
        is_edge = entry.get("type") == "edge"

        vec_fields = _has_vector_index(entry)
        if vec_fields:
            vector_indexes.append({"collection": name, "fields": vec_fields})

        if is_edge:
            role = _edge_role(name, fields)
            if role == "similarity":
                similarity_edges.append(name)
            elif role == "mention":
                mention_edges.append(name)
        else:
            if _is_chunk_collection(name, fields):
                chunk_collections.append(name)
            elif _is_entity_collection(name, fields):
                entity_collections.append(name)

    categories = sum(
        1
        for present in (
            bool(chunk_collections),
            bool(entity_collections),
            bool(similarity_edges),
            bool(mention_edges),
        )
        if present
    )
    # Vector indexes reinforce but never solely establish the pattern.
    is_graphrag = categories >= 2

    if not is_graphrag:
        return {"status": "ok", "isGraphRag": False}

    confidence = _confidence(chunk_collections, entity_collections, similarity_edges, mention_edges, vector_indexes)
    _annotate_mappings(data, set(chunk_collections), set(entity_collections), set(similarity_edges), set(mention_edges))

    evidence: list[str] = []
    if chunk_collections:
        evidence.append(f"{len(chunk_collections)} chunk collection(s)")
    if entity_collections:
        evidence.append(f"{len(entity_collections)} entity collection(s)")
    if similarity_edges:
        evidence.append(f"{len(similarity_edges)} similarity edge collection(s)")
    if mention_edges:
        evidence.append(f"{len(mention_edges)} mention edge collection(s)")
    if vector_indexes:
        evidence.append(f"{len(vector_indexes)} vector index(es)")

    return {
        "status": "ok",
        "isGraphRag": True,
        "confidence": confidence,
        "chunkCollections": sorted(chunk_collections),
        "entityCollections": sorted(entity_collections),
        "similarityEdges": sorted(similarity_edges),
        "mentionEdges": sorted(mention_edges),
        "vectorIndexes": sorted(vector_indexes, key=lambda v: str(v.get("collection"))),
        "evidence": evidence,
    }


def _confidence(
    chunks: list[str],
    entities: list[str],
    similarity: list[str],
    mention: list[str],
    vectors: list[dict[str, Any]],
) -> str:
    has_chunks = bool(chunks)
    has_entities = bool(entities)
    has_edges = bool(similarity or mention)
    if has_chunks and has_entities and has_edges:
        return "high"
    if has_chunks and (vectors or has_edges):
        return "high" if vectors and has_edges else "medium"
    return "medium"


def _annotate_mappings(
    data: dict[str, Any],
    chunk_names: set[str],
    entity_names: set[str],
    similarity_names: set[str],
    mention_names: set[str],
) -> None:
    pm = data.get("physicalMapping")
    if not isinstance(pm, dict):
        return
    entities = pm.get("entities")
    if isinstance(entities, dict):
        for mapping in entities.values():
            if not isinstance(mapping, dict):
                continue
            col = mapping.get("collectionName")
            if col in chunk_names:
                mapping["graphRagRole"] = "chunk"
            elif col in entity_names:
                mapping["graphRagRole"] = "entity"
    relationships = pm.get("relationships")
    if isinstance(relationships, dict):
        for mapping in relationships.values():
            if not isinstance(mapping, dict):
                continue
            col = mapping.get("edgeCollectionName")
            if col in similarity_names:
                mapping["graphRagRole"] = "similarity"
            elif col in mention_names:
                mapping["graphRagRole"] = "mention"
