from __future__ import annotations

from typing import Any

from .conceptual import ConceptualSchema
from .defaults import (
    FULL_LABEL_SET_HARD_CAP,
    MAX_TYPE_FIELD_DISTINCT_VALUES,
    SAMPLE_VALUE_TOP_K,
    UNRESOLVED_ENDPOINT,
)
from .mapping import PhysicalMapping
from .type_detection import (
    _pick_best_type_field,
    _type_values_for_field,
    infer_entity_type_from_collection_name,
)
from .utils import iter_edge_definitions, pascal_case


def _choose_type_field(col: dict[str, Any], *, is_edge: bool, max_distinct_values: int | None = None) -> str | None:
    return _pick_best_type_field(col, is_edge=is_edge, max_distinct_values=max_distinct_values)


def _snapshot_max_distinct(snapshot: dict[str, Any]) -> int:
    """Discriminator acceptance bound for this snapshot.

    Scales with the snapshot's ``sample_value_top_k`` so a caller who raised
    the value-sampling cap (``max_entity_types``) doesn't get the field
    rejected for exceeding the default 32-distinct-value bound. A full-label-set
    snapshot (``min_type_value_count`` > 0) accepts the high-cardinality
    discriminator outright so every above-floor label maps.
    """
    floor = snapshot.get("min_type_value_count")
    if isinstance(floor, int) and floor > 0:
        return FULL_LABEL_SET_HARD_CAP
    top_k = snapshot.get("sample_value_top_k")
    if not isinstance(top_k, int) or top_k <= 0:
        top_k = SAMPLE_VALUE_TOP_K
    return max(MAX_TYPE_FIELD_DISTINCT_VALUES, top_k)


def type_value_caps_from_snapshot(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Report discriminator values dropped by the snapshot's top-K sampling.

    For every collection whose chosen type-discriminator field has more
    distinct values than the snapshot captured, emit a cap record naming the
    dropped classes (from ``sample_field_value_overflow``) and their counts —
    so a downstream consumer sees *why* a perfectly-spelled label is absent
    from the mapping instead of a bare ``MAPPING_NOT_FOUND``.

    Returns ``(entity_caps, relationship_caps)`` — document collections feed
    the first list, edge collections the second. Both empty when nothing was
    dropped (the common case).
    """
    entity_caps: list[dict[str, Any]] = []
    relationship_caps: list[dict[str, Any]] = []
    collections = snapshot.get("collections") or []
    if not isinstance(collections, list):
        return entity_caps, relationship_caps
    max_distinct = _snapshot_max_distinct(snapshot)

    for col in collections:
        if not isinstance(col, dict):
            continue
        is_edge = col.get("type") == "edge"
        type_field = _choose_type_field(col, is_edge=is_edge, max_distinct_values=max_distinct)
        if not type_field:
            continue
        captured = _iter_type_values(col, type_field)
        distinct = (col.get("sample_field_distinct_counts") or {}).get(type_field)
        if not isinstance(distinct, int) or distinct <= len(captured):
            continue
        overflow = (col.get("sample_field_value_overflow") or {}).get(type_field)
        dropped_values = [
            {"value": str(it["value"]), "count": it.get("count")}
            for it in (overflow if isinstance(overflow, list) else [])
            if isinstance(it, dict) and "value" in it
        ]
        cap: dict[str, Any] = {
            "collectionName": col.get("name"),
            "typeField": type_field,
            "distinctValues": distinct,
            "exported": len(captured),
            "dropped": distinct - len(captured),
            "droppedValues": dropped_values,
        }
        if distinct - len(captured) > len(dropped_values):
            cap["droppedValuesTruncated"] = True
        (relationship_caps if is_edge else entity_caps).append(cap)

    return entity_caps, relationship_caps


def _iter_type_values(col: dict[str, Any], field: str) -> list[str]:
    return _type_values_for_field(col, field)


def _resolve_endpoints(
    col: dict[str, Any],
    rel_type: str,
    *,
    is_generic: bool,
    collection_to_entities: dict[str, list[str]],
) -> tuple[str, str]:
    """
    Resolve fromEntity / toEntity for a relationship type.

    Resolution chain for generic edges:
    1. entity_types_by_relation (LPG — per-relation entity type via DOCUMENT())
    2. collections_by_relation (hybrid — per-relation from/to collections)
    3. from_collections / to_collections (union across all relation types)
    4. UNRESOLVED_ENDPOINT ("Any")
    """
    endpoints = col.get("edge_endpoints") or {}

    if is_generic:
        # Strategy 1: LPG entity type resolution
        by_rel = endpoints.get("entity_types_by_relation") or {}
        rel_ep = by_rel.get(rel_type)
        if rel_ep:
            from_types = rel_ep.get("from_entity_types") or []
            to_types = rel_ep.get("to_entity_types") or []
            from_ent = pascal_case(from_types[0]) if len(from_types) == 1 else UNRESOLVED_ENDPOINT
            to_ent = pascal_case(to_types[0]) if len(to_types) == 1 else UNRESOLVED_ENDPOINT
            return from_ent, to_ent

        # Strategy 2: Hybrid per-relation collection resolution
        cols_by_rel = endpoints.get("collections_by_relation") or {}
        rel_cols = cols_by_rel.get(rel_type)
        if rel_cols:
            from_cols = rel_cols.get("from_collections") or []
            to_cols = rel_cols.get("to_collections") or []
            from_ent = _resolve_single_entity(from_cols, collection_to_entities)
            to_ent = _resolve_single_entity(to_cols, collection_to_entities)
            return from_ent, to_ent

        # Strategy 3: Union-level fallback
        from_cols = endpoints.get("from_collections") or []
        to_cols = endpoints.get("to_collections") or []
        from_ent = _resolve_single_entity(from_cols, collection_to_entities)
        to_ent = _resolve_single_entity(to_cols, collection_to_entities)
        return from_ent, to_ent

    # Dedicated edge collection
    from_cols = endpoints.get("from_collections") or []
    to_cols = endpoints.get("to_collections") or []
    from_ent = _resolve_single_entity(from_cols, collection_to_entities)
    to_ent = _resolve_single_entity(to_cols, collection_to_entities)
    return from_ent, to_ent


def _resolve_single_entity(cols: list[str], collection_to_entities: dict[str, list[str]]) -> str:
    """Map a list of collections to a single entity type if unambiguous."""
    if not cols:
        return UNRESOLVED_ENDPOINT
    entity_types: set[str] = set()
    for c in cols:
        ents = collection_to_entities.get(c)
        if ents:
            entity_types.update(ents)
        else:
            entity_types.add(infer_entity_type_from_collection_name(c))
    if len(entity_types) == 1:
        return next(iter(entity_types))
    return UNRESOLVED_ENDPOINT


def _build_index_lookup(col: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Build a field_name → index info lookup from the snapshot's index list.

    For compound indexes, only the first field gets the "indexed" flag since
    ArangoDB persistent indexes are prefix-usable.  All participating fields
    get listed in the compound entry.

    Vertex-centric indexes (VCI) are excluded: they are edge-traversal
    indexes keyed on ``_from``/``_to`` plus an edge property, not
    property-scan indexes, so they must not flip ``indexed=True`` on a
    conceptual property.
    """
    lookup: dict[str, dict[str, Any]] = {}
    for idx in col.get("indexes") or []:
        if not isinstance(idx, dict):
            continue
        idx_type = idx.get("type", "")
        if idx_type == "primary":
            continue
        if idx.get("vci") or idx_type in _VCI_INDEX_TYPES:
            continue
        fields = idx.get("fields")
        if not isinstance(fields, list) or not fields:
            continue
        is_unique = bool(idx.get("unique"))
        is_sparse = bool(idx.get("sparse"))
        for i, f in enumerate(fields):
            if not isinstance(f, str):
                continue
            entry: dict[str, Any] = {
                "indexType": idx_type,
                "unique": is_unique,
                "sparse": is_sparse,
            }
            if len(fields) > 1:
                entry["compound"] = [str(x) for x in fields]
                entry["positionInCompound"] = i
            if f not in lookup or is_unique and not lookup[f].get("unique"):
                lookup[f] = entry
    return lookup


def _extract_properties(
    col: dict[str, Any],
    type_value: str | None = None,
) -> list[dict[str, Any]]:
    """Extract property list from observed_fields in the snapshot."""
    observed = col.get("observed_fields") or {}
    fields: list[str] = []

    if type_value and isinstance(observed.get("by_type"), dict):
        fields = observed["by_type"].get(type_value, [])
    elif isinstance(observed.get("fields"), list):
        fields = observed["fields"]
    elif isinstance(observed.get("by_type"), dict):
        # collection_per_entity over a collection the snapshot profiled by
        # discriminator: the entity IS the whole collection, so its property
        # set is the union of every type bucket (sorted for determinism).
        # Without this, a heterogeneous collection yields an entity with NO
        # properties — Document lost accountId and broke the join contract.
        # The discriminator column itself (factored out of the buckets, e.g.
        # Document.source) is an observed field too and must survive.
        union: set[str] = set()
        for bucket in observed["by_type"].values():
            if isinstance(bucket, list):
                union.update(f for f in bucket if isinstance(f, str))
        candidates = col.get("candidate_type_fields")
        if isinstance(candidates, list):
            union.update(f for f in candidates if isinstance(f, str))
        fields = sorted(union)

    if not isinstance(fields, list):
        return []

    index_lookup = _build_index_lookup(col)

    props: list[dict[str, Any]] = []
    for f in fields:
        if not isinstance(f, str) or not f:
            continue
        prop: dict[str, Any] = {"name": f, "type": "string"}
        idx_info = index_lookup.get(f)
        if idx_info:
            prop["indexed"] = True
            if idx_info.get("unique"):
                prop["unique"] = True
        props.append(prop)
    return props


_VCI_INDEX_TYPES: frozenset[str] = frozenset({"vci", "vertex_centric_index"})


def _extract_indexes_for_mapping(col: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract the list of non-primary indexes from a collection entry for
    inclusion in the physical mapping.

    In addition to the core shape (type, fields, unique, sparse, name) the
    exporter propagates three flags that downstream transpilers need but
    that the raw python-arango response carries unobtrusively:

      - ``vci`` — vertex-centric index marker. Surfaced when either the
        ``vci`` boolean is truthy on the index dict or the index type is
        one of the known VCI aliases. Edge traversal planners need this to
        emit correct ``OPTIONS.indexHint`` clauses in AQL ``FOR ... IN
        OUTBOUND/INBOUND`` statements.

      - ``deduplicate`` — emitted only when explicitly ``False`` on an
        array index. The default is ``True`` on ArangoDB array indexes so
        the absence of the key signals the default; explicit ``False`` is
        the signal to wrap a scan in ``DISTINCT`` to avoid duplicate rows.

      - ``storedValues`` — when present and non-empty, lets projection
        planners read columns from the index without a document fetch.
    """
    result: list[dict[str, Any]] = []
    for idx in col.get("indexes") or []:
        if not isinstance(idx, dict):
            continue
        idx_type = idx.get("type", "")
        if idx_type == "primary":
            continue
        entry: dict[str, Any] = {
            "type": idx_type,
            "fields": [str(f) for f in idx.get("fields", []) if isinstance(f, str)],
        }
        if idx.get("unique"):
            entry["unique"] = True
        if idx.get("sparse"):
            entry["sparse"] = True
        name = idx.get("name")
        if isinstance(name, str) and name:
            entry["name"] = name
        if idx.get("vci") or idx_type in _VCI_INDEX_TYPES:
            entry["vci"] = True
        if idx.get("deduplicate") is False:
            entry["deduplicate"] = False
        stored_values = idx.get("storedValues")
        if isinstance(stored_values, list) and stored_values:
            filtered = [str(f) for f in stored_values if isinstance(f, str) and f]
            if filtered:
                entry["storedValues"] = filtered
        result.append(entry)
    return result


def _build_property_mapping(props: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    Build a conceptual-property-name → physical-field-info mapping.

    Since the baseline uses field names directly as property names, the
    mapping is identity, but it records physical field name, index status,
    and uniqueness so downstream consumers (transpilers, query planners)
    can use it.
    """
    mapping: dict[str, dict[str, Any]] = {}
    for p in props:
        name = p.get("name")
        if not isinstance(name, str) or not name:
            continue
        entry: dict[str, Any] = {"field": name}
        if p.get("indexed"):
            entry["indexed"] = True
        if p.get("unique"):
            entry["unique"] = True
        mapping[name] = entry
    return mapping


def infer_baseline_from_snapshot(
    snapshot: dict[str, Any], *, collection_per_entity: bool = False, multi_label: bool = False
) -> dict[str, Any]:
    """
    Deterministic baseline inference from a physical schema snapshot.

    Detects LPG vs PG physical model style, extracts the correct ontology
    (entities + relationships), resolves domain/range from edge endpoints,
    and populates properties from observed fields.

    When ``collection_per_entity`` is True, type-discriminator inference is
    skipped entirely: every document collection maps to exactly one entity
    (``COLLECTION`` style) and every edge collection to one relationship
    (``DEDICATED_COLLECTION`` style). This is the correct model for a pure
    property-graph schema (one collection per type) where the collections
    already *are* the types, so attribute fields (e.g. ``region``) must not be
    mistaken for type tags. Default (False) preserves the auto LPG/PG heuristic.

    When ``multi_label`` is True, a collection whose type field holds label arrays
    (nodes with more than one label were sampled) maps each primary label to a
    ``LABELS``-style entity — ``{"style": "LABELS", "collectionName", "labelsField",
    "typeValue"}``, matched as ``typeValue IN doc[labelsField]`` — instead of
    ``LABEL``, so a node carrying several labels matches each of them. Conceptual
    output (aliases, subtype proposals, facets) is unchanged. Default (False)
    preserves today's ``LABEL`` output.

    ``LABEL``-style entity mappings whose PascalCase name is lossy relative to
    the raw discriminator value (``FIN_METRIC`` → ``FINMETRIC``) carry the raw
    value in ``aliases`` so downstream consumers can resolve the label that
    actually appears in the data. The returned dict also includes
    ``entityTypeCaps`` / ``relationshipTypeCaps`` (see
    :func:`type_value_caps_from_snapshot`) reporting discriminator values the
    snapshot's top-K sampling dropped.
    """
    cs = ConceptualSchema.empty()
    # Secondary labels found on only some nodes of a primary label (issue #28): subtype
    # candidates, proposed as rdfs:subClassOf the primary's entity.
    label_subclass_proposals: list[dict[str, Any]] = []
    pm = PhysicalMapping.empty()
    detected_patterns: list[str] = []
    max_distinct = _snapshot_max_distinct(snapshot)

    collections = snapshot.get("collections") or []
    if not isinstance(collections, list):
        collections = []

    # collection_name → [entity_type, ...] for endpoint resolution
    collection_to_entities: dict[str, list[str]] = {}

    # ── Entities ────────────────────────────────────────────────────────
    for col in collections:
        if not isinstance(col, dict):
            continue
        if col.get("type") != "document":
            continue

        collection_name = col.get("name")
        if not isinstance(collection_name, str) or not collection_name:
            continue

        col_indexes = _extract_indexes_for_mapping(col)

        type_field = (
            None if collection_per_entity else _choose_type_field(col, is_edge=False, max_distinct_values=max_distinct)
        )
        if type_field:
            if "LPG_LABEL" not in detected_patterns:
                detected_patterns.append("LPG_LABEL")
            cooccurrence = (col.get("label_cooccurrence") or {}).get(type_field) or {}
            # A secondary label riding on two or more *different* primary labels cuts across
            # the types — a provenance tag (``SourceCRM``), tenant, or status — not a subtype
            # of any one of them. Only a label nested inside a single primary is a subtype.
            primaries_by_label: dict[str, int] = {}
            for labels_on_primary in cooccurrence.values():
                for label in labels_on_primary:
                    primaries_by_label[label] = primaries_by_label.get(label, 0) + 1
            facet_labels = {label for label, n in primaries_by_label.items() if n >= 2}
            primary_totals = {
                str(it.get("value")): int(it.get("count") or 0)
                for it in (col.get("sample_field_value_counts") or {}).get(type_field) or []
                if isinstance(it, dict)
            }
            for raw in _iter_type_values(col, type_field):
                ent_name = pascal_case(raw)
                props = _extract_properties(col, type_value=raw)
                entity: dict[str, Any] = {"name": ent_name, "labels": [ent_name], "properties": props}
                if cooccurrence.get(raw):
                    _apply_secondary_labels(
                        entity,
                        cooccurrence[raw],
                        primary_totals.get(raw, 0),
                        label_subclass_proposals,
                        facet_labels=facet_labels,
                    )
                cs.entities.append(entity)
                ent_mapping: dict[str, Any] = {
                    "style": "LABEL",
                    "collectionName": collection_name,
                    "typeField": type_field,
                    "typeValue": raw,
                }
                # shortcut: label arrays are recognised by sampled multi-label nodes (label_cooccurrence);
                # a collection whose arrays all hold one label stays LABEL, upgrade if that matters.
                if multi_label and cooccurrence:
                    ent_mapping = {
                        "style": "LABELS",
                        "collectionName": collection_name,
                        "labelsField": type_field,
                        "typeValue": raw,
                    }
                # PascalCase collapses separators (FIN_METRIC → FINMETRIC),
                # so the raw discriminator value — the label a Cypher author
                # actually sees in the data — is kept as an accepted alias.
                if raw != ent_name:
                    ent_mapping["aliases"] = [raw]
                if col_indexes:
                    ent_mapping["indexes"] = col_indexes
                prop_map = _build_property_mapping(props)
                if prop_map:
                    ent_mapping["properties"] = prop_map
                pm.entities[ent_name] = ent_mapping
                collection_to_entities.setdefault(collection_name, []).append(ent_name)
        else:
            if "PG_ENTITY_COLLECTION" not in detected_patterns:
                detected_patterns.append("PG_ENTITY_COLLECTION")
            ent_name = col.get("inferred_entity_type") or pascal_case(collection_name)
            props = _extract_properties(col)
            cs.entities.append({"name": ent_name, "labels": [ent_name], "properties": props})
            ent_mapping = {"style": "COLLECTION", "collectionName": collection_name}
            if col_indexes:
                ent_mapping["indexes"] = col_indexes
            prop_map = _build_property_mapping(props)
            if prop_map:
                ent_mapping["properties"] = prop_map
            pm.entities[ent_name] = ent_mapping
            collection_to_entities.setdefault(collection_name, []).append(ent_name)

    # ── Relationships ───────────────────────────────────────────────────
    # Also use graph definitions for PG endpoint resolution
    _enrich_endpoints_from_graphs(collections, snapshot, collection_to_entities)

    for col in collections:
        if not isinstance(col, dict):
            continue
        if col.get("type") != "edge":
            continue

        collection_name = col.get("name")
        if not isinstance(collection_name, str) or not collection_name:
            continue

        edge_indexes = _extract_indexes_for_mapping(col)

        type_field = (
            None if collection_per_entity else _choose_type_field(col, is_edge=True, max_distinct_values=max_distinct)
        )
        if type_field:
            if "LPG_GENERIC_EDGE" not in detected_patterns:
                detected_patterns.append("LPG_GENERIC_EDGE")
            for raw in _iter_type_values(col, type_field):
                rel_type = str(raw).strip()
                if not rel_type:
                    continue
                from_ent, to_ent = _resolve_endpoints(
                    col,
                    rel_type,
                    is_generic=True,
                    collection_to_entities=collection_to_entities,
                )
                props = _extract_properties(col, type_value=raw)
                cs.relationships.append(
                    {
                        "type": rel_type,
                        "fromEntity": from_ent,
                        "toEntity": to_ent,
                        "properties": props,
                    }
                )
                rel_mapping: dict[str, Any] = {
                    "style": "GENERIC_WITH_TYPE",
                    "edgeCollectionName": collection_name,
                    "typeField": type_field,
                    "typeValue": raw,
                }
                if edge_indexes:
                    rel_mapping["indexes"] = edge_indexes
                prop_map = _build_property_mapping(props)
                if prop_map:
                    rel_mapping["properties"] = prop_map
                pm.relationships[rel_type] = rel_mapping
        else:
            if "PG_DEDICATED_EDGE" not in detected_patterns:
                detected_patterns.append("PG_DEDICATED_EDGE")
            rel_type = col.get("inferred_relationship_type") or collection_name.upper()
            from_ent, to_ent = _resolve_endpoints(
                col,
                rel_type,
                is_generic=False,
                collection_to_entities=collection_to_entities,
            )
            props = _extract_properties(col)
            cs.relationships.append(
                {
                    "type": rel_type,
                    "fromEntity": from_ent,
                    "toEntity": to_ent,
                    "properties": props,
                }
            )
            rel_mapping = {"style": "DEDICATED_COLLECTION", "edgeCollectionName": collection_name}
            if edge_indexes:
                rel_mapping["indexes"] = edge_indexes
            prop_map = _build_property_mapping(props)
            if prop_map:
                rel_mapping["properties"] = prop_map
            pm.relationships[rel_type] = rel_mapping

    entity_caps: list[dict[str, Any]] = []
    relationship_caps: list[dict[str, Any]] = []
    if not collection_per_entity:
        entity_caps, relationship_caps = type_value_caps_from_snapshot(snapshot)

    conceptual_json = cs.to_json()
    if label_subclass_proposals:
        entity_names = {e.get("name") for e in cs.entities}
        # A label that is itself some node's primary label already has an entity of its
        # own; proposing it as a subtype of another would assert a hierarchy the data does
        # not show, so only labels with no entity of their own are proposed.
        proposals = [pr for pr in label_subclass_proposals if pr["subClass"] not in entity_names]
        if proposals:
            conceptual_json["subClassOfProposals"] = proposals
    return {
        "conceptualSchema": conceptual_json,
        "physicalMapping": pm.to_json(),
        "detectedPatterns": detected_patterns,
        "entityTypeCaps": entity_caps,
        "relationshipTypeCaps": relationship_caps,
    }


def _apply_secondary_labels(
    entity: dict[str, Any],
    secondary: dict[str, int],
    total: int,
    proposals: list[dict[str, Any]],
    *,
    facet_labels: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Fold a primary label's co-occurring labels into its entity (issue #28).

    A label carried by *every* node of the primary is another name the node answers to
    (r2g's ``extra_labels``) and joins the entity's conceptual ``labels``, mirroring r2g's
    forward CSI so a round trip is lossless. A label on only *some* nodes is a subtype
    candidate: recorded on the entity under ``partialLabels`` with its share, and proposed
    as ``rdfs:subClassOf`` the entity — unless it is in ``facet_labels`` (it also rides on
    other primary labels), in which case it is a cross-cutting tag recorded under
    ``facetLabels`` and never proposed as a subtype. The physical ``typeValue`` stays the
    primary label.
    """
    partial: dict[str, dict[str, int]] = {}
    facets: dict[str, dict[str, int]] = {}
    for label, count in sorted(secondary.items()):
        name = pascal_case(label)
        if total and count >= total:
            if name not in entity["labels"]:
                entity["labels"].append(name)
            continue
        if label in facet_labels:
            facets[name] = {"count": count, "of": total}
            continue
        partial[name] = {"count": count, "of": total}
        proposals.append(
            {
                "subClass": name,
                "superClass": entity["name"],
                "method": "secondary_label",
                "evidence": f"label '{label}' on {count} of {total} '{entity['name']}' nodes",
                "source": "baseline",
            }
        )
    if partial:
        entity["partialLabels"] = partial
    if facets:
        entity["facetLabels"] = facets


def _enrich_endpoints_from_graphs(
    collections: list[dict[str, Any]],
    snapshot: dict[str, Any],
    collection_to_entities: dict[str, list[str]],
) -> None:
    """
    For PG-style edge collections without edge_endpoints data, fill in
    from/to collections from named graph edge_definitions.
    """
    graphs_detailed = snapshot.get("graphs_detailed") or []
    if not isinstance(graphs_detailed, list):
        return

    edge_def_map: dict[str, dict[str, list[str]]] = {}
    for g in graphs_detailed:
        if not isinstance(g, dict):
            continue
        for edef in iter_edge_definitions(g):
            ec = edef["collection"]
            edge_def_map[ec] = {
                "from_collections": edef.get("from") or [],
                "to_collections": edef.get("to") or [],
            }

    for col in collections:
        if not isinstance(col, dict) or col.get("type") != "edge":
            continue
        cname = col.get("name", "")
        endpoints = col.get("edge_endpoints")
        if endpoints and (endpoints.get("from_collections") or endpoints.get("to_collections")):
            continue
        if cname in edge_def_map:
            col.setdefault("edge_endpoints", {}).update(edge_def_map[cname])
