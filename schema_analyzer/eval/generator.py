from __future__ import annotations

import contextlib
import random
import string
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from ..defaults import DEFAULT_EVAL_SCALE, DEFAULT_EVAL_SEED

if TYPE_CHECKING:
    from arango.database import StandardDatabase
    from arango.graph import Graph

EntityStyle = Literal["COLLECTION", "GENERIC_WITH_TYPE"]
RelStyle = Literal["DEDICATED_COLLECTION", "GENERIC_WITH_TYPE"]


@dataclass(frozen=True)
class PhysicalVariant:
    """
    A physical-schema variation for a given domain spec.

    - entity_style:
        - COLLECTION: one document collection per entity type
        - GENERIC_WITH_TYPE: single 'entities' collection with type field
    - rel_style:
        - DEDICATED_COLLECTION: one edge collection per relationship type
        - GENERIC_WITH_TYPE: single 'relationships' edge collection with relation type field
    """

    name: str
    entity_style: EntityStyle
    rel_style: RelStyle
    entity_collection_prefix: str = ""
    rel_collection_prefix: str = ""
    entity_generic_collection: str = "entities"
    rel_generic_collection: str = "relationships"
    entity_type_field: str = "type"
    rel_type_field: str = "relation"


def entity_collection_name(prefix: str, entity: str) -> str:
    """The per-entity document collection a ``COLLECTION``-style variant creates (``User`` -> ``users``).

    ``eval/scoring.py`` builds the expected mapping from this, so the two stay in step.
    """
    return f"{prefix}{entity.lower()}s"


def relationship_collection_name(prefix: str, rel_type: str) -> str:
    """The per-type edge collection a ``DEDICATED_COLLECTION``-style variant creates."""
    return f"{prefix}{rel_type.lower()}"


def _rand_id(prefix: str, n: int = 10) -> str:
    return prefix + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def _ensure_collection(db: StandardDatabase, name: str, *, edge: bool) -> Any:
    if db.has_collection(name):
        return db.collection(name)
    return db.create_collection(name, edge=edge)


def _ensure_persistent_index(col, fields: list[str]) -> None:
    with contextlib.suppress(Exception):
        col.add_index({"type": "persistent", "fields": fields, "unique": False, "sparse": False})


def _insert_many(col, docs: list[dict[str, Any]]) -> None:
    if not docs:
        return
    try:
        col.insert_many(docs, silent=True)
    except Exception:
        # fallback: insert one by one
        for d in docs:
            try:
                col.insert(d, silent=True)
            except Exception:
                continue


def materialize_domain_variant(
    db: StandardDatabase,
    domain_spec: dict[str, Any],
    variant: PhysicalVariant,
    *,
    seed: int = DEFAULT_EVAL_SEED,
    scale: int = DEFAULT_EVAL_SCALE,
    create_graph: bool = True,
) -> dict[str, Any]:
    """
    Create collections, indexes, a named graph (best-effort), and seed sample documents/edges.

    Returns a dict describing what was created (collection names, graph name, and the ground-truth conceptual model).
    """
    random.seed(seed)
    entities = list(domain_spec.get("entities") or [])
    rels = list(domain_spec.get("relationships") or [])

    created: dict[str, Any] = {
        "domain": domain_spec.get("domain"),
        "variant": variant.name,
        "entity_style": variant.entity_style,
        "rel_style": variant.rel_style,
        "collections": {"documents": [], "edges": []},
        "graph_name": None,
        "ground_truth": domain_spec,
    }

    # --- Entity collections
    entity_collections: dict[str, str] = {}
    if variant.entity_style == "GENERIC_WITH_TYPE":
        c_name = variant.entity_generic_collection
        _ensure_collection(db, c_name, edge=False)
        created["collections"]["documents"].append(c_name)
        entity_collections = {e["name"]: c_name for e in entities if isinstance(e, dict) and "name" in e}
        # index type field
        col = db.collection(c_name)
        _ensure_persistent_index(col, [variant.entity_type_field])
    else:
        for e in entities:
            if not isinstance(e, dict) or "name" not in e:
                continue
            c_name = entity_collection_name(variant.entity_collection_prefix, e["name"])
            _ensure_collection(db, c_name, edge=False)
            created["collections"]["documents"].append(c_name)
            entity_collections[e["name"]] = c_name

    # --- Relationship collections
    rel_collections: dict[str, str] = {}
    if variant.rel_style == "GENERIC_WITH_TYPE":
        c_name = variant.rel_generic_collection
        _ensure_collection(db, c_name, edge=True)
        created["collections"]["edges"].append(c_name)
        rel_collections = {r["type"]: c_name for r in rels if isinstance(r, dict) and "type" in r}
        col = db.collection(c_name)
        _ensure_persistent_index(col, [variant.rel_type_field])
        _ensure_persistent_index(col, ["_from"])
        _ensure_persistent_index(col, ["_to"])
    else:
        for r in rels:
            if not isinstance(r, dict) or "type" not in r:
                continue
            c_name = relationship_collection_name(variant.rel_collection_prefix, r["type"])
            _ensure_collection(db, c_name, edge=True)
            created["collections"]["edges"].append(c_name)
            rel_collections[r["type"]] = c_name

    # --- Seed documents
    # Create a small set of entity instances per entity type.
    entity_ids: dict[str, list[str]] = {}
    for e in entities:
        if not isinstance(e, dict) or "name" not in e:
            continue
        et = e["name"]
        c_name = entity_collections[et]
        col = db.collection(c_name)
        docs: list[dict[str, Any]] = []
        ids: list[str] = []
        for i in range(max(1, scale)):
            key = _rand_id(et[:2].lower(), 12)
            doc = {"_key": key}
            if variant.entity_style == "GENERIC_WITH_TYPE":
                doc[variant.entity_type_field] = et
            # include a couple properties if present
            raw_props = e.get("properties")
            props = raw_props if isinstance(raw_props, list) else []
            for p in props[:2]:
                doc[p] = f"{p}_{i}"
            docs.append(doc)
            ids.append(f"{c_name}/{key}")
        _insert_many(col, docs)
        entity_ids[et] = ids

    # --- Seed edges
    # Connect random endpoints based on relationship definitions.
    for r in rels:
        if not isinstance(r, dict) or "type" not in r:
            continue
        rt = r["type"]
        from_t = r.get("from")
        to_t = r.get("to")
        if from_t not in entity_ids or to_t not in entity_ids:
            continue
        e_col_name = rel_collections[rt]
        e_col = db.collection(e_col_name)
        edges: list[dict[str, Any]] = []
        for i in range(max(1, scale * 2)):
            _from = random.choice(entity_ids[from_t])
            _to = random.choice(entity_ids[to_t])
            edge = {"_from": _from, "_to": _to}
            if variant.rel_style == "GENERIC_WITH_TYPE":
                edge[variant.rel_type_field] = rt
            # add one rel property if present
            raw_props = r.get("properties")
            props = raw_props if isinstance(raw_props, list) else []
            if props:
                edge[props[0]] = f"{props[0]}_{i}"
            edges.append(edge)
        _insert_many(e_col, edges)

    # --- Graph (best-effort)
    if create_graph:
        graph_name = f"{domain_spec.get('domain', 'domain')}_{variant.name}"
        try:
            if not db.has_graph(graph_name):
                graph = cast("Graph", db.create_graph(graph_name))
                # If dedicated edge collections, create edge definitions per rel type.
                if variant.rel_style == "DEDICATED_COLLECTION":
                    for r in rels:
                        if not isinstance(r, dict):
                            continue
                        rt = r.get("type")
                        frm = r.get("from")
                        to = r.get("to")
                        if not (rt and frm and to):
                            continue
                        edge_col = rel_collections[rt]
                        from_cols = [entity_collections[frm]]
                        to_cols = [entity_collections[to]]
                        graph.create_edge_definition(edge_col, from_cols, to_cols)
                else:
                    # One generic edge definition (relationships edges connect entity vertex collections)
                    # In generic entity style, there is one vertex collection; otherwise many.
                    edge_col = variant.rel_generic_collection
                    vertex_cols = sorted(set(entity_collections.values()))
                    graph.create_edge_definition(edge_col, vertex_cols, vertex_cols)
            created["graph_name"] = graph_name
        except Exception:
            created["graph_name"] = None

    return created
