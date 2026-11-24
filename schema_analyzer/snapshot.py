from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from arango.exceptions import ArangoServerError

if TYPE_CHECKING:
    from arango.database import StandardDatabase


from ._arango import (
    aql_execute,
    collection_count,
    collection_indexes,
    collection_properties,
    database_graphs,
    graph_properties,
)
from .defaults import (
    FULL_LABEL_SET_HARD_CAP,
    MAX_TYPE_VALUE_LENGTH,
    SAMPLE_VALUE_OVERFLOW_K,
    SAMPLE_VALUE_TOP_K,
    SNAPSHOT_FORMAT_VERSION,
)
from .errors import SchemaAnalyzerError
from .list_values import list_fields_of
from .nested_fields import nested_depth, summarize_documents
from .type_detection import (
    EDGE_FROM_TYPE_FIELDS,
    EDGE_TO_TYPE_FIELDS,
    _detect_candidate_type_fields,
    _pick_best_type_field,
    _type_values_for_field,
    infer_entity_type_from_collection_name,
    infer_relationship_type_from_collection_name,
    type_field_max_distinct,
)
from .utils import SYSTEM_FIELDS, graph_collections, sha256_hex, stable_dumps

logger = logging.getLogger(__name__)

_PROPERTY_SAMPLE_LIMIT = 10


#: AQL for a type field's *primary* value. A labelled-property-graph node may carry its
#: labels as an array (r2g's LPG layout: ``labels: ["Account", "Customer"]``); grouping by
#: the whole array makes every value a list, which no discriminator check accepts, so the
#: collection collapsed to one entity (issue #28). The first label is the node's identity —
#: r2g builds the ``_key`` namespace and the CSI ``typeValue`` on it — so an array groups by
#: its first element. Scalar fields are unchanged.
_PRIMARY = "(IS_ARRAY({x}) ? FIRST({x}) : {x})"

#: One sampled document's attribute names, and which of them hold a list: ``scalars``
#: is false when the list holds an object or a list (``list_values``). Read by
#: :func:`_read_attribute_rows`.
_ATTRIBUTES_AND_LISTS = (
    "LET a = ATTRIBUTES(d) RETURN {fields: a, lists: (FOR k IN a FILTER IS_ARRAY(d[k]) "
    "RETURN {name: k, scalars: LENGTH(FOR x IN d[k] FILTER IS_OBJECT(x) OR IS_ARRAY(x) LIMIT 1 RETURN 1) == 0})}"
)


def _read_attribute_rows(rows: Iterable[Any], exclude: set[str]) -> tuple[set[str], set[str], set[str]]:
    """``(names, lists of scalars, lists holding structures)`` from :data:`_ATTRIBUTES_AND_LISTS` rows.

    A row that is a bare list of names (the shape before list detection) still reads.
    """
    keys: set[str] = set()
    scalars: set[str] = set()
    structures: set[str] = set()
    for row in rows:
        if isinstance(row, list):
            keys.update(k for k in row if isinstance(k, str) and k not in exclude)
            continue
        if not isinstance(row, dict):
            continue
        names = row.get("fields")
        if isinstance(names, list):
            keys.update(k for k in names if isinstance(k, str) and k not in exclude)
        for item in row.get("lists") or []:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or name in exclude:
                continue
            (scalars if item.get("scalars") else structures).add(name)
    return keys, scalars, structures


def _detect_label_cooccurrence(db: StandardDatabase, collection_name: str, field: str) -> dict[str, dict[str, int]]:
    """For an array-valued type field, count the secondary labels riding on each primary.

    Returns ``{primary: {label: count}}``; empty when the field is not array-valued. A label
    on *every* node of a primary is an alias of it; one on only some is a subtype candidate.
    """
    try:
        cursor = aql_execute(
            db,
            "FOR d IN @@c FILTER IS_ARRAY(d[@field]) AND LENGTH(d[@field]) > 1 "
            "LET p = FIRST(d[@field]) "
            "FOR l IN UNIQUE(SLICE(d[@field], 1)) "
            "FILTER IS_STRING(l) AND l != p "
            "COLLECT primary = p, label = l WITH COUNT INTO cnt "
            'RETURN {"primary": primary, "label": label, "count": cnt}',
            bind_vars={"@c": collection_name, "field": field},
        )
        out: dict[str, dict[str, int]] = {}
        for row in cursor:
            if isinstance(row, dict) and isinstance(row.get("primary"), str) and isinstance(row.get("label"), str):
                out.setdefault(row["primary"], {})[row["label"]] = int(row.get("count") or 0)
        return out
    except Exception as exc:
        logger.debug("label co-occurrence failed for %s.%s: %s", collection_name, field, exc)
        return {}


def _detect_type_fields_via_collect(
    db: StandardDatabase,
    collection_name: str,
    *,
    top_k: int = SAMPLE_VALUE_TOP_K,
    floor: int = 0,
    hard_cap: int = FULL_LABEL_SET_HARD_CAP,
) -> tuple[list[str], dict[str, list[dict[str, Any]]], dict[str, int], dict[str, list[dict[str, Any]]]]:
    """
    Detect type discriminator fields using AQL COLLECT for accurate counting.
    Unlike LIMIT-based sampling, this scans all documents and finds every distinct value.

    Returns ``(candidates, value_counts, distinct_counts, overflow)``:
    ``value_counts`` keeps the top ``top_k`` values per field (unchanged
    contract), ``distinct_counts`` records each field's TRUE non-null distinct
    total, and ``overflow`` keeps up to ``SAMPLE_VALUE_OVERFLOW_K`` values
    ranked past the top-K — so a dropped discriminator value (e.g. a class
    ranked 24th under a top-20 cap) is reportable instead of silently absent.

    ``floor`` > 0 switches to LPG full-label-set mode: ``value_counts`` then
    keeps EVERY value whose count is >= ``floor`` and whose text looks like a
    label (dropping sub-floor and free-text junk), unbounded by ``top_k`` and
    ceilinged by ``hard_cap``; ``overflow`` samples the genuine sub-floor drops.
    ``floor`` == 0 preserves the exact top-K behaviour.
    """
    try:
        cursor = aql_execute(
            db,
            "FOR d IN @@c LIMIT 1 RETURN d",
            bind_vars={"@c": collection_name},
        )
        samples = list(cursor)
    except ArangoServerError as exc:
        logger.warning("could not sample %s for discriminator detection: %s", collection_name, exc)
        return [], {}, {}, {}
    except Exception as exc:
        logger.warning("unexpected error sampling %s for discriminator detection: %s", collection_name, exc)
        return [], {}, {}, {}

    if not samples:
        return [], {}, {}, {}

    sample = samples[0] if isinstance(samples[0], dict) else {}
    candidates = _detect_candidate_type_fields(sample)
    if not candidates:
        return [], {}, {}, {}

    value_counts: dict[str, list[dict[str, Any]]] = {}
    distinct_counts: dict[str, int] = {}
    overflow: dict[str, list[dict[str, Any]]] = {}
    failures = 0
    for key in candidates:
        try:
            if floor and floor > 0:
                # Full-label-set mode: keep EVERY value clearing the floor that
                # looks like a label; report a sample of the sub-floor drops.
                cursor = aql_execute(
                    db,
                    "LET groups = ("
                    "FOR d IN @@c "
                    "COLLECT val = " + _PRIMARY.format(x="d[@field]") + " WITH COUNT INTO cnt "
                    "FILTER val != null "
                    "SORT cnt DESC "
                    "RETURN {value: val, count: cnt}"
                    ") "
                    "LET kept = ("
                    "FOR g IN groups "
                    "FILTER g.count >= @floor "
                    "FILTER IS_STRING(g.value) AND LENGTH(g.value) <= @maxLen "
                    "AND REGEX_TEST(g.value, @labelRe) AND REGEX_TEST(g.value, @letterRe) "
                    "LIMIT @hardCap RETURN g"
                    ") "
                    # ``distinct`` is an AQL reserved word — quote reserved-word
                    # object keys or ArangoDB rejects the query (ERR 1501).
                    'RETURN {"distinct": LENGTH(groups), "top": kept, '
                    '"overflow": SLICE('
                    "(FOR g IN groups FILTER g.count < @floor RETURN g), 0, @overflowLen)}",
                    bind_vars={
                        "@c": collection_name,
                        "field": key,
                        "floor": floor,
                        "maxLen": MAX_TYPE_VALUE_LENGTH,
                        "labelRe": "^[A-Za-z0-9_-]+$",
                        "letterRe": "[A-Za-z]",
                        "hardCap": hard_cap,
                        "overflowLen": SAMPLE_VALUE_OVERFLOW_K,
                    },
                )
            else:
                cursor = aql_execute(
                    db,
                    "LET groups = ("
                    "FOR d IN @@c "
                    "COLLECT val = " + _PRIMARY.format(x="d[@field]") + " WITH COUNT INTO cnt "
                    "FILTER val != null "
                    "SORT cnt DESC "
                    "RETURN {value: val, count: cnt}"
                    ") "
                    # ``distinct`` is an AQL reserved word — quote reserved-word
                    # object keys or ArangoDB rejects the query (ERR 1501).
                    'RETURN {"distinct": LENGTH(groups), '
                    '"top": SLICE(groups, 0, @top), '
                    '"overflow": SLICE(groups, @top, @overflowLen)}',
                    bind_vars={
                        "@c": collection_name,
                        "field": key,
                        "top": top_k,
                        "overflowLen": SAMPLE_VALUE_OVERFLOW_K,
                    },
                )
            rows = list(cursor)
            row = rows[0] if rows and isinstance(rows[0], dict) else {}
            items = row.get("top")
            if isinstance(items, list) and items:
                value_counts[key] = items
                if isinstance(row.get("distinct"), int):
                    distinct_counts[key] = row["distinct"]
                extra = row.get("overflow")
                if isinstance(extra, list) and extra:
                    overflow[key] = extra
        except ArangoServerError as exc:
            # A swallowed query error previously made an AQL reserved-word bug
            # (ERR 1501) indistinguishable from "this field has no values",
            # disabling LPG discriminator detection for a whole release. Catch the
            # server error we actually expect and log it at WARNING so degradation
            # is visible instead of silent.
            failures += 1
            logger.warning(
                "discriminator value query failed for %s.%s — detection degraded, "
                "treating field as having no values: %s",
                collection_name,
                key,
                exc,
            )
            continue
        except Exception as exc:
            # Resilience fallback (non-server / fake-DB / unexpected errors): never
            # let one field abort the whole snapshot — but log it, so it is not
            # silent either. This is the tier that keeps graceful degradation.
            failures += 1
            logger.warning(
                "unexpected error sampling discriminator %s.%s — treating field as having no values: %s",
                collection_name,
                key,
                exc,
            )
            continue

    if failures:
        logger.warning(
            "%d of %d candidate discriminator field(s) failed to sample in %s; type detection may be incomplete",
            failures,
            len(candidates),
            collection_name,
        )
    return candidates, value_counts, distinct_counts, overflow


def _detect_observed_fields(
    db: StandardDatabase,
    collection_name: str,
    *,
    type_field: str | None = None,
    type_values: list[str] | None = None,
    nested_field_depth: int = 0,
) -> dict[str, Any]:
    """
    Detect property fields for a collection by sampling document attributes.
    For type-discriminated collections, detects per type value.

    With ``nested_field_depth`` > 0 (issue #33) the same sample is read whole and
    walked into nested objects, adding ``nested_paths`` (or ``nested_paths_by_type``),
    ``nested_conflicts`` (dotted names held in two shapes, collection-wide) and
    ``nested_arrays_skipped``. Depth 0 keeps the ``ATTRIBUTES(d)`` pass and output.

    Either way, ``list_fields`` names the top-level attributes the sample holds as a
    list of scalars (:mod:`.list_values`), written only when there is one.
    """
    exclude = set(SYSTEM_FIELDS)
    if type_field:
        exclude.add(type_field)
        exclude.add("labels")

    if nested_field_depth > 0:
        return _detect_observed_fields_nested(
            db, collection_name, exclude, type_field=type_field, type_values=type_values, depth=nested_field_depth
        )

    # Lists are judged collection-wide, as nested conflicts are: one attribute is one
    # property mapping per entity, and reading a scalar as a one-element list is harmless.
    scalars: set[str] = set()
    structures: set[str] = set()
    out: dict[str, Any]
    if type_field and type_values:
        result: dict[str, list[str]] = {}
        for tv in type_values:
            try:
                cursor = aql_execute(
                    db,
                    "FOR d IN @@c FILTER "
                    + _PRIMARY.format(x="d[@field]")
                    + " == @val LIMIT @lim "
                    + _ATTRIBUTES_AND_LISTS,
                    bind_vars={
                        "@c": collection_name,
                        "field": type_field,
                        "val": tv,
                        "lim": _PROPERTY_SAMPLE_LIMIT,
                    },
                )
                keys, found_scalars, found_structures = _read_attribute_rows(cursor, exclude)
                result[str(tv)] = sorted(keys)
                scalars |= found_scalars
                structures |= found_structures
            except Exception:
                continue
        out = {"by_type": result}
    else:
        try:
            cursor = aql_execute(
                db,
                "FOR d IN @@c LIMIT @lim " + _ATTRIBUTES_AND_LISTS,
                bind_vars={"@c": collection_name, "lim": _PROPERTY_SAMPLE_LIMIT},
            )
            keys_all, scalars, structures = _read_attribute_rows(cursor, exclude)
            out = {"fields": sorted(keys_all)}
        except Exception:
            return {}
    lists = sorted(scalars - structures)
    if lists:
        out["list_fields"] = lists
    return out


def _detect_observed_fields_nested(
    db: StandardDatabase,
    collection_name: str,
    exclude: set[str],
    *,
    type_field: str | None,
    type_values: list[str] | None,
    depth: int,
) -> dict[str, Any]:
    """:func:`_detect_observed_fields` with a look into nested objects (same sample)."""
    all_docs: list[Any] = []
    if type_field and type_values:
        by_type: dict[str, list[str]] = {}
        paths_by_type: dict[str, list[list[str]]] = {}
        for tv in type_values:
            try:
                docs = list(
                    aql_execute(
                        db,
                        "FOR d IN @@c FILTER " + _PRIMARY.format(x="d[@field]") + " == @val LIMIT @lim RETURN d",
                        bind_vars={
                            "@c": collection_name,
                            "field": type_field,
                            "val": tv,
                            "lim": _PROPERTY_SAMPLE_LIMIT,
                        },
                    )
                )
            except Exception:
                continue
            bucket = summarize_documents(docs, depth, exclude=exclude)
            by_type[str(tv)] = bucket["fields"]
            if bucket["paths"]:
                paths_by_type[str(tv)] = bucket["paths"]
            all_docs.extend(docs)
        out: dict[str, Any] = {"by_type": by_type}
        if paths_by_type:
            out["nested_paths_by_type"] = paths_by_type
        # Conflicts and skipped lists are judged over the whole collection's sample: one
        # collection holding both shapes under a name is what a person has to settle.
        whole = summarize_documents(all_docs, depth, exclude=exclude)
    else:
        try:
            all_docs = list(
                aql_execute(
                    db,
                    "FOR d IN @@c LIMIT @lim RETURN d",
                    bind_vars={"@c": collection_name, "lim": _PROPERTY_SAMPLE_LIMIT},
                )
            )
        except Exception:
            return {}
        whole = summarize_documents(all_docs, depth, exclude=exclude)
        out = {"fields": whole["fields"]}
        if whole["paths"]:
            out["nested_paths"] = whole["paths"]
    lists = list_fields_of(all_docs, exclude=exclude)
    if lists:
        out["list_fields"] = lists
    if whole["conflicts"]:
        out["nested_conflicts"] = whole["conflicts"]
    if whole["arrays_skipped"]:
        out["nested_arrays_skipped"] = whole["arrays_skipped"]
    return out


def _detect_edge_endpoint_type_fields(
    db: StandardDatabase,
    edge_collection_name: str,
    *,
    sample: int = 50,
) -> tuple[str, str] | None:
    """Return ``(from_field, to_field)`` when sampled edges carry endpoint types directly.

    Both fields must be present on the sample (preference order in
    ``EDGE_FROM_TYPE_FIELDS`` / ``EDGE_TO_TYPE_FIELDS``); otherwise ``None`` and the
    caller falls back to ``DOCUMENT()`` or collection-name resolution.
    """
    try:
        cursor = aql_execute(
            db,
            "FOR e IN @@c LIMIT @n RETURN ATTRIBUTES(e, true)",
            bind_vars={"@c": edge_collection_name, "n": int(sample)},
        )
        present: set[str] = set()
        for attrs in cursor:
            if isinstance(attrs, list):
                present.update(str(a) for a in attrs)
    except Exception as exc:
        logger.debug("could not sample %s for endpoint type fields: %s", edge_collection_name, exc)
        return None
    from_field = next((f for f in EDGE_FROM_TYPE_FIELDS if f in present), None)
    to_field = next((f for f in EDGE_TO_TYPE_FIELDS if f in present), None)
    if from_field and to_field:
        return from_field, to_field
    return None


def _fold_by_relation(rows: Iterable[Any], from_key: str, to_key: str, kind: str) -> dict[str, dict[str, list[str]]]:
    """Fold ``{relType, <from_key>, <to_key>}`` rows into ``{rel: {from_<kind>, to_<kind>}}``.

    Rows without a relation type are skipped, as are empty endpoint values; both lists
    and the relations are sorted.
    """
    folded: dict[str, tuple[set[str], set[str]]] = {}
    for item in rows:
        if not isinstance(item, dict) or not item.get("relType"):
            continue
        froms, tos = folded.setdefault(str(item["relType"]), (set(), set()))
        if item.get(from_key):
            froms.add(str(item[from_key]))
        if item.get(to_key):
            tos.add(str(item[to_key]))
    return {rt: {f"from_{kind}": sorted(f), f"to_{kind}": sorted(t)} for rt, (f, t) in sorted(folded.items())}


def _detect_edge_endpoint_collections(db: StandardDatabase, edge_collection_name: str) -> dict[str, list[str]] | None:
    """The collections an edge collection's ``_from`` / ``_to`` point into, or
    ``None`` when the query fails."""
    try:
        cursor = aql_execute(
            db,
            "FOR e IN @@c "
            "COLLECT fromCol = PARSE_IDENTIFIER(e._from).collection, "
            "toCol = PARSE_IDENTIFIER(e._to).collection "
            "RETURN {fromCollection: fromCol, toCollection: toCol}",
            bind_vars={"@c": edge_collection_name},
        )
        all_from_cols: set[str] = set()
        all_to_cols: set[str] = set()
        for item in cursor:
            if isinstance(item, dict):
                fc = item.get("fromCollection")
                tc = item.get("toCollection")
                if fc:
                    all_from_cols.add(fc)
                if tc:
                    all_to_cols.add(tc)
    except Exception:
        return None
    return {"from_collections": sorted(all_from_cols), "to_collections": sorted(all_to_cols)}


def _detect_edge_endpoints(
    db: StandardDatabase,
    edge_collection_name: str,
    *,
    rel_type_field: str | None = None,
    doc_type_info: dict[str, tuple[str, set[str]]],
    collections: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """
    Detect _from/_to endpoint collections and resolve entity types.

    ``collections`` is :func:`_detect_edge_endpoint_collections`' result when the
    caller already has it; otherwise it is queried here. ``{}`` when that query fails.

    Three resolution strategies depending on what's available:
    1. LPG endpoints (doc collections have type discriminators): DOCUMENT()
       lookups resolve per-relation entity types.
    2. Hybrid/PG endpoints with generic edge collection: COLLECT by relation
       type + from/to collection, then map collection → entity type.
    3. No relation type field: just report from/to collections.
    """
    if collections is None:
        collections = _detect_edge_endpoint_collections(db, edge_collection_name)
        if collections is None:
            return {}
    all_from_cols = set(collections["from_collections"])
    all_to_cols = set(collections["to_collections"])
    result: dict[str, Any] = {
        "from_collections": sorted(all_from_cols),
        "to_collections": sorted(all_to_cols),
    }

    if not rel_type_field:
        return result

    # Strategy 0 (converged from arango-ontoextract, 2026-09-14): edges that carry
    # their endpoint types directly (``_fromType`` / ``_toType`` …) resolve per-relation
    # entity types with one COLLECT and no ``DOCUMENT()`` lookups — and they work even
    # when the vertex collections have no detectable discriminator of their own.
    endpoint_fields = _detect_edge_endpoint_type_fields(db, edge_collection_name)
    if endpoint_fields is not None:
        from_field, to_field = endpoint_fields
        try:
            cursor = aql_execute(
                db,
                "FOR e IN @@ec "
                "COLLECT relType = e[@relField], "
                "fromType = " + _PRIMARY.format(x="e[@fromField]") + ", "
                "toType = " + _PRIMARY.format(x="e[@toField]") + " "
                "RETURN {relType: relType, fromType: fromType, toType: toType}",
                bind_vars={
                    "@ec": edge_collection_name,
                    "relField": rel_type_field,
                    "fromField": from_field,
                    "toField": to_field,
                },
            )
            direct = _fold_by_relation(cursor, "fromType", "toType", "entity_types")
            if direct:
                result["entity_types_by_relation"] = direct
                result["endpoint_type_fields"] = {"from": from_field, "to": to_field}
                return result
        except Exception as exc:
            logger.debug("Endpoint-type-field resolution failed for %s: %s", edge_collection_name, exc)

    any_lpg_endpoint = any(c in doc_type_info for c in all_from_cols | all_to_cols)

    if any_lpg_endpoint:
        # Strategy 1: LPG — resolve entity types via DOCUMENT() lookups
        node_type_field = None
        for col_name in sorted(all_from_cols | all_to_cols):
            if col_name in doc_type_info:
                node_type_field = doc_type_info[col_name][0]
                break

        if node_type_field:
            try:
                cursor = aql_execute(
                    db,
                    "FOR e IN @@ec "
                    "LET fromDoc = DOCUMENT(e._from) "
                    "LET toDoc = DOCUMENT(e._to) "
                    "COLLECT relType = e[@relField], "
                    "fromType = " + _PRIMARY.format(x="fromDoc[@nodeTypeField]") + ", "
                    "toType = " + _PRIMARY.format(x="toDoc[@nodeTypeField]") + " "
                    "RETURN {relType: relType, fromType: fromType, toType: toType}",
                    bind_vars={
                        "@ec": edge_collection_name,
                        "relField": rel_type_field,
                        "nodeTypeField": node_type_field,
                    },
                )
                result["entity_types_by_relation"] = _fold_by_relation(cursor, "fromType", "toType", "entity_types")
                return result
            except Exception as exc:
                logger.debug("LPG endpoint resolution failed for %s: %s", edge_collection_name, exc)

    # Strategy 2: Hybrid/PG — resolve per-relation endpoints by collection
    # name (no DOCUMENT() needed, just PARSE_IDENTIFIER).
    try:
        cursor = aql_execute(
            db,
            "FOR e IN @@ec "
            "COLLECT relType = e[@relField], "
            "fromCol = PARSE_IDENTIFIER(e._from).collection, "
            "toCol = PARSE_IDENTIFIER(e._to).collection "
            "RETURN {relType: relType, fromCol: fromCol, toCol: toCol}",
            bind_vars={
                "@ec": edge_collection_name,
                "relField": rel_type_field,
            },
        )
        result["collections_by_relation"] = _fold_by_relation(cursor, "fromCol", "toCol", "collections")
    except Exception as exc:
        logger.debug("Per-relation collection resolution failed for %s: %s", edge_collection_name, exc)

    return result


def _omit_samples(snapshot: dict[str, Any]) -> dict[str, Any]:
    clone = {**snapshot}
    # generated_at is intentionally excluded from fingerprints/caching because it is time-varying
    # and does not represent the underlying physical schema.
    if "generated_at" in clone:
        clone["generated_at"] = None
    cols = []
    for c in clone.get("collections", []) or []:
        if not isinstance(c, dict):
            continue
        c2 = dict(c)
        c2.pop("sample_documents", None)
        c2.pop("sample_edges", None)
        cols.append(c2)
    clone["collections"] = cols
    return clone


def fingerprint_physical_schema(snapshot: dict[str, Any], *, include_samples: bool = False) -> str:
    # Even when including samples, normalize away generated_at to keep fingerprints stable.
    data = dict(snapshot) if isinstance(snapshot, dict) else {"raw": snapshot}
    if "generated_at" in data:
        data["generated_at"] = None
    if not include_samples:
        data = _omit_samples(data)
    return sha256_hex(stable_dumps(data))


# ──────────────────────────────────────────────────────────────────────────
# Cheap db-keyed fingerprint probes (issue #7)
#
# These helpers answer "has the physical schema changed?" without running a
# full snapshot. They read only python-arango primitives (``db.collections()``,
# ``col.indexes()``, ``col.count()``) — no AQL, no samples, no analyzer logic.
# ──────────────────────────────────────────────────────────────────────────


def _stable_index_digest(idx: dict[str, Any]) -> str:
    """Identity-only digest of an index.

    Intentionally insensitive to the auto-generated ``name`` and ``id`` that
    ArangoDB can re-assign to semantically-equivalent indexes across restarts
    and rebuilds. Primary indexes contribute no bytes because every collection
    has exactly one and their content is not user-controlled.
    """
    if not isinstance(idx, dict):
        return ""
    if idx.get("type") == "primary":
        return ""
    fields = idx.get("fields")
    fields_part = ",".join(str(f) for f in fields) if isinstance(fields, list) else ""
    return "|".join(
        [
            str(idx.get("type") or ""),
            fields_part,
            "u" if idx.get("unique") else "",
            "s" if idx.get("sparse") else "",
            "v" if idx.get("vci") else "",
            # ``deduplicate`` defaults to True on persistent indexes; only the
            # explicit opt-out (``False``) perturbs the digest.
            "d" if idx.get("deduplicate") is False else "",
        ]
    )


def _iter_user_collections(
    db: StandardDatabase,
    *,
    exclude_collections: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Return user (non-system) collection descriptors sorted by name.

    Tolerates a ``db.collections()`` call that raises or returns a non-list.
    Each entry is the raw descriptor dict from python-arango (keys such as
    ``name``, ``type``, ``status``); callers should only rely on ``name`` and
    ``type``.
    """
    exclude = set(exclude_collections or ())
    try:
        raw = db.collections()
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if not isinstance(name, str) or not name or name.startswith("_"):
            continue
        if name in exclude:
            continue
        out.append(c)
    out.sort(key=lambda x: str(x.get("name", "")))
    return out


def fingerprint_physical_shape(
    db: StandardDatabase,
    *,
    exclude_collections: Iterable[str] | None = None,
) -> str:
    """Cheap shape-only fingerprint of the physical schema.

    Hashes only the user-collection set, each collection's type
    (``document`` vs ``edge``), and each collection's sorted set of index
    digests (``type``, ``fields``, ``unique``, ``sparse``, ``vci``,
    ``deduplicate``). Auto-generated index ``name`` / ``id`` are excluded.

    **Stable** under ordinary writes (INSERT / UPDATE / REMOVE).

    **Changes** when:

    * a user collection is created or dropped,
    * a collection changes type between document and edge,
    * an index is added, dropped, or any identity-carrying property of an
      existing index changes.

    Individual-collection failures (e.g. ``indexes()`` raises) are absorbed
    as a sentinel contribution rather than propagated — callers comparing
    two fingerprints against each other still get a useful boolean.
    """
    parts: list[str] = []
    for c in _iter_user_collections(db, exclude_collections=exclude_collections):
        name = str(c.get("name", ""))
        col_type = "edge" if c.get("type") in (3, "edge") else "doc"
        try:
            idxs = collection_indexes(db.collection(name))
        except Exception:
            idxs = []
        digests = sorted(d for d in (_stable_index_digest(i) for i in idxs) if d)
        parts.append(f"{name}:{col_type}:" + ";".join(digests))
    raw = f"{getattr(db, 'name', '')}|" + "|".join(parts)
    return sha256_hex(raw)


def fingerprint_physical_counts(
    db: StandardDatabase,
    *,
    exclude_collections: Iterable[str] | None = None,
) -> str:
    """Shape fingerprint combined with per-collection row counts.

    Changes whenever :func:`fingerprint_physical_shape` changes **or** any
    included collection's ``count()`` changes. Intended for consumers that
    want to refresh derived *statistics* (cf. issue #3) when only data volume
    drifts, while the conceptual schema and physical mapping themselves
    remain valid.

    A ``count()`` failure on any individual collection contributes ``-1`` so
    the fingerprint stays well-defined under transient errors.
    """
    shape = fingerprint_physical_shape(db, exclude_collections=exclude_collections)
    parts: list[str] = []
    for c in _iter_user_collections(db, exclude_collections=exclude_collections):
        name = str(c.get("name", ""))
        try:
            count = collection_count(db.collection(name))
        except Exception:
            count = -1
        parts.append(f"{name}:{count}")
    raw = f"{shape}|" + "|".join(parts)
    return sha256_hex(raw)


def _normalize_index(idx: Any) -> dict[str, Any]:
    if not isinstance(idx, dict):
        return {"raw": idx}
    # Keep a stable subset; preserve remaining keys for forward compatibility.
    normalized = dict(idx)
    # Normalize fields ordering if present
    fields = normalized.get("fields")
    if isinstance(fields, list):
        normalized["fields"] = list(fields)
    return normalized


def _sort_indexes(indexes: list[Any]) -> list[Any]:
    def key(idx: Any):
        if not isinstance(idx, dict):
            return ("", "", "")
        return (
            str(idx.get("type") or ""),
            ",".join([str(x) for x in (idx.get("fields") or [])]) if isinstance(idx.get("fields"), list) else "",
            str(idx.get("name") or idx.get("id") or ""),
        )

    return sorted([_normalize_index(i) for i in indexes], key=key)


def _summarize_graph_props(props: Any) -> dict[str, Any]:
    """
    Extract stable, high-signal graph details:
    - edgeDefinitions (collection, from[], to[])
    - orphanCollections
    - SmartGraph topology flags needed by sharding-profile
      classification: isSmart, isDisjoint, smartGraphAttribute,
      isSatellite (only when present on the source — older ArangoDB
      versions omit them).
    """
    if not isinstance(props, dict):
        return {"raw": props}
    out: dict[str, Any] = {}
    if "name" in props:
        out["name"] = props.get("name")

    # Accept both python-arango's normalized snake_case shape
    # (``edge_definitions`` with ``edge_collection`` / ``from_vertex_collections``
    # / ``to_vertex_collections``) and the raw HTTP camelCase shape
    # (``edgeDefinitions`` with ``collection`` / ``from`` / ``to``), normalizing
    # to the internal ``{collection, from, to}`` records the rest of the
    # codebase (utils.iter_edge_definitions, sharding_profile, membership) reads.
    raw_defs = props.get("edge_definitions")
    if not isinstance(raw_defs, list):
        raw_defs = props.get("edgeDefinitions")
    if isinstance(raw_defs, list):
        defs = []
        for d in raw_defs:
            if not isinstance(d, dict):
                continue
            collection = d.get("collection") or d.get("edge_collection")
            frm = d.get("from")
            if not isinstance(frm, list):
                frm = d.get("from_vertex_collections")
            to = d.get("to")
            if not isinstance(to, list):
                to = d.get("to_vertex_collections")
            defs.append(
                {
                    "collection": collection,
                    "from": sorted(list(frm or [])),
                    "to": sorted(list(to or [])),
                }
            )
        out["edge_definitions"] = sorted(defs, key=lambda x: str(x.get("collection") or ""))

    orphans = props.get("orphan_collections")
    if not isinstance(orphans, list):
        orphans = props.get("orphanCollections")
    if isinstance(orphans, list):
        out["orphan_collections"] = sorted(orphans)

    # Smart/Satellite topology flags — accept snake_case (python-arango) and
    # camelCase (raw), emit the camelCase keys sharding-profile consumes.
    flag_aliases = {
        "isSmart": ("isSmart", "is_smart", "smart"),
        "isDisjoint": ("isDisjoint", "is_disjoint", "disjoint"),
        "smartGraphAttribute": ("smartGraphAttribute", "smart_graph_attribute"),
        "isSatellite": ("isSatellite", "is_satellite", "satellite"),
    }
    for out_key, sources in flag_aliases.items():
        for src in sources:
            if src in props:
                out[out_key] = props[src]
                break
    return out


def _collect_database_properties(db: Any) -> dict[str, Any]:
    """Capture the database-level properties needed by the
    sharding-profile classifier. Best-effort: any probe that throws
    is silently skipped and the missing keys degrade the classifier
    to ``Sharded`` / ``degraded`` at classification time.

    Returns a dict with whichever of ``name``, ``sharding``,
    ``replicationFactor``, ``writeConcern`` could be retrieved.
    """
    out: dict[str, Any] = {}
    try:
        name = getattr(db, "name", None)
        if isinstance(name, str) and name:
            out["name"] = name
    except Exception:  # pragma: no cover — defensive
        pass
    try:
        props = db.properties() if hasattr(db, "properties") else None
    except Exception:
        props = None
    if isinstance(props, dict):
        for key in ("sharding", "replicationFactor", "writeConcern"):
            if key in props and props[key] is not None:
                out[key] = props[key]
    return out


def _graph_scope_collections(db: StandardDatabase, graph_name: str) -> set[str]:
    """Collections belonging to a named graph: edge-definition collections +
    their ``from``/``to`` vertices + orphan collections. Raises
    ``INVALID_ARGUMENT`` when the graph cannot be read (missing / no access)."""
    try:
        props = graph_properties(db, graph_name)
    except Exception as e:
        raise SchemaAnalyzerError(
            f"Named graph not found or unreadable: {graph_name}",
            code="INVALID_ARGUMENT",
            cause=e,
        ) from e
    vertices, edges = graph_collections(_summarize_graph_props(props))
    return vertices | edges


def snapshot_physical_schema(
    db: StandardDatabase,
    *,
    sample_limit_per_collection: int = 0,
    include_samples_in_snapshot: bool = False,
    graph_scope: str | None = None,
    sample_value_top_k: int | None = None,
    min_type_value_count: int = 0,
    nested_field_depth: int = 0,
) -> dict[str, Any]:
    """
    Deterministic physical schema snapshot using python-arango Database.

    Always detects type discriminator fields (via AQL COLLECT) and edge endpoint
    collections regardless of sample_limit_per_collection, so the baseline can
    correctly classify PG vs LPG physical model styles.

    When ``graph_scope`` names an existing named graph, analysis is restricted
    to that graph's collections (edge collections + their from/to vertices +
    orphan collections); all other user collections are excluded. Useful to
    bound cost / LLM egress / focus on a single subsystem. Raises
    ``INVALID_ARGUMENT`` if the named graph does not exist.

    ``sample_value_top_k`` raises (or lowers) the per-field distinct-value cap
    kept in ``sample_field_value_counts`` — and thereby the maximum number of
    entity/relationship types derivable from one discriminated collection.
    Default ``None`` keeps ``SAMPLE_VALUE_TOP_K``. When raised past
    ``MAX_TYPE_FIELD_DISTINCT_VALUES``, the discriminator acceptance bound
    scales with it so the extra observed values don't disqualify the field.

    ``nested_field_depth`` (0-5, default 0 = off; issue #33) also walks the sampled
    documents into nested objects, so each collection's ``observed_fields`` gains the
    nested paths, the dotted names held in two shapes, and the lists of objects not
    walked. The depth is recorded on the snapshot, so it is part of the fingerprint
    and a cached analysis made without it is never reused.
    """
    depth = nested_depth(nested_field_depth)
    top_k = sample_value_top_k if isinstance(sample_value_top_k, int) and sample_value_top_k > 0 else SAMPLE_VALUE_TOP_K
    floor = min_type_value_count if isinstance(min_type_value_count, int) and min_type_value_count > 0 else 0
    max_distinct = type_field_max_distinct(top_k, floor)
    collections_info = db.collections()
    if isinstance(collections_info, dict):
        collections = collections_info
    elif isinstance(collections_info, list):
        collections = {}
        for item in collections_info:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name:
                    try:
                        collections[name] = db.collection(name)
                    except Exception:
                        continue
    else:
        collections = {}

    if graph_scope:
        scope_cols = _graph_scope_collections(db, graph_scope)
        collections = {n: c for n, c in collections.items() if n in scope_cols}
    snapshot: dict[str, Any] = {
        "version": SNAPSHOT_FORMAT_VERSION,
        "generated_at": None,
        "sample_value_top_k": top_k,
        "collections": [],
        "graphs": [],
        "database": _collect_database_properties(db),
    }
    if floor:
        snapshot["min_type_value_count"] = floor
    if depth:
        snapshot["nested_field_depth"] = depth

    # ── Phase 1: Collect metadata for every collection ──────────────────
    entries: list[dict[str, Any]] = []
    for name in sorted(collections.keys()):
        col = collections[name]
        if name.startswith("_"):
            continue

        try:
            props = collection_properties(col)
        except Exception as e:
            props = {"error": str(e)}

        try:
            count = collection_count(col)
        except Exception:
            count = None

        try:
            indexes = _sort_indexes(collection_indexes(col))
        except Exception:
            indexes = []

        is_edge = bool(props.get("type") == 3) if isinstance(props, dict) else False

        entry: dict[str, Any] = {
            "name": name,
            "type": "edge" if is_edge else "document",
            "count": count,
            "properties": props,
            "indexes": indexes,
            "candidate_type_fields": [],
        }
        if is_edge:
            entry["inferred_relationship_type"] = infer_relationship_type_from_collection_name(name)
        else:
            entry["inferred_entity_type"] = infer_entity_type_from_collection_name(name)

        entries.append(entry)

    # ── Phase 2: Detect type discriminators for ALL collections (COLLECT) ──
    for entry in entries:
        candidates, value_counts, distinct_counts, overflow = _detect_type_fields_via_collect(
            db, entry["name"], top_k=top_k, floor=floor
        )
        entry["candidate_type_fields"] = candidates
        entry["sample_field_value_counts"] = value_counts
        entry["sample_field_distinct_counts"] = distinct_counts
        if overflow:
            entry["sample_field_value_overflow"] = overflow

    # ── Phase 3: Build doc type field map for edge endpoint resolution ──
    doc_type_info: dict[str, tuple[str, set[str]]] = {}
    # Phase 4 reuses these picks: a document's pick reads only its phase-2 stats.
    doc_type_fields: dict[str, str | None] = {}
    for entry in entries:
        if entry["type"] != "document":
            continue
        best_field = _pick_best_type_field(entry, is_edge=False, max_distinct_values=max_distinct)
        doc_type_fields[entry["name"]] = best_field
        if best_field:
            values = set(_type_values_for_field(entry, best_field))
            if values:
                doc_type_info[entry["name"]] = (best_field, values)

    # ── Phase 4: Detect edge endpoints and property fields ──────────────
    for entry in entries:
        is_edge = entry["type"] == "edge"
        endpoint_collections: dict[str, list[str]] | None = None
        if is_edge:
            # The endpoint collections come first: the picker drops a field that only
            # repeats them (``ENDPOINT_MIRROR_FRACTION``), as the baseline's pick does.
            endpoint_collections = _detect_edge_endpoint_collections(db, entry["name"])
            entry["edge_endpoints"] = dict(endpoint_collections or {})
            best_field = _pick_best_type_field(entry, is_edge=True, max_distinct_values=max_distinct)
        else:
            best_field = doc_type_fields[entry["name"]]
        type_values = _type_values_for_field(entry, best_field) if best_field else None

        if not is_edge and best_field:
            cooccurrence = _detect_label_cooccurrence(db, entry["name"], best_field)
            if cooccurrence:
                entry["label_cooccurrence"] = {best_field: cooccurrence}

        if is_edge and endpoint_collections is not None:
            entry["edge_endpoints"] = _detect_edge_endpoints(
                db,
                entry["name"],
                rel_type_field=best_field,
                doc_type_info=doc_type_info,
                collections=endpoint_collections,
            )

        entry["observed_fields"] = _detect_observed_fields(
            db,
            entry["name"],
            type_field=best_field,
            type_values=type_values,
            nested_field_depth=depth,
        )

    # ── Phase 5: Full document sampling (optional) ──────────────────────
    # Only when the samples are kept: nothing else reads them.
    if include_samples_in_snapshot and sample_limit_per_collection and sample_limit_per_collection > 0:
        for entry in entries:
            cname = entry["name"]
            try:
                cursor = aql_execute(
                    db,
                    "FOR d IN @@c LIMIT @limit RETURN d",
                    bind_vars={"@c": cname, "limit": int(sample_limit_per_collection)},
                )
                entry["sample_edges" if entry["type"] == "edge" else "sample_documents"] = list(cursor)
            except Exception as e:
                entry["sample_error"] = str(e)

    snapshot["collections"] = entries

    # ── Named graphs (best-effort) ──────────────────────────────────────
    try:
        graphs = database_graphs(db)
        snapshot["graphs"] = graphs
        detailed = []
        if isinstance(graphs, list):
            for g in graphs:
                name = None
                if isinstance(g, str):
                    name = g
                elif isinstance(g, dict):
                    name = g.get("name") or g.get("_key") or g.get("id")
                if not name:
                    continue
                try:
                    gp = graph_properties(db, name)
                    detailed.append(_summarize_graph_props(gp))
                except Exception as e:
                    detailed.append({"name": name, "error": str(e)})
        snapshot["graphs_detailed"] = sorted(detailed, key=lambda x: str(x.get("name") or ""))
    except Exception as e:
        snapshot["graphs_error"] = str(e)

    return snapshot
