"""Class-abstraction discovery (PRD §6.3), delegated to ``conceptual-taxonomy``.

Discovering `rdfs:subClassOf` from a schema is paradigm-neutral — it reads entity names,
property sets, and relationships, and never touches AQL. `relational-schema-analyzer` needs
the identical capability over the identical bundle shape, so the mechanisms live in a shared
library and this module is only the adapter: it assembles the two inputs that *do* need
ArangoDB knowledge, and folds the proposals back into an analysis.

Optional dependency. Without it the analyzer degrades to no abstraction discovery rather
than failing, matching how every other optional capability behaves here.
"""

from __future__ import annotations

import logging
from typing import Any

from ._arango import aql_first
from .utils import normalize_analysis_dict

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    from conceptual_taxonomy import (
        Discriminator,
        KeyContainment,
        SpecializationMeasurement,
        discover_abstractions,
    )

    TAXONOMY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via the degradation test
    TAXONOMY_AVAILABLE = False


def shard_family_members(physical_mapping: dict[str, Any]) -> set[str]:
    """Entities already grouped as structural duplicates.

    A shard family satisfies the concept-analysis criteria too, so without excluding its
    members the run yields a synthesized abstraction competing with the family class. The
    two are genuinely different: family members are interchangeable — the shipped UNION
    guidance depends on it — while taxonomy members are not.
    """
    families = physical_mapping.get("shardFamilies")
    if not isinstance(families, list):
        return set()
    members: set[str] = set()
    for family in families:
        if not isinstance(family, dict):
            continue
        for member in family.get("members") or []:
            if isinstance(member, dict) and isinstance(member.get("entity"), str):
                members.add(member["entity"])
    return members


def build_discriminators(physical_mapping: dict[str, Any]) -> list[Any]:
    """Group ``LABEL`` entities by the collection and field that discriminate them.

    The value→entity mapping is supplied explicitly rather than left to name matching: the
    analyzer already knows which entity each discriminator value produced, so handing over a
    guess would be strictly worse than handing over the fact.
    """
    if not TAXONOMY_AVAILABLE:
        return []
    entities = physical_mapping.get("entities")
    if not isinstance(entities, dict):
        return []

    grouped: dict[tuple[str, str], dict[str, str]] = {}
    whole_collection_owner: dict[str, str] = {}

    for name, mapping in entities.items():
        if not isinstance(mapping, dict):
            continue
        collection = mapping.get("collectionName")
        if not isinstance(collection, str) or not collection:
            continue
        if mapping.get("style") == "LABEL":
            type_field, type_value = mapping.get("typeField"), mapping.get("typeValue")
            if isinstance(type_field, str) and isinstance(type_value, str):
                grouped.setdefault((collection, type_field), {})[type_value] = name
        elif mapping.get("style") == "COLLECTION":
            whole_collection_owner[collection] = name

    out = []
    for (collection, type_field), values in sorted(grouped.items()):
        if len(values) < 2:
            continue
        out.append(
            Discriminator(
                container=collection,
                field=type_field,
                values=sorted(values),
                # An entity mapped to the *whole* collection is the supertype: the
                # specialization case, where no parent needs synthesizing.
                parent_entity=whole_collection_owner.get(collection),
                entities=dict(values),
            )
        )
    return out


def measure_key_containment(
    db: Any,
    physical_mapping: dict[str, Any],
    *,
    max_probes: int = 64,
) -> tuple[list[Any], list[Any]]:
    """Find collections whose ``_key`` set is a subset of another's.

    The ArangoDB analogue of relational class-table inheritance, where a child table's
    primary key is also a foreign key to its parent's. Requires the database, which is why
    it is measured here and consumed there.

    Returns ``(containment, measurements)`` — the second carries the disjointness and
    completeness counts that make ``owl:disjointWith`` and a covering axiom *earned* rather
    than assumed (PRD §6.3, SPEC §4.3.1).
    """
    if not TAXONOMY_AVAILABLE or db is None:
        return [], []

    entities = physical_mapping.get("entities")
    if not isinstance(entities, dict):
        return [], []

    collections: dict[str, str] = {}
    for name, mapping in entities.items():
        if isinstance(mapping, dict) and mapping.get("style") == "COLLECTION":
            collection = mapping.get("collectionName")
            if isinstance(collection, str) and collection:
                collections[name] = collection

    containment: list[Any] = []
    probes = 0
    child_hits: dict[str, list[str]] = {}

    for child, child_collection in sorted(collections.items()):
        for parent, parent_collection in sorted(collections.items()):
            if child == parent or probes >= max_probes:
                continue
            probes += 1
            ratio = _key_containment_ratio(db, child_collection, parent_collection)
            if ratio is not None and ratio >= 0.99:
                containment.append(KeyContainment(child=child, parent=parent, ratio=ratio))
                child_hits.setdefault(parent, []).append(child)

    measurements: list[Any] = []
    for parent, children in sorted(child_hits.items()):
        if len(children) < 2:
            continue
        overlap = _specialization_counts(db, collections[parent], [collections[c] for c in children])
        if overlap is not None:
            measurements.append(
                SpecializationMeasurement(
                    parent=parent,
                    parent_keys_in_multiple_children=overlap["inMultiple"],
                    parent_keys_in_no_child=overlap["inNone"],
                    parent_key_count=overlap["total"],
                )
            )
    return containment, measurements


_CONTAINMENT_AQL = """
LET keys = (FOR d IN @@child LIMIT @sample RETURN d._key)
LET hits = LENGTH(FOR k IN keys FILTER DOCUMENT(@parent, k) != null RETURN 1)
RETURN LENGTH(keys) == 0 ? null : hits / LENGTH(keys)
"""

_SPECIALIZATION_AQL = """
LET rows = (
  FOR p IN @@parent
    LIMIT @sample
    LET n = LENGTH(FOR c IN @children FILTER DOCUMENT(c, p._key) != null RETURN 1)
    RETURN n
)
RETURN {
  total: LENGTH(rows),
  inMultiple: LENGTH(FOR n IN rows FILTER n > 1 RETURN 1),
  inNone: LENGTH(FOR n IN rows FILTER n == 0 RETURN 1)
}
"""


def _key_containment_ratio(db: Any, child: str, parent: str, sample: int = 200) -> float | None:
    row = aql_first(
        db,
        _CONTAINMENT_AQL,
        {"@child": child, "parent": parent, "sample": sample},
        log=logger,
        what=f"key containment probe for {child} in {parent}",
        level=logging.WARNING,
    )
    return None if row is None else float(row)


def _specialization_counts(db: Any, parent: str, children: list[str], sample: int = 200) -> dict[str, int] | None:
    row = aql_first(
        db,
        _SPECIALIZATION_AQL,
        {"@parent": parent, "children": children, "sample": sample},
        log=logger,
        what=f"specialization probe for {parent}",
        level=logging.WARNING,
    )
    return row if isinstance(row, dict) else None


def discover(
    analysis: Any,
    *,
    db: Any = None,
    namer: Any = None,
    measure_containment: bool = False,
) -> dict[str, Any] | None:
    """Run abstraction discovery over an analysis and return the proposals.

    ``None`` when the optional dependency is absent. Containment measurement is opt-in for
    the same reason FK probing is: it is a cross-collection database cost.
    """
    if not TAXONOMY_AVAILABLE:
        logger.info("conceptual-taxonomy not installed; skipping abstraction discovery")
        return None

    data = normalize_analysis_dict(analysis)
    conceptual = data.get("conceptualSchema") or {}
    physical = data.get("physicalMapping") or {}

    excluded = shard_family_members(physical)
    entities = [
        entity
        for entity in (conceptual.get("entities") or [])
        if isinstance(entity, dict) and entity.get("name") not in excluded
    ]
    bundle = {
        "conceptualSchema": {
            "entities": entities,
            "relationships": conceptual.get("relationships") or [],
        },
        "physicalMapping": physical,
    }

    containment: list[Any] = []
    measurements: list[Any] = []
    if measure_containment and db is not None:
        containment, measurements = measure_key_containment(db, physical)

    result = discover_abstractions(
        bundle,
        discriminators=build_discriminators(physical),
        key_containment=containment,
        measurements=measurements,
        namer=namer,
    )
    return result.to_json()


def merge_into_analysis(data: dict[str, Any], proposals: dict[str, Any] | None) -> dict[str, Any]:
    """Fold proposals into a normalized analysis dict, additively.

    Never rewrites an existing entity or subclass edge — abstraction discovery emits
    *proposals*, and arbitration belongs to the consumer that merges schema-derived taxonomy
    with taxonomy extracted from documents and from cross-ontology alignment.
    """
    if not proposals:
        return data

    conceptual = data.setdefault("conceptualSchema", {})
    entities = conceptual.setdefault("entities", [])
    known = {e.get("name") for e in entities if isinstance(e, dict)}

    for abstract in proposals.get("abstractClasses") or []:
        name = abstract.get("conceptualClass")
        if not isinstance(name, str) or name in known:
            continue
        entities.append(
            {
                "name": name,
                "labels": [name],
                # No physicalMapping entry by design — the absent mapping *is* the signal
                # that this class is not directly queryable (see quality.py).
                "abstract": True,
                "properties": [dict(p) for p in abstract.get("sharedProperties") or []],
                "source": abstract.get("source", "baseline"),
            }
        )
        known.add(name)

    conceptual["abstractClasses"] = list(proposals.get("abstractClasses") or [])
    # Merge rather than replace: the baseline may already have proposed subtypes from
    # secondary node labels (issue #28), and taxonomy discovery must not erase them.
    merged = list(conceptual.get("subClassOfProposals") or [])
    seen = {(pr.get("subClass"), pr.get("superClass")) for pr in merged if isinstance(pr, dict)}
    for pr in proposals.get("subClassOf") or []:
        if not isinstance(pr, dict):
            continue
        key = (pr.get("subClass"), pr.get("superClass"))
        if key not in seen:
            merged.append(pr)
            seen.add(key)
    conceptual["subClassOfProposals"] = merged
    return data


#: Public aliases — `discover`/`merge_into_analysis` are unhelpfully generic at
#: package scope, where they sit beside a dozen other analyzer entry points.
discover_abstractions_for_analysis = discover
merge_taxonomy_into_analysis = merge_into_analysis
