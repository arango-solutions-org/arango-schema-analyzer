"""Redaction of physical snapshots before LLM egress (PRD §4.3).

When an LLM provider is configured, the physical schema snapshot — which can
include sampled documents and sampled field-value distributions — is sent to a
third-party API. That is customer-configured data egress, and some deployments
need to scrub the actual *data values* first while still letting the model see
the *structure* (collections, field names, indexes) it needs to infer a good
conceptual model.

Redaction is applied only to the copy of the snapshot handed to
``_build_prompt``; the local snapshot used for fingerprinting, baseline
inference, reconciliation, and statistics is always the unredacted original, so
output quality and grounding are unaffected by what was withheld from the
vendor.

Four independent, composable modes are supported:

* ``strip_samples`` — drop ``sample_documents`` / ``sample_edges`` entirely.
* ``mask_field_values`` — replace concrete *data values* (type-discriminator
  values) with opaque tokens while preserving field names, distinct-value
  counts, and structure. A single snapshot-wide value→token map is used so the
  same value masks to the same token everywhere it appears:
  ``sample_field_value_counts`` / ``sample_field_value_overflow`` values,
  ``observed_fields.by_type`` keys, ``label_cooccurrence`` labels, and
  ``edge_endpoints.entity_types_by_relation`` (relation keys + resolved
  endpoint entity-type lists). Type values resolved purely from collection names
  (Property-Graph endpoints) are left intact because they are not field data.
* ``mask_field_names`` — replace document *field names* with opaque, name-like
  ``redacted_field_N`` tokens before egress, then **round-trip** them back to the
  real names in the LLM output. A single snapshot-wide name→token map masks
  every field-name occurrence (``candidate_type_fields``,
  ``sample_field_value_counts`` / ``sample_field_value_overflow`` /
  ``sample_field_distinct_counts`` keys, ``observed_fields.fields`` /
  ``by_type`` value lists, ``indexes[*].fields``, ``label_cooccurrence`` keys,
  and sample-document keys),
  excluding ArangoDB system fields (``_key`` / ``_from`` / ``_to`` / …). The
  caller (:class:`AgenticSchemaAnalyzer`) applies the map to the prompt snapshot
  and then calls :func:`unmask_field_names` on the model's response so the
  conceptual schema and physical mapping carry the real names again. Because the
  token pattern ``redacted_field_<int>`` is un-masked by exact regex match, it is
  robust to the tokens appearing standalone (property names) or embedded in prose
  (descriptions).
* ``screen_field_values`` — the per-field form of ``mask_field_values``. The
  caller passes a function ``(collection, field, values) -> bool``; it is asked
  once per field that has sampled values in a collection, and a field it picks
  has its values masked the way ``mask_field_values`` masks every field (same
  ``<redacted>:N`` tokens, also in ``by_type`` keys and edge endpoints), and its
  values in sample documents replaced by ``<redacted>``. A nested object or a
  list is offered under its top-level field, with every string and number inside
  it, and picking it withholds the whole value. Every other value goes
  out unchanged, so a caller can withhold personal data and still let the model
  see type values like ``"Person"``. Library-only: a function cannot come from a
  request, so ``from_dict`` never sets it. Ignored when ``mask_field_values``
  already masks everything.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

REDACTED_VALUE_TOKEN = "<redacted>"
FIELD_NAME_TOKEN_PREFIX = "redacted_field_"

_SAMPLE_KEYS = ("sample_documents", "sample_edges")

# ArangoDB system / structural fields that are not sensitive user data and must
# stay intact so the LLM (and downstream reconciliation) can reason about edges.
_SYSTEM_FIELD_NAMES = frozenset({"_key", "_id", "_rev", "_from", "_to"})

_FIELD_TOKEN_RE = re.compile(re.escape(FIELD_NAME_TOKEN_PREFIX) + r"\d+")

_VALUE_COUNT_KEYS = ("sample_field_value_counts", "sample_field_value_overflow")

#: ``(collection, field, sampled values) -> True`` withholds that field's values.
FieldValueScreen = Callable[[str, str, list[str]], bool]


@dataclass(frozen=True)
class RedactionOptions:
    strip_samples: bool = False
    mask_field_values: bool = False
    mask_field_names: bool = False
    screen_field_values: FieldValueScreen | None = None

    @property
    def active(self) -> bool:
        return (
            self.strip_samples
            or self.mask_field_values
            or self.mask_field_names
            or self.screen_field_values is not None
        )

    @classmethod
    def from_dict(cls, data: Any) -> RedactionOptions:
        if not isinstance(data, dict):
            return cls()
        return cls(
            strip_samples=bool(data.get("stripSamples", False)),
            mask_field_values=bool(data.get("maskFieldValues", False)),
            mask_field_names=bool(data.get("maskFieldNames", False)),
        )


def _collect_sensitive_values(collections: list[Any]) -> dict[str, str]:
    """Build a deterministic value→token map from every data value in the snapshot.

    Sources are the discriminator-value spaces: ``sample_field_value_counts``
    values and ``observed_fields.by_type`` keys. Sorting before assignment keeps
    the mapping stable across runs.
    """
    values: set[str] = set()
    for entry in collections:
        if not isinstance(entry, dict):
            continue
        for svc_key in ("sample_field_value_counts", "sample_field_value_overflow"):
            svc = entry.get(svc_key)
            if isinstance(svc, dict):
                for items in svc.values():
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict) and "value" in item:
                                values.add(str(item["value"]))
        observed = entry.get("observed_fields")
        if isinstance(observed, dict) and isinstance(observed.get("by_type"), dict):
            values.update(str(k) for k in observed["by_type"])
        values.update(_cooccurring_labels(entry))
    return {v: f"{REDACTED_VALUE_TOKEN}:{i}" for i, v in enumerate(sorted(values))}


def _cooccurring_labels(entry: dict[str, Any], fields: set[str] | None = None) -> set[str]:
    """Every label in ``label_cooccurrence``, only under ``fields`` when given."""
    labels: set[str] = set()
    lc = entry.get("label_cooccurrence")
    if isinstance(lc, dict):
        for field, by_primary in lc.items():
            if (fields is None or field in fields) and isinstance(by_primary, dict):
                for primary, others in by_primary.items():
                    labels.add(str(primary))
                    if isinstance(others, dict):
                        labels.update(str(o) for o in others)
    return labels


def _mask_label_cooccurrence(entry: dict[str, Any], token_map: dict[str, str]) -> None:
    lc = entry.get("label_cooccurrence")
    if not isinstance(lc, dict):
        return
    masked: dict[str, Any] = {}
    for field, by_primary in lc.items():
        if not isinstance(by_primary, dict):
            masked[field] = by_primary
            continue
        masked[field] = {
            token_map.get(str(p), str(p)): (
                {token_map.get(str(o), str(o)): n for o, n in others.items()} if isinstance(others, dict) else others
            )
            for p, others in by_primary.items()
        }
    entry["label_cooccurrence"] = masked


def _scalar_leaves(value: Any) -> list[str]:
    """Every string and number in ``value``, however deep in objects and lists."""
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _scalar_leaves(v)]
    if isinstance(value, list):
        return [leaf for v in value for leaf in _scalar_leaves(v)]
    return [str(value)] if isinstance(value, (str, int, float)) else []


def _sampled_values_by_field(entry: dict[str, Any]) -> dict[str, list[str]]:
    """Every sampled value of each non-system field in one collection entry.

    A nested object or list counts as one top-level field holding every value inside it.
    """
    by_field: dict[str, list[str]] = {}
    for svc_key in _VALUE_COUNT_KEYS:
        svc = entry.get(svc_key)
        if isinstance(svc, dict):
            for field, items in svc.items():
                if isinstance(items, list):
                    by_field.setdefault(field, []).extend(
                        str(item["value"]) for item in items if isinstance(item, dict) and "value" in item
                    )
    for sk in _SAMPLE_KEYS:
        for doc in entry.get(sk) or []:
            if isinstance(doc, dict):
                for field, value in doc.items():
                    leaves = [] if field in _SYSTEM_FIELD_NAMES else _scalar_leaves(value)
                    if leaves:
                        by_field.setdefault(field, []).extend(leaves)
    return by_field


def _screened_fields(collections: list[Any], screen: FieldValueScreen) -> dict[int, set[str]]:
    """The fields ``screen`` withholds, keyed by the entry's position in ``collections``."""
    picked: dict[int, set[str]] = {}
    for i, entry in enumerate(collections):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        fields = {f for f, values in _sampled_values_by_field(entry).items() if screen(name, f, values)}
        if fields:
            picked[i] = fields
    return picked


def _screened_value_tokens(collections: list[Any], picked: dict[int, set[str]]) -> dict[str, str]:
    """Value→token map over the screened fields' counted values only, stable across runs."""
    values: set[str] = set()
    for i, fields in picked.items():
        for svc_key in _VALUE_COUNT_KEYS:
            svc = collections[i].get(svc_key)
            if isinstance(svc, dict):
                for field in fields:
                    items = svc.get(field)
                    if isinstance(items, list):
                        values.update(
                            str(item["value"]) for item in items if isinstance(item, dict) and "value" in item
                        )
        values.update(_cooccurring_labels(collections[i], fields))
    return {v: f"{REDACTED_VALUE_TOKEN}:{i}" for i, v in enumerate(sorted(values))}


def _mask_screened_fields(entry: dict[str, Any], fields: set[str], token_map: dict[str, str]) -> None:
    for svc_key in _VALUE_COUNT_KEYS:
        svc = entry.get(svc_key)
        if isinstance(svc, dict):
            screened = _mask_value_counts({f: v for f, v in svc.items() if f in fields}, token_map)
            entry[svc_key] = {f: screened.get(f, v) for f, v in svc.items()}
    for sk in _SAMPLE_KEYS:
        docs = entry.get(sk)
        if isinstance(docs, list):
            entry[sk] = [
                {k: (REDACTED_VALUE_TOKEN if k in fields else v) for k, v in doc.items()}
                if isinstance(doc, dict)
                else doc
                for doc in docs
            ]


def _mask_value_counts(value_counts: dict[str, Any], token_map: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, items in value_counts.items():
        if not isinstance(items, list):
            out[field] = items
            continue
        masked: list[Any] = []
        for item in items:
            if isinstance(item, dict) and "value" in item:
                new_item = dict(item)
                new_item["value"] = token_map.get(str(item["value"]), f"{REDACTED_VALUE_TOKEN}")
                masked.append(new_item)
            else:
                masked.append(item)
        out[field] = masked
    return out


def _mask_observed_fields(observed: dict[str, Any], token_map: dict[str, str]) -> None:
    for key in ("by_type", "nested_paths_by_type"):
        by_type = observed.get(key)
        if isinstance(by_type, dict):
            observed[key] = {token_map.get(str(k), str(k)): v for k, v in by_type.items()}


def _mask_edge_endpoints(endpoints: dict[str, Any], token_map: dict[str, str]) -> None:
    by_rel = endpoints.get("entity_types_by_relation")
    if not isinstance(by_rel, dict):
        return
    new_by_rel: dict[str, Any] = {}
    for rel, info in by_rel.items():
        masked_info = info
        if isinstance(info, dict):
            masked_info = dict(info)
            for side in ("from_entity_types", "to_entity_types"):
                vals = masked_info.get(side)
                if isinstance(vals, list):
                    masked_info[side] = [token_map.get(str(v), str(v)) for v in vals]
        new_by_rel[token_map.get(str(rel), str(rel))] = masked_info
    endpoints["entity_types_by_relation"] = new_by_rel


def build_field_name_map(collections: list[Any]) -> dict[str, str]:
    """Build a deterministic real-name→token map for every document field name.

    Gathers names from every field-bearing location in the snapshot
    (``candidate_type_fields``, ``sample_field_value_counts`` keys,
    ``observed_fields.fields`` / ``by_type`` value lists, ``indexes[*].fields``,
    and sample-document keys), excluding ArangoDB system fields. Sorting before
    token assignment keeps the mapping stable across runs.
    """
    names: set[str] = set()

    def _add(name: Any) -> None:
        if isinstance(name, str) and name and name not in _SYSTEM_FIELD_NAMES:
            names.add(name)

    for entry in collections:
        if not isinstance(entry, dict):
            continue
        for f in entry.get("candidate_type_fields") or []:
            _add(f)
        for svc_key in (
            "sample_field_value_counts",
            "sample_field_value_overflow",
            "sample_field_distinct_counts",
        ):
            svc = entry.get(svc_key)
            if isinstance(svc, dict):
                for k in svc:
                    _add(k)
        observed = entry.get("observed_fields")
        if isinstance(observed, dict):
            for f in observed.get("fields") or []:
                _add(f)
            by_type = observed.get("by_type")
            if isinstance(by_type, dict):
                for field_list in by_type.values():
                    if isinstance(field_list, list):
                        for f in field_list:
                            _add(f)
            # Nested lookup (issue #33): every path segment is a field name, and a
            # conflict's dotted name is one too (it may match no single segment).
            for path in _nested_paths_of(observed):
                for segment in path:
                    _add(segment)
            for conflict in observed.get("nested_conflicts") or []:
                if isinstance(conflict, dict):
                    _add(conflict.get("name"))
        indexes = entry.get("indexes")
        if isinstance(indexes, list):
            for idx in indexes:
                if isinstance(idx, dict) and isinstance(idx.get("fields"), list):
                    for f in idx["fields"]:
                        _add(f)
        for sk in _SAMPLE_KEYS:
            docs = entry.get(sk)
            if isinstance(docs, list):
                for doc in docs:
                    if isinstance(doc, dict):
                        for k in doc:
                            _add(k)

    return {name: f"{FIELD_NAME_TOKEN_PREFIX}{i}" for i, name in enumerate(sorted(names))}


def _nested_paths_of(observed: dict[str, Any]) -> list[list[Any]]:
    """Every path list under the nested-lookup keys of ``observed_fields``."""
    out: list[list[Any]] = []
    for key in ("nested_paths", "nested_arrays_skipped"):
        out.extend(p for p in observed.get(key) or [] if isinstance(p, list))
    by_type = observed.get("nested_paths_by_type")
    if isinstance(by_type, dict):
        for bucket in by_type.values():
            out.extend(p for p in bucket or [] if isinstance(p, list))
    for conflict in observed.get("nested_conflicts") or []:
        if isinstance(conflict, dict):
            out.extend(p for p in conflict.get("locations") or [] if isinstance(p, list))
    return out


def _mask_nested_names(observed: dict[str, Any], name_map: dict[str, str]) -> None:
    def mask_path(p: Any) -> Any:
        return [_mask_name(seg, name_map) for seg in p] if isinstance(p, list) else p

    for key in ("nested_paths", "nested_arrays_skipped"):
        if isinstance(observed.get(key), list):
            observed[key] = [mask_path(p) for p in observed[key]]
    by_type = observed.get("nested_paths_by_type")
    if isinstance(by_type, dict):
        observed["nested_paths_by_type"] = {
            k: [mask_path(p) for p in v] if isinstance(v, list) else v for k, v in by_type.items()
        }
    if isinstance(observed.get("nested_conflicts"), list):
        observed["nested_conflicts"] = [
            {
                **c,
                "name": _mask_name(c.get("name"), name_map),
                "locations": [mask_path(p) for p in c.get("locations") or []],
            }
            if isinstance(c, dict)
            else c
            for c in observed["nested_conflicts"]
        ]


def _mask_name(name: Any, name_map: dict[str, str]) -> Any:
    return name_map.get(name, name) if isinstance(name, str) else name


def _mask_field_names_in_entry(entry: dict[str, Any], name_map: dict[str, str]) -> None:
    ctf = entry.get("candidate_type_fields")
    if isinstance(ctf, list):
        entry["candidate_type_fields"] = [_mask_name(f, name_map) for f in ctf]

    for svc_key in (
        "sample_field_value_counts",
        "sample_field_value_overflow",
        "sample_field_distinct_counts",
    ):
        svc = entry.get(svc_key)
        if isinstance(svc, dict):
            entry[svc_key] = {_mask_name(k, name_map): v for k, v in svc.items()}

    observed = entry.get("observed_fields")
    if isinstance(observed, dict):
        fields = observed.get("fields")
        if isinstance(fields, list):
            observed["fields"] = [_mask_name(f, name_map) for f in fields]
        by_type = observed.get("by_type")
        if isinstance(by_type, dict):
            observed["by_type"] = {
                k: ([_mask_name(f, name_map) for f in v] if isinstance(v, list) else v) for k, v in by_type.items()
            }
        _mask_nested_names(observed, name_map)

    lc = entry.get("label_cooccurrence")
    if isinstance(lc, dict):
        entry["label_cooccurrence"] = {_mask_name(k, name_map): v for k, v in lc.items()}

    indexes = entry.get("indexes")
    if isinstance(indexes, list):
        for idx in indexes:
            if isinstance(idx, dict) and isinstance(idx.get("fields"), list):
                idx["fields"] = [_mask_name(f, name_map) for f in idx["fields"]]

    for sk in _SAMPLE_KEYS:
        docs = entry.get(sk)
        if isinstance(docs, list):
            entry[sk] = [
                {_mask_name(k, name_map): v for k, v in doc.items()} if isinstance(doc, dict) else doc for doc in docs
            ]


def unmask_field_names(obj: Any, name_map: dict[str, str]) -> Any:
    """Round-trip: replace ``redacted_field_N`` tokens back to real field names.

    Deep-walks ``obj`` (dict keys + values, lists, strings) and substitutes each
    token via exact regex match, so tokens are restored whether they appear as a
    standalone property name or embedded in a description string. Unknown tokens
    are left as-is. Returns a new structure; ``obj`` is not mutated.
    """
    inverse = {token: real for real, token in name_map.items()}
    if not inverse:
        return obj

    def _sub(s: str) -> str:
        return _FIELD_TOKEN_RE.sub(lambda m: inverse.get(m.group(0), m.group(0)), s)

    def _walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {(_sub(k) if isinstance(k, str) else k): _walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_walk(v) for v in o]
        if isinstance(o, str):
            return _sub(o)
        return o

    return _walk(obj)


def redact_snapshot_for_egress(
    snapshot: dict[str, Any],
    options: RedactionOptions | None,
    *,
    field_name_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a redacted deep copy of ``snapshot`` for LLM egress.

    When ``options`` is ``None`` or inactive, returns the snapshot unchanged
    (same object) to preserve byte-identical prompts and avoid needless copies.
    """
    if options is None or not options.active:
        return snapshot

    redacted = copy.deepcopy(snapshot)
    collections = redacted.get("collections")
    if not isinstance(collections, list):
        return redacted

    screened: dict[int, set[str]] = {}
    if options.mask_field_values:
        token_map = _collect_sensitive_values(collections)
    elif options.screen_field_values is not None:
        screened = _screened_fields(collections, options.screen_field_values)
        token_map = _screened_value_tokens(collections, screened)
    else:
        token_map = {}
    name_map: dict[str, str] = {}
    if options.mask_field_names:
        name_map = field_name_map if field_name_map is not None else build_field_name_map(collections)

    for i, entry in enumerate(collections):
        if not isinstance(entry, dict):
            continue
        if options.strip_samples:
            for key in _SAMPLE_KEYS:
                entry.pop(key, None)
        if options.mask_field_values:
            for svc_key in _VALUE_COUNT_KEYS:
                svc = entry.get(svc_key)
                if isinstance(svc, dict):
                    entry[svc_key] = _mask_value_counts(svc, token_map)
        elif i in screened:
            _mask_screened_fields(entry, screened[i], token_map)
        if token_map:
            # A masked value is masked everywhere it appears, in any collection.
            observed = entry.get("observed_fields")
            if isinstance(observed, dict):
                _mask_observed_fields(observed, token_map)
            endpoints = entry.get("edge_endpoints")
            if isinstance(endpoints, dict):
                _mask_edge_endpoints(endpoints, token_map)
            _mask_label_cooccurrence(entry, token_map)
        if options.mask_field_names and name_map:
            _mask_field_names_in_entry(entry, name_map)
    return redacted
