"""Attributes that hold a list of values: find them in the sample and mark them.

ArangoDB stores a multi-valued property as one attribute holding an array
(``questionsServed: ["Q1", "Q2"]``). A reader that takes the attribute as one
value binds the whole array: a lookup of one element finds nothing, and a count
of values counts documents. Reading it one element per value is right for such an
attribute and wrong for every other one (an equality an index can serve becomes a
membership test, and a document whose array is empty has no value), so a reader
needs to know which attributes are lists. This module tells it.

**What the snapshot records.** For each collection, ``observed_fields.list_fields``
names the top-level attributes that some sampled document holds as an array of
scalars (strings, numbers, booleans, null), an empty array included. The key is
written only when there is one, so a collection without lists snapshots as before.
An attribute some sampled document holds as an array containing an object or an
array is left out: its elements are not values a reader can compare, and the nested
lookup already reports a list of objects as skipped. A scalar in one document and a
list in another is a list: a scalar is a one-element list, so reading it that way
loses nothing.

**What the analysis says.** :func:`apply_list_values` marks each property mapping
whose attribute is listed, on the baseline and the LLM path alike, with
``multiValued: true``, and the conceptual property of the same name too. ``type``
keeps naming the type of one value (an element), not ``array``. Two exceptions,
taken from the collection's indexes: an attribute a geo index covers holds one point
(``[lat, lon]``) and one a vector index covers holds one embedding, so neither is
marked.

**It is a sample.** ``_PROPERTY_SAMPLE_LIMIT`` documents per collection (per type
value for a discriminated one). An attribute that holds a list only in documents
the sample missed is not marked, and a reader then binds its arrays whole, as it
did before this marker existed.

A value inside a nested object (``path``) is never marked: lists below the top
level are a later step, as in :mod:`.nested_fields`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: The key a property mapping (and a conceptual property) carries when its
#: attribute holds a list, one value per element.
MULTI_VALUED_KEY = "multiValued"

#: Index types whose field holds one value even though it is stored as an array.
_ONE_VALUE_INDEX_TYPES = frozenset({"geo", "geo1", "geo2", "vector"})

_SYSTEM_FIELDS = frozenset({"_key", "_id", "_rev", "_from", "_to"})


def classify_list(value: Any) -> str | None:
    """``"scalars"`` for an array of scalars (or an empty one), ``"structures"`` for
    an array holding an object or an array, ``None`` for anything that is not a list."""
    if not isinstance(value, list):
        return None
    if any(isinstance(item, dict | list) for item in value):
        return "structures"
    return "scalars"


def list_fields_of(docs: Iterable[Any], *, exclude: Iterable[str] = ()) -> list[str]:
    """Top-level attributes the sampled *docs* hold as a list of scalars (sorted).

    An attribute that any document holds as a list of objects or of lists is left out.
    """
    excluded = set(_SYSTEM_FIELDS) | set(exclude)
    scalars: set[str] = set()
    structures: set[str] = set()
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, value in doc.items():
            if not isinstance(key, str) or not key or key in excluded:
                continue
            kind = classify_list(value)
            if kind == "scalars":
                scalars.add(key)
            elif kind == "structures":
                structures.add(key)
    return sorted(scalars - structures)


def _one_value_fields(col: dict[str, Any]) -> set[str]:
    """Attributes a geo or vector index covers: stored as an array, one value."""
    out: set[str] = set()
    for idx in col.get("indexes") or []:
        if not isinstance(idx, dict):
            continue
        if str(idx.get("type", "")).lower() not in _ONE_VALUE_INDEX_TYPES:
            continue
        for field in idx.get("fields") or []:
            if isinstance(field, str):
                out.add(field)
    return out


def observed_list_fields(col: dict[str, Any]) -> set[str]:
    """The attributes of a snapshot collection to read one element per value."""
    observed = col.get("observed_fields")
    if not isinstance(observed, dict):
        return set()
    listed = {f for f in observed.get("list_fields") or [] if isinstance(f, str) and f}
    return listed - _one_value_fields(col)


def _mark_conceptual(data: dict[str, Any], kind: str, key: str, marked: dict[str, set[str]]) -> None:
    cs = data.get("conceptualSchema")
    elements = cs.get(kind) if isinstance(cs, dict) else None
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        names = marked.get(str(element.get(key)))
        if not names:
            continue
        for prop in element.get("properties") or []:
            if isinstance(prop, dict) and prop.get("name") in names:
                prop[MULTI_VALUED_KEY] = True


def apply_list_values(data: dict[str, Any], snapshot: dict[str, Any]) -> int:
    """Mark the property mappings whose attribute holds a list; return how many.

    For each entity and relationship property mapping that reads a top-level
    attribute (``field``, or the property name when there is none) of a collection
    whose sample holds that attribute as a list (:func:`observed_list_fields`), set
    ``multiValued: true`` on the mapping and on the conceptual property of the same
    name. Mutates *data*; writes nothing when no attribute is a list, so the output
    of a database without lists is unchanged.
    """
    by_collection: dict[str, set[str]] = {}
    for col in snapshot.get("collections") or []:
        if isinstance(col, dict) and isinstance(col.get("name"), str):
            fields = observed_list_fields(col)
            if fields:
                by_collection[col["name"]] = fields
    if not by_collection:
        return 0

    count = 0
    pm = data.get("physicalMapping")
    for kind, coll_key, key in (
        ("entities", "collectionName", "name"),
        ("relationships", "edgeCollectionName", "type"),
    ):
        specs = pm.get(kind) if isinstance(pm, dict) else None
        if not isinstance(specs, dict):
            continue
        marked: dict[str, set[str]] = {}
        for owner, spec in specs.items():
            if not isinstance(spec, dict) or not isinstance(spec.get("properties"), dict):
                continue
            lists = by_collection.get(spec.get(coll_key) or spec.get("collectionName") or "")
            if not lists:
                continue
            for prop, entry in spec["properties"].items():
                if not isinstance(entry, dict) or "path" in entry:
                    continue
                field = entry.get("field")
                attribute = field if isinstance(field, str) and field else prop
                if attribute in lists:
                    entry[MULTI_VALUED_KEY] = True
                    marked.setdefault(str(owner), set()).add(str(prop))
                    count += 1
        _mark_conceptual(data, kind, key, marked)
    return count
