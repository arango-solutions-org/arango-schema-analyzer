"""Deterministic type-discriminator heuristics for the physical snapshot.

Pure helpers extracted from ``snapshot.py``: given sample documents and the
per-field value distributions the snapshot collects, decide which field (if
any) acts as a type discriminator, and derive conceptual names from collection
names. No database access — every function operates on plain dicts/strings —
so they are cheap to unit-test in isolation.

``snapshot.py`` re-exports these names, so existing imports such as
``from schema_analyzer.snapshot import _pick_best_type_field`` keep working.
"""

from __future__ import annotations

import re
from typing import Any

from .defaults import (
    FULL_LABEL_SET_HARD_CAP,
    MAX_BROADENED_TYPE_CANDIDATES,
    MAX_TYPE_FIELD_DISTINCT_VALUES,
    MAX_TYPE_VALUE_LENGTH,
    MIN_TYPE_FIELD_COVERAGE_FRACTION,
    MIN_TYPE_FIELD_DISTINCT_VALUES,
    SAMPLE_VALUE_TOP_K,
)
from .utils import pascal_case, singularize

CANDIDATE_TYPE_KEYS: list[str] = [
    "type",
    "_type",
    "label",
    "labels",
    "kind",
    "entityType",
    "relation",
    "relType",
    # Converged 2026-09-14 from arango-ontoextract's detector (CDF paper Q-5: the
    # analyzer owns type detection; three detectors are becoming one).
    "@type",
    "entity_type",
    "category",
    "relationship",
    "predicate",
]

PREFERRED_DOC_TYPE_FIELDS: list[str] = [
    "type",
    "_type",
    "entityType",
    "@type",
    "entity_type",
    "kind",
    "category",
    "label",
]
PREFERRED_EDGE_TYPE_FIELDS: list[str] = ["relation", "relType", "relationship", "predicate", "type"]

#: Tier-1 discriminator names (converged from arango-ontoextract / arango-cypher-py).
#: These names are unambiguously *type* fields, so they are accepted on **coverage
#: alone**: a high distinct count (a rich graph with many entity types) must not
#: disqualify them — that was the bug that collapsed a 40-type graph into one
#: collection-named class. Tier-2 names (``label``, ``category``, ``kind`` …) can just
#: as well hold a free-text display name, so they still pass the full distribution gate.
TIER1_DOC_TYPE_FIELDS: tuple[str, ...] = ("type", "_type", "entityType", "@type", "entity_type")
TIER1_EDGE_TYPE_FIELDS: tuple[str, ...] = ("type", "relation", "relationship", "relType", "predicate")

#: Edge documents may carry their endpoint *types* directly (arango-cypher-py's
#: ``GENERIC_WITH_TYPE`` convention), which lets endpoint resolution skip a per-edge
#: ``DOCUMENT()`` lookup. Checked in preference order. ``fromLabels`` / ``toLabels`` are
#: r2g's LPG layout, where each is a copy of the endpoint's label array; its first entry
#: (the primary label) is the endpoint type.
EDGE_FROM_TYPE_FIELDS: tuple[str, ...] = ("_fromType", "fromType", "_from_type", "fromLabels")
EDGE_TO_TYPE_FIELDS: tuple[str, ...] = ("_toType", "toType", "_to_type", "toLabels")


def infer_entity_type_from_collection_name(collection_name: str) -> str:
    return pascal_case(singularize(collection_name))


def infer_relationship_type_from_collection_name(collection_name: str) -> str:
    s = collection_name.replace("-", "_")
    return s.upper()


_DISCRIMINATOR_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: A field name that is not on the allow-list is probed as a discriminator only when it
#: carries a type-like token. ``rel_kind``, ``etype``, ``node_class`` qualify; ``priority``,
#: ``name``, ``mcc``, ``amount``, ``since``, ``ip`` and ``role`` do not (``role`` on an
#: edge is an attribute of the relationship, not its type — ``seen_by.role`` with ten
#: values must stay one dedicated relationship). Without this, any short
#: snake_case attribute holding a handful of label-shaped values split a dedicated
#: collection into one LABEL entity per value (found by the eval regression gate:
#: 7 gold entities became 23 on financial_fraud_detection once discriminator queries
#: started working on ArangoDB 3.12 in 0.3.x; the July baseline had scored a detector
#: that never ran).
_TYPE_LIKE_TOKENS: tuple[str, ...] = ("type", "kind", "class", "category", "label")
_DISCRIMINATOR_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
# Names ending in any of these are (almost) certainly identifiers, not types.
_ID_SUFFIXES: tuple[str, ...] = ("_id", "id", "_key", "key", "_uuid", "uuid", "_guid", "guid")


def _looks_like_discriminator_name(field: str) -> bool:
    """
    Heuristic: is this field name *plausibly* a type discriminator?

    We require a short, lowercase, snake_case identifier that is not obviously
    an ID/key/system field. Broadens the net past ``CANDIDATE_TYPE_KEYS`` so
    that collections whose type-tag field happens to be spelled ``category``,
    ``rel_kind``, ``etype`` etc. still get probed.
    """
    if not isinstance(field, str) or not field:
        return False
    if field.startswith("_"):
        return False
    lower = field.lower()
    if any(lower == suffix or lower.endswith(suffix) for suffix in _ID_SUFFIXES):
        return False
    if not _DISCRIMINATOR_NAME_RE.match(field):
        return False
    return any(tok in lower for tok in _TYPE_LIKE_TOKENS)


def _detect_candidate_type_fields(sample: dict[str, Any]) -> list[str]:
    """
    Enumerate candidate type-discriminator fields from a sample document.

    First the allow-listed names from ``CANDIDATE_TYPE_KEYS`` (fast path,
    preserves deterministic ordering for fixtures), then an additional batch
    of name-pattern matches capped at ``MAX_BROADENED_TYPE_CANDIDATES`` so
    AQL cost stays bounded.
    """
    if not isinstance(sample, dict):
        return []

    allow_listed: list[str] = [k for k in CANDIDATE_TYPE_KEYS if k in sample]
    already = set(allow_listed)

    broadened: list[str] = []
    for k, v in sample.items():
        if k in already:
            continue
        if not _looks_like_discriminator_name(k):
            continue
        if not isinstance(v, str):
            continue
        if len(v) > MAX_TYPE_VALUE_LENGTH or not _DISCRIMINATOR_VALUE_RE.match(v):
            continue
        broadened.append(k)
        if len(broadened) >= MAX_BROADENED_TYPE_CANDIDATES:
            break

    return allow_listed + broadened


def _iter_scalar_values(v: Any):
    if v is None:
        return
    if isinstance(v, (str, int, float, bool)):
        yield v
        return
    if isinstance(v, list):
        for x in v:
            if isinstance(x, (str, int, float, bool)):
                yield x
        return


#: Share of an edge field's observed value mass that must name the edge's own
#: endpoint collections for the field to count as an *endpoint mirror*. Such a
#: field (``destination_sub_type = "aws_ec2_instance"`` on a ``CAN_ACCESS``
#: edge collection, found on the IAM demo database) records where each edge
#: points, which ``_to`` already says; read as a relationship type it renames a
#: dedicated ``CAN_ACCESS`` collection into one "relationship" per target
#: collection, and Cypher can no longer say ``[:CAN_ACCESS]``.
ENDPOINT_MIRROR_FRACTION = 0.9


def _endpoint_collection_names(entry: dict[str, Any]) -> set[str]:
    """Casefolded names of an edge collection's from/to collections, as the
    snapshot sampled them or the named graphs declared them."""
    endpoints = entry.get("edge_endpoints") or {}
    if not isinstance(endpoints, dict):
        return set()
    names: set[str] = set()
    for key in ("from_collections", "to_collections"):
        for c in endpoints.get(key) or []:
            if isinstance(c, str) and c:
                names.add(c.casefold())
    return names


def _mirrors_endpoint_collections(items: Any, endpoint_names: set[str]) -> bool:
    """True when the field's observed values are (by mass) endpoint collection names."""
    if not endpoint_names or not isinstance(items, list) or not items:
        return False
    total = 0
    mirrored = 0
    for it in items:
        if not isinstance(it, dict) or "value" not in it:
            continue
        n = it.get("count")
        weight = n if isinstance(n, int) and n > 0 else 1
        total += weight
        if str(it["value"]).casefold() in endpoint_names:
            mirrored += weight
    return total > 0 and mirrored / total >= ENDPOINT_MIRROR_FRACTION


def type_field_max_distinct(top_k: Any, floor: Any) -> int:
    """Discriminator acceptance bound (``max_distinct_values``) for one snapshot.

    Scales with the value-sampling cap ``top_k`` (``sample_value_top_k``; unset or
    non-positive means ``SAMPLE_VALUE_TOP_K``) so a caller who raised it
    (``max_entity_types``) doesn't get the field rejected for exceeding the default
    32-distinct-value bound. A full-label-set snapshot (``floor`` =
    ``min_type_value_count`` > 0) accepts the high-cardinality discriminator
    outright so every above-floor label maps. The snapshot (from its arguments) and
    the baseline (from the snapshot's recorded values) both call this, so they pick
    against one bound.
    """
    if isinstance(floor, int) and floor > 0:
        return FULL_LABEL_SET_HARD_CAP
    cap = top_k if isinstance(top_k, int) and top_k > 0 else SAMPLE_VALUE_TOP_K
    return max(MAX_TYPE_FIELD_DISTINCT_VALUES, cap)


def _pick_best_type_field(
    entry: dict[str, Any],
    *,
    is_edge: bool = False,
    max_distinct_values: int | None = None,
) -> str | None:
    """Pick the best type-discriminator field from snapshot stats.

    Acceptance rules (any failure disqualifies the field):

    * Distinct value count in
      ``[MIN_TYPE_FIELD_DISTINCT_VALUES, MAX_TYPE_FIELD_DISTINCT_VALUES]``.
      The upper bound rejects high-cardinality ID-like fields that happen
      to pass name-pattern checks (e.g. ``comment_id`` with one distinct
      value per document).
    * Coverage fraction of the top-K observed distinct values, relative to
      the collection's document ``count``, is at least
      ``MIN_TYPE_FIELD_COVERAGE_FRACTION``. Fields whose non-null coverage
      is sparse do not look like a type tag.
    * Every observed value is a string of at most ``MAX_TYPE_VALUE_LENGTH``
      characters matching ``[A-Za-z0-9_-]+``. Free-form content is not a
      type label.

    Among the candidates that pass, the field with the most distinct values
    wins (tie-broken by the preferred-name ordering).

    Edge rule — a field whose values mirror the edge's endpoint collection
    names (:data:`ENDPOINT_MIRROR_FRACTION`) is never a discriminator, in any
    tier: it says where an edge points, not what relationship it is.

    Edge special case — a single-distinct-value field is accepted as a
    discriminator when that single value differs from both the collection
    name and its derived relationship type. This disambiguates a genuine
    generic-but-currently-single-type edge collection from a dedicated PG
    edge that carries a redundant metadata field (e.g. ``mentions`` edges
    with ``relation = "mentions"``).

    ``max_distinct_values`` overrides ``MAX_TYPE_FIELD_DISTINCT_VALUES`` —
    used when the snapshot was captured with a raised value-sampling top-K
    (``max_entity_types``), so a caller who asked for 50 entity types does
    not get the field rejected for having more than 32 observed values.
    """
    max_distinct = (
        max_distinct_values
        if isinstance(max_distinct_values, int) and max_distinct_values > 0
        else MAX_TYPE_FIELD_DISTINCT_VALUES
    )
    candidates = entry.get("candidate_type_fields") or []
    value_counts = entry.get("sample_field_value_counts") or {}
    distinct_totals = entry.get("sample_field_distinct_counts") or {}
    total_docs = int(entry.get("count") or 0)
    preferred = PREFERRED_EDGE_TYPE_FIELDS if is_edge else PREFERRED_DOC_TYPE_FIELDS
    tier1 = TIER1_EDGE_TYPE_FIELDS if is_edge else TIER1_DOC_TYPE_FIELDS
    ordered = [c for c in preferred if c in candidates] + [c for c in candidates if c not in preferred]
    if is_edge:
        endpoint_names = _endpoint_collection_names(entry)
        ordered = [c for c in ordered if not _mirrors_endpoint_collections(value_counts.get(c), endpoint_names)]

    # Tier-1 rule (converged from arango-ontoextract): an unambiguous type-field
    # name is accepted on coverage alone, in preference order, even when its true
    # distinct total exceeds the acceptance bound. The top-K sampling still caps the
    # *observed* values; ``entityTypeCaps`` / ``relationshipTypeCaps`` (or a raised
    # ``max_entity_types``) report what the cap dropped.
    for f in ordered:
        if f not in tier1:
            continue
        if _tier1_coverage_ok(value_counts.get(f), total_docs, distinct_totals.get(f)):
            return str(f)

    best: str | None = None
    best_n = 0
    for f in ordered:
        items = value_counts.get(f)
        if not _passes_distribution_shape(items, total_docs, max_distinct=max_distinct):
            continue
        n = len({str(it["value"]) for it in (items or []) if isinstance(it, dict) and "value" in it})
        if n > best_n:
            best = f
            best_n = n

    if best is not None and MIN_TYPE_FIELD_DISTINCT_VALUES <= best_n <= max_distinct:
        return best

    if is_edge:
        single = _single_value_edge_fallback(entry, value_counts, ordered)
        if single:
            return single

    return None


def _tier1_coverage_ok(items: Any, total_docs: int, distinct_total: Any) -> bool:
    """Tier-1 acceptance: label-shaped string values, broadly present, ≥ 1 distinct.

    Coverage is the observed count of the sampled values (top-K) plus nothing else, so
    a field whose top-K covers less than ``MIN_TYPE_FIELD_COVERAGE_FRACTION`` of the
    documents is accepted anyway when the snapshot reports a true distinct total larger
    than the sample (the long tail explains the shortfall). The distinct upper bound is
    deliberately *not* applied — that is the whole point of tier 1.
    """
    if not isinstance(items, list) or not items:
        return False
    observed = 0
    distinct: set[str] = set()
    for it in items:
        if not isinstance(it, dict) or "value" not in it:
            return False
        v = it["value"]
        if not isinstance(v, str) or len(v) > MAX_TYPE_VALUE_LENGTH or not _DISCRIMINATOR_VALUE_RE.match(v):
            return False
        distinct.add(v)
        c = it.get("count")
        if isinstance(c, int) and c > 0:
            observed += c
    # A single observed value is not tier-1 evidence: a dedicated PG edge collection
    # often carries a redundant ``relation = "<collection name>"`` field, which the
    # edge single-value fallback below judges on its own terms.
    if len(distinct) < MIN_TYPE_FIELD_DISTINCT_VALUES and not (
        isinstance(distinct_total, int) and distinct_total >= MIN_TYPE_FIELD_DISTINCT_VALUES
    ):
        return False
    if total_docs <= 0 or observed <= 0:
        return True
    if observed / total_docs >= MIN_TYPE_FIELD_COVERAGE_FRACTION:
        return True
    return isinstance(distinct_total, int) and distinct_total > len(items)


def _passes_distribution_shape(
    items: Any,
    total_docs: int,
    *,
    max_distinct: int = MAX_TYPE_FIELD_DISTINCT_VALUES,
) -> bool:
    """
    Gate a candidate field on its observed value distribution.

    Returns True iff:
      * ``items`` is a list of ``{"value", "count"}`` dicts,
      * every value is a string matching the discriminator shape rule,
      * the distinct count is within
        ``[MIN_TYPE_FIELD_DISTINCT_VALUES, max_distinct]``,
      * the sum of observed counts covers at least
        ``MIN_TYPE_FIELD_COVERAGE_FRACTION`` of ``total_docs``
        (or ``total_docs`` is unknown / 0, in which case we accept based on
        distinct count alone since we can't compute the ratio).
    """
    if not isinstance(items, list) or not items:
        return False
    distinct: set[str] = set()
    observed_count = 0
    for it in items:
        if not isinstance(it, dict) or "value" not in it:
            return False
        v = it["value"]
        if not isinstance(v, str):
            return False
        if len(v) > MAX_TYPE_VALUE_LENGTH or not _DISCRIMINATOR_VALUE_RE.match(v):
            return False
        distinct.add(v)
        c = it.get("count")
        if isinstance(c, int) and c > 0:
            observed_count += c
    n_distinct = len(distinct)
    if n_distinct < MIN_TYPE_FIELD_DISTINCT_VALUES:
        # Single-value fallback is handled separately upstream.
        return n_distinct == 1
    if n_distinct > max_distinct:
        return False
    # Full-label-set mode (signalled by the raised acceptance bound) deliberately
    # keeps the long tail of real labels and drops the sub-floor / non-label
    # junk, so the kept values legitimately cover less than the default fraction
    # of documents — skip the coverage gate rather than reject the field.
    if max_distinct >= FULL_LABEL_SET_HARD_CAP:
        return True
    return not (
        total_docs > 0 and observed_count > 0 and (observed_count / total_docs) < MIN_TYPE_FIELD_COVERAGE_FRACTION
    )


def _single_value_edge_fallback(
    entry: dict[str, Any],
    value_counts: dict[str, Any],
    ordered: list[str],
) -> str | None:
    """Accept a single-distinct-value field on an edge collection when its
    value is not just the collection name echoed back (see docstring on
    ``_pick_best_type_field``)."""
    for f in ordered:
        items = value_counts.get(f)
        if not isinstance(items, list) or len(items) != 1:
            continue
        first = items[0]
        if not isinstance(first, dict) or "value" not in first:
            continue
        raw = first["value"]
        if not isinstance(raw, str):
            continue
        single_val = raw.strip().upper().replace("-", "_")
        col_name = str(entry.get("name", "")).strip().upper().replace("-", "_")
        derived = infer_relationship_type_from_collection_name(entry.get("name", ""))
        if single_val and single_val != col_name and single_val != derived:
            return f
    return None


def _type_values_for_field(entry: dict[str, Any], field: str) -> list[str]:
    """Extract sorted distinct type values for a field from snapshot stats."""
    items = (entry.get("sample_field_value_counts") or {}).get(field)
    if not isinstance(items, list):
        return []
    return sorted(
        {
            str(it["value"]).strip()
            for it in items
            if isinstance(it, dict) and "value" in it and str(it["value"]).strip()
        }
    )
