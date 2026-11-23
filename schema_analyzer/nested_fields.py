"""Values inside nested objects: find them, name them, and flag ambiguity (issue #33).

A property mapping names where a value is stored. ``field`` is one literal attribute
name; ``path`` (a list, one name per level) names a value inside a nested object. The
dotted spelling ``address.city`` cannot say which it means: ``city`` inside the object
``address``, or one top-level attribute named exactly ``address.city``. So the analyzer
decides once, here, from what the sampled documents actually hold, and writes ``path`` or
``field`` to match. A reader never has to guess at query time.

**Flag, don't guess.** When one collection holds two shapes under the same dotted name,
the sample cannot say which one a caller means, so neither gets a mapping. The name is
reported under ``metadata.nestedFields.conflicts`` (and carried into CSI provenance) for a
person to settle.

**Lists are a later step.** A list of objects partway along a path is not walked:
readers treat a value behind a list as absent. Each one met is recorded under
``arraysSkipped`` so it is not silently missing.

Opt-in and depth-limited (``analysisOptions.nestedFieldDepth``, default 0): with depth 0
nothing here runs and the analyzer's output is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .utils import SYSTEM_FIELDS, collections_by_name, observed_field_names

#: Deepest look into nested objects a caller may request (levels below the top).
MAX_NESTED_FIELD_DEPTH = 5

Location = tuple[str, ...]

_SINGULAR = {"entities": "entity", "relationships": "relationship"}


def nested_depth(value: Any) -> int:
    """Clamp a requested depth to ``0..MAX_NESTED_FIELD_DEPTH`` (non-integers mean 0)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(int(value), MAX_NESTED_FIELD_DEPTH))


def dotted(location: Location) -> str:
    """The spelling a location shares with its look-alikes: segments joined by ``.``."""
    return ".".join(location)


def walk_document(
    doc: Any,
    max_depth: int,
    *,
    exclude: Iterable[str] = (),
) -> tuple[set[str], set[Location], set[Location]]:
    """Return ``(top_level_names, nested_paths, arrays_skipped)`` for one document.

    ``nested_paths`` holds every key path of length 2 to ``max_depth + 1`` reached
    through objects (an intermediate object is a value too, like a top-level one).
    ``arrays_skipped`` holds the path of each list that contains an object, which is
    not walked.
    """
    top: set[str] = set()
    nested: set[Location] = set()
    skipped: set[Location] = set()
    if not isinstance(doc, dict):
        return top, nested, skipped
    excluded = set(SYSTEM_FIELDS) | set(exclude)

    def visit(value: Any, prefix: Location, levels_left: int) -> None:
        if isinstance(value, list):
            if any(isinstance(item, dict) for item in value):
                skipped.add(prefix)
            return
        if not isinstance(value, dict) or levels_left <= 0:
            return
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                continue
            path = (*prefix, key)
            nested.add(path)
            visit(child, path, levels_left - 1)

    for key, value in doc.items():
        if not isinstance(key, str) or not key or key in excluded:
            continue
        top.add(key)
        if max_depth > 0:
            visit(value, (key,), max_depth)
    return top, nested, skipped


def find_conflicts(top_level: Iterable[str], nested: Iterable[Location]) -> dict[str, list[Location]]:
    """Dotted names reached by more than one location, mapped to those locations.

    A top-level attribute ``k`` is the location ``(k,)``. Two locations conflict when
    their dotted spellings match: ``("address.city",)`` and ``("address", "city")``,
    or ``("a", "b.c")`` and ``("a", "b", "c")``.
    """
    by_name: dict[str, set[Location]] = {}
    for name in top_level:
        by_name.setdefault(name, set()).add((name,))
    for path in nested:
        by_name.setdefault(dotted(path), set()).add(path)
    return {name: sorted(locs) for name, locs in sorted(by_name.items()) if len(locs) > 1}


def location_spec(location: Location) -> dict[str, Any]:
    """The property-mapping location for one location: ``field`` or ``path``."""
    return {"field": location[0]} if len(location) == 1 else {"path": list(location)}


def summarize_documents(docs: Iterable[Any], max_depth: int, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
    """Walk sampled documents into the snapshot's ``nested`` block.

    ``fields`` (top-level names) is returned too, so a caller can replace its
    ``ATTRIBUTES(d)`` pass with this one. Conflicted names are kept here, flagged;
    the baseline leaves them out of the mapping.
    """
    top: set[str] = set()
    nested: set[Location] = set()
    skipped: set[Location] = set()
    for doc in docs:
        t, n, s = walk_document(doc, max_depth, exclude=exclude)
        top |= t
        nested |= n
        skipped |= s
    return {
        "fields": sorted(top),
        "paths": [list(p) for p in sorted(nested)],
        "arrays_skipped": [list(p) for p in sorted(skipped)],
        "conflicts": [
            {"name": name, "locations": [list(loc) for loc in locs]}
            for name, locs in find_conflicts(top, nested).items()
        ],
    }


def conflicted_names(observed: dict[str, Any]) -> set[str]:
    """Dotted names the snapshot flagged for a collection (empty when nested was off)."""
    out: set[str] = set()
    for conflict in observed.get("nested_conflicts") or []:
        if isinstance(conflict, dict) and isinstance(conflict.get("name"), str):
            out.add(conflict["name"])
    return out


def _collection_nested(col: dict[str, Any]) -> tuple[set[Location], set[str], set[str]]:
    """``(nested paths, top-level names, conflicted names)`` for a snapshot collection."""
    observed = col.get("observed_fields") or {}
    if not isinstance(observed, dict):
        return set(), set(), set()
    paths: set[Location] = set()
    for raw in observed.get("nested_paths") or []:
        if isinstance(raw, list) and len(raw) > 1 and all(isinstance(s, str) and s for s in raw):
            paths.add(tuple(raw))
    for bucket in (observed.get("nested_paths_by_type") or {}).values():
        for raw in bucket or []:
            if isinstance(raw, list) and len(raw) > 1 and all(isinstance(s, str) and s for s in raw):
                paths.add(tuple(raw))
    return paths, observed_field_names(col), conflicted_names(observed)


def _drop_conceptual_properties(data: dict[str, Any], removed: list[dict[str, Any]]) -> None:
    cs = data.get("conceptualSchema")
    if not removed or not isinstance(cs, dict):
        return
    for kind, key in (("entities", "name"), ("relationships", "type")):
        gone: dict[str, set[str]] = {}
        for r in removed:
            owner = r.get(_SINGULAR[kind])
            if isinstance(owner, str):
                gone.setdefault(owner, set()).add(r["property"])
        for element in cs.get(kind) or []:
            if not isinstance(element, dict) or element.get(key) not in gone:
                continue
            props = element.get("properties")
            if isinstance(props, list):
                element["properties"] = [
                    p for p in props if not (isinstance(p, dict) and p.get("name") in gone[element[key]])
                ]


def apply_field_locations(data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Settle each property mapping's location against the snapshot; return the report.

    Runs after the baseline or the LLM has written the mapping, so both paths end with
    the same rule. For each entity and relationship property mapping on a collection the
    snapshot walked:

    * a dotted ``field`` the sample holds only as a nested path becomes ``path``;
    * a dotted ``field`` the sample holds as a literal top-level name stays ``field``;
    * a mapping whose dotted name is conflicted is removed (flagged, never guessed).

    Returns ``None`` when nested lookup was off (``snapshot["nested_field_depth"]`` unset),
    so the output stays byte-identical. Otherwise returns the ``metadata.nestedFields``
    block: ``maxDepth``, ``paths`` (count written as ``path``), ``conflicts`` and
    ``arraysSkipped`` (each naming its collection).
    """
    depth = nested_depth(snapshot.get("nested_field_depth"))
    if depth <= 0:
        return None

    by_collection = collections_by_name(snapshot)

    conflicts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for coll_name, coll_entry in sorted(by_collection.items()):
        observed = coll_entry.get("observed_fields") or {}
        if not isinstance(observed, dict):
            continue
        for conflict in observed.get("nested_conflicts") or []:
            if isinstance(conflict, dict):
                conflicts.append(
                    {
                        "collection": coll_name,
                        "name": conflict.get("name"),
                        "locations": [location_spec(tuple(loc)) for loc in conflict.get("locations") or []],
                    }
                )
        for path in observed.get("nested_arrays_skipped") or []:
            if isinstance(path, list):
                skipped.append({"collection": coll_name, "path": list(path)})

    written = 0
    removed: list[dict[str, Any]] = []
    pm = data.get("physicalMapping")
    for kind, coll_key in (("entities", "collectionName"), ("relationships", "edgeCollectionName")):
        specs = pm.get(kind) if isinstance(pm, dict) else None
        if not isinstance(specs, dict):
            continue
        for owner, spec in specs.items():
            if not isinstance(spec, dict) or not isinstance(spec.get("properties"), dict):
                continue
            col = by_collection.get(spec.get(coll_key) or spec.get("collectionName") or "")
            if col is None:
                continue
            paths, top, conflicted = _collection_nested(col)
            nested_by_name = {dotted(p): p for p in paths}
            for prop, entry in list(spec["properties"].items()):
                if not isinstance(entry, dict):
                    continue
                loc = entry.get("path")
                field_value = entry.get("field")
                name: str = prop
                if isinstance(loc, list) and loc:
                    name = dotted(tuple(str(seg) for seg in loc))
                elif isinstance(field_value, str):
                    name = field_value
                if name in conflicted:
                    del spec["properties"][prop]
                    removed.append({_SINGULAR[kind]: owner, "property": prop, "name": name})
                    continue
                if isinstance(loc, list) and len(loc) > 1:
                    written += 1
                    continue
                field = entry.get("field")
                if isinstance(field, str) and "." in field and field not in top and field in nested_by_name:
                    del entry["field"]
                    entry["path"] = list(nested_by_name[field])
                    written += 1
            if not spec["properties"]:
                del spec["properties"]

    # A conceptual property left without its mapping would be read by its own name — a
    # literal-field guess — so the conceptual side drops it too.
    _drop_conceptual_properties(data, removed)

    report: dict[str, Any] = {"maxDepth": depth, "paths": written, "conflicts": conflicts}
    if skipped:
        report["arraysSkipped"] = skipped
    if removed:
        report["unmapped"] = removed
    return report
