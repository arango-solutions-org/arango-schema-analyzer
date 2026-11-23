"""Shard-family detection (PRD §6.2 bullet 5).

A *shard family* groups conceptual entities that share an identical
property set and a common name suffix — the structural fingerprint of
the per-source / per-repository / per-stream collection-duplication
pattern. The canonical example is::

    IBEX_Documents       → IBEXDocument
    MAROCCHINO_Documents → MAROCCHINODocument
    MOR1KX_Documents     → MOR1KXDocument
    OR1200_Documents     → OR1200Document

Four collections, four conceptual entities, *one* logical entity from a
consumer's perspective. Without family detection the LLM prompt builder
sees four entities, picks the first one alphabetically, and silently
narrows queries that should be fanning out across all four members.
That's defect D7 in ``arango-cypher-py/docs/schema_inference_bugfix_prd.md``.

This module is a deterministic, snapshot-only post-processor over
``data["physicalMapping"]["entities"]``. No DB round-trip, no LLM call.

Detection rules
---------------

1. **Bucket** entities by ``sha256(sorted(property_names))``. Skip
   buckets of size < ``MIN_SHARD_FAMILY_SIZE``.
2. Within each bucket, find the **longest common suffix** of the
   conceptual entity names that is at least
   ``MIN_SHARD_FAMILY_SUFFIX_LEN`` characters long and ends on a
   capital-letter boundary (the character just before the suffix in
   each name is lower-case, or the suffix is the entire name). Skip
   buckets with no qualifying suffix.
3. Extract the **prefix** of each member name (everything before the
   suffix). This is the per-member discriminator candidate.
4. **Probe for a discriminator field**: if every member entity declares
   a property whose name appears in
   ``SHARD_FAMILY_DISCRIMINATOR_FIELDS`` (case-insensitive),
   record ``discriminator.source = "field"`` and the field name.
   Otherwise record ``discriminator.source = "collection_prefix"`` and
   omit ``field``.
5. Emit one ``ShardFamily`` record per confirmed bucket. Families of
   one member are never emitted.

The output is a list (possibly empty) suitable for placement at
``data["physicalMapping"]["shardFamilies"]``. Empty lists are still
written so downstream consumers can tell "no families detected" apart
from "this analyzer build didn't run the detector" (the latter writes
nothing — see :func:`schema_analyzer.analyzer._apply_shard_families`).
"""

from __future__ import annotations

import logging
from typing import Any

from .defaults import (
    MIN_SHARD_FAMILY_SIZE,
    MIN_SHARD_FAMILY_SUFFIX_LEN,
    SHARD_FAMILY_DISCRIMINATOR_FIELDS,
)
from .utils import entity_property_names, sha256_hex

logger = logging.getLogger(__name__)


def _entity_property_names(entity: dict[str, Any]) -> list[str]:
    """Return the **sorted** property-name list for an entity mapping.

    Thin wrapper over :func:`schema_analyzer.utils.entity_property_names`
    that imposes the sort order required by shard-family bucketing
    (the bucket key is ``hash(sorted(properties))``).
    """
    return sorted(entity_property_names(entity))


def _has_field(entity: dict[str, Any], field: str) -> bool:
    """Case-insensitive check: does the entity mapping declare ``field``?"""
    target = field.lower()
    return any(name.lower() == target for name in entity_property_names(entity))


def _word_boundary_starts(name: str) -> set[int]:
    """Indices in ``name`` where a CamelCase / snake_case word starts.

    The set always contains ``0``. Additional boundaries:

    * ``aB``  — lowercase or digit immediately followed by an uppercase
      letter. Standard CamelCase boundary.
    * ``ABc`` — uppercase letter immediately followed by a lowercase
      letter, with the *previous* character also uppercase. This
      catches the ``IBEXDocument`` case: the boundary is at the
      ``D``, even though both ``X`` and ``D`` are uppercase, because
      the ``D`` starts a new word (``Document``).
    * ``_X`` — any non-underscore character immediately following an
      underscore. Snake_case word boundary.
    """
    starts: set[int] = {0}
    n = len(name)
    for i in range(1, n):
        prev = name[i - 1]
        cur = name[i]
        if cur == "_" and prev != "_":
            starts.add(i)
            continue
        if prev == "_" and cur != "_":
            starts.add(i)
            continue
        if cur.isupper() and (
            prev.islower() or prev.isdigit() or (prev.isupper() and i + 1 < n and name[i + 1].islower())
        ):
            starts.add(i)
    return starts


def _common_suffix(names: list[str], min_len: int) -> str | None:
    """Longest common suffix of ``names`` that is ≥ ``min_len`` chars
    AND begins on a CamelCase / snake_case word boundary in every name.

    Algorithm:
        1. Compute the longest common literal suffix across ``names``.
        2. Walk that length downwards to ``min_len``; return the first
           length whose start index sits on a word boundary in every
           name (ensuring we don't slice mid-word).

    The walk-back is necessary because the longest literal common
    suffix can land mid-word (e.g. ``"cument"`` shared between
    ``IBEXDocument`` and ``MAROCCHINODocument``); the next-shorter
    candidate (``"Document"``) is the one that actually corresponds to
    a clean conceptual boundary.

    Returns ``None`` when no qualifying suffix exists.
    """
    if not names:
        return None
    shortest = min(len(n) for n in names)
    if shortest < min_len:
        return None

    longest = 0
    for i in range(1, shortest + 1):
        candidate = names[0][-i:]
        if all(n.endswith(candidate) for n in names):
            longest = i
        else:
            break

    boundary_caches = {n: _word_boundary_starts(n) for n in names}
    for length in range(longest, min_len - 1, -1):
        suffix = names[0][-length:]
        if all((len(n) - length) in boundary_caches[n] for n in names):
            return suffix
    return None


def _bucket_hash(prop_names: list[str]) -> str:
    """sha256 of the sorted property-name list (joined by ``\\u0000``).

    Using a 1-byte separator that cannot legally appear in an entity
    property name makes the hash collision-free across pathological
    cases like ``["ab", "c"]`` vs ``["a", "bc"]``.
    """
    return sha256_hex("\u0000".join(prop_names))


def _resolve_discriminator(
    members: list[dict[str, Any]],
    candidate_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Pick the first candidate field carried by *every* member, or
    fall back to ``collection_prefix``."""
    for field in candidate_fields:
        if all(_has_field(m["entity_data"], field) for m in members):
            return {"source": "field", "field": field}
    return {"source": "collection_prefix"}


def detect_shard_families(
    data: dict[str, Any],
    *,
    min_family_size: int = MIN_SHARD_FAMILY_SIZE,
    min_suffix_len: int = MIN_SHARD_FAMILY_SUFFIX_LEN,
    discriminator_fields: tuple[str, ...] = SHARD_FAMILY_DISCRIMINATOR_FIELDS,
) -> list[dict[str, Any]] | None:
    """Detect shard families in ``data["physicalMapping"]["entities"]``.

    Returns:
        * ``None`` when the input has no usable entity dict — caller
          should not write a ``shardFamilies`` key in this case (lets
          consumers distinguish "didn't run" from "ran, found none").
        * ``[]`` when at least one entity exists but no bucket
          qualifies as a family — written as an explicit empty list so
          consumers see "ran, found none".
        * A list of family records otherwise. Each record matches the
          ``ShardFamily`` ``$def`` in the v1 tool-contract response
          schema.

    The list is sorted by ``name`` then by ``suffix`` for deterministic
    output across runs (golden-snapshot stability).
    """
    pm = data.get("physicalMapping")
    if not isinstance(pm, dict):
        return None
    entities = pm.get("entities")
    if not isinstance(entities, dict) or not entities:
        return None

    buckets: dict[str, list[dict[str, Any]]] = {}
    for ent_name, ent_data in entities.items():
        if not isinstance(ent_data, dict):
            continue
        prop_names = _entity_property_names(ent_data)
        if not prop_names:
            continue
        bucket_key = _bucket_hash(prop_names)
        buckets.setdefault(bucket_key, []).append(
            {
                "entity": ent_name,
                "entity_data": ent_data,
                "prop_names": prop_names,
            }
        )

    families: list[dict[str, Any]] = []
    for members in buckets.values():
        if len(members) < min_family_size:
            continue
        names = [m["entity"] for m in members]
        suffix = _common_suffix(names, min_suffix_len)
        if suffix is None:
            continue

        family_name = suffix.lstrip("_")
        if not family_name or len(family_name) < min_suffix_len:
            family_name = suffix

        discriminator = _resolve_discriminator(members, discriminator_fields)

        member_records: list[dict[str, Any]] = []
        for m in members:
            ent_name = m["entity"]
            prefix = ent_name[: len(ent_name) - len(suffix)]
            collection_name = m["entity_data"].get("collectionName")
            if not isinstance(collection_name, str):
                collection_name = ent_name
            member_records.append(
                {
                    "entity": ent_name,
                    "collectionName": collection_name,
                    "discriminatorValue": prefix,
                }
            )
        member_records.sort(key=lambda r: (r["discriminatorValue"], r["entity"]))

        shared_props = list(members[0]["prop_names"])

        families.append(
            {
                "name": family_name,
                "suffix": suffix,
                "discriminator": discriminator,
                "sharedProperties": shared_props,
                "members": member_records,
            }
        )

    families.sort(key=lambda f: (f["name"], f["suffix"]))
    if families:
        logger.info(
            "Shard-family detection: %d family(ies) found across %d entity(ies)",
            len(families),
            sum(len(f["members"]) for f in families),
        )
    return families
