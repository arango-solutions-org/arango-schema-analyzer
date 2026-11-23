"""Structural + grounding quality metrics and a composite health score.

Implements PRD §3.12.3. The single ``metadata.confidence`` scalar tells a
consumer *how sure* the analyzer is, but not *why* or *where* the model is
weak. This module derives deterministic, schema-grounded signals from the
conceptual schema, the physical mapping, and the physical snapshot, plus a
normalized 0–100 ``healthScore`` that folds those signals together with
confidence.

All metrics are deterministic given the same inputs (no LLM, no clock, no
randomness). Every ratio degrades gracefully on empty inputs by reporting
``None`` rather than dividing by zero, and the composite score redistributes
weight away from any component that is not applicable (e.g. a schema with no
relationships is not penalized for having zero connectivity).
"""

from __future__ import annotations

from typing import Any

from .utils import singularize

# Composite health-score weights. Components that are not applicable for a
# given schema (see ``compute_health_score``) are dropped and the remaining
# weights are renormalized so the score always spans the full 0–100 range.
# ``gold`` is only present when a reference schema is supplied (PRD §3.12.3
# "composite … + optional gold overlap").
HEALTH_WEIGHTS = {
    "confidence": 0.40,
    "connectivity": 0.20,
    "consistency": 0.20,
    "grounding": 0.20,
    "gold": 0.20,
}


def _entity_names(conceptual: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for e in conceptual.get("entities", []) or []:
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"]:
            out.append(e["name"])
    return out


def _ratio(num: int, denom: int) -> float | None:
    if denom <= 0:
        return None
    return round(num / denom, 4)


def compute_structural_metrics(conceptual: dict[str, Any]) -> dict[str, Any]:
    """Connectivity, orphan ratio, property richness, and consistency flags.

    Derived purely from the conceptual schema.
    """
    entities = [e for e in (conceptual.get("entities", []) or []) if isinstance(e, dict)]
    relationships = [r for r in (conceptual.get("relationships", []) or []) if isinstance(r, dict)]
    entity_names = set(_entity_names(conceptual))
    entity_count = len(entities)
    rel_count = len(relationships)

    connected: set[str] = set()
    dangling = 0
    for r in relationships:
        frm = r.get("fromEntity") if isinstance(r.get("fromEntity"), str) else None
        to = r.get("toEntity") if isinstance(r.get("toEntity"), str) else None
        endpoints = [x for x in (frm, to) if x]
        for ep in endpoints:
            if ep in entity_names:
                connected.add(ep)
        # A relationship is dangling if either declared endpoint is missing
        # from the entity set, or an endpoint was not declared at all.
        if not frm or not to or frm not in entity_names or to not in entity_names:
            dangling += 1

    with_props = 0
    total_props = 0
    for e in entities:
        props = e.get("properties")
        n = len(props) if isinstance(props, list) else 0
        total_props += n
        if n > 0:
            with_props += 1

    orphan_count = entity_count - len(connected)

    return {
        "entityCount": entity_count,
        "relationshipCount": rel_count,
        "connectedEntityCount": len(connected),
        "connectedEntityRatio": _ratio(len(connected), entity_count),
        "orphanEntityCount": orphan_count,
        "orphanEntityRatio": _ratio(orphan_count, entity_count),
        "entitiesWithPropertiesRatio": _ratio(with_props, entity_count),
        "avgPropertiesPerEntity": round(total_props / entity_count, 4) if entity_count else None,
        "danglingRelationshipCount": dangling,
        "danglingRelationshipRatio": _ratio(dangling, rel_count),
    }


def _mapping_collection_names(physical_mapping: dict[str, Any]) -> list[str]:
    names: list[str] = []
    entities = physical_mapping.get("entities")
    if isinstance(entities, dict):
        for entry in entities.values():
            if isinstance(entry, dict) and isinstance(entry.get("collectionName"), str):
                names.append(entry["collectionName"])
    relationships = physical_mapping.get("relationships")
    if isinstance(relationships, dict):
        for entry in relationships.values():
            if isinstance(entry, dict) and isinstance(entry.get("edgeCollectionName"), str):
                names.append(entry["edgeCollectionName"])
    return names


def compute_grounding_metrics(
    conceptual: dict[str, Any],
    physical_mapping: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Faithfulness of the mapping to the physical snapshot.

    Checks that every collection the mapping references actually exists in the
    snapshot, and that every conceptual entity has a mapping entry.
    """
    snapshot_names = {
        c.get("name")
        for c in (snapshot.get("collections", []) or [])
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    }

    mapped = _mapping_collection_names(physical_mapping)
    grounded = [n for n in mapped if n in snapshot_names]
    ungrounded = sorted({n for n in mapped if n not in snapshot_names})

    entity_names = _entity_names(conceptual)
    mapping_entities = physical_mapping.get("entities")
    mapped_entity_keys = set(mapping_entities.keys()) if isinstance(mapping_entities, dict) else set()

    # A synthesized abstract class has no physical mapping *by design* — the absent mapping
    # is precisely what marks it as not directly queryable (PRD §6.3). Counting it as
    # ungrounded would penalise the analyzer for discovering a taxonomy.
    abstract_names = {
        e.get("name") for e in (conceptual.get("entities") or []) if isinstance(e, dict) and e.get("abstract") is True
    }
    unmapped_entities = sorted(n for n in entity_names if n not in mapped_entity_keys and n not in abstract_names)

    return {
        "mappedCollectionCount": len(mapped),
        "groundedCollectionCount": len(grounded),
        "mappingGroundingRatio": _ratio(len(grounded), len(mapped)),
        "ungroundedCollections": ungrounded,
        "unmappedEntityCount": len(unmapped_entities),
        "unmappedEntities": unmapped_entities,
    }


def normalize_name(s: str) -> str:
    """*s* lowercased, stripped to ``[a-z0-9_]`` (``-`` becomes ``_``) and singularized.

    The one name normalization for gold comparison here and for the eval scorer
    (``eval/scoring.py``), so ``Users`` matches ``user`` in both.
    """
    x = "".join(ch.lower() for ch in s if ch.isalnum() or ch in ("_", "-")).replace("-", "_")
    return singularize(x)


def name_set(items: Any, key: str) -> set[str]:
    """The normalized non-empty string ``key`` values of the dicts in the list *items*."""
    out: set[str] = set()
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and isinstance(it.get(key), str) and it[key]:
                out.add(normalize_name(it[key]))
    return out


def entity_name_set(data: dict[str, Any], *, include_labels: bool) -> set[str]:
    """The normalized entity names in *data* (plus each entity's ``labels`` if asked)."""
    out: set[str] = set()
    entities = data.get("entities")
    for e in entities if isinstance(entities, list) else []:
        if not isinstance(e, dict):
            continue
        if isinstance(e.get("name"), str) and e["name"]:
            out.add(normalize_name(e["name"]))
        if include_labels and isinstance(e.get("labels"), list):
            out.update(normalize_name(x) for x in e["labels"] if isinstance(x, str) and x)
    return out


def prf(pred: set[str], truth: set[str], *, ndigits: int | None = None) -> dict[str, float]:
    """Precision / recall / F1 of ``pred`` against ``truth`` (gold).

    Degenerate cases: empty/empty is perfect; an empty gold set yields recall
    1.0 / precision 0.0; an empty prediction yields precision 1.0 / recall 0.0.
    ``ndigits`` rounds the three values (the eval scorer keeps them unrounded).
    """
    if not pred and not truth:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    if not pred:
        return {"precision": 1.0, "recall": 0.0, "f1": 0.0}
    if not truth:
        return {"precision": 0.0, "recall": 1.0, "f1": 0.0}
    tp = len(pred & truth)
    p = tp / len(pred)
    r = tp / len(truth)
    f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
    if ndigits is not None:
        p, r, f1 = round(p, ndigits), round(r, ndigits), round(f1, ndigits)
    return {"precision": p, "recall": r, "f1": f1}


def compute_gold_comparison(conceptual: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """Precision/recall/F1 of the conceptual schema vs a supplied gold reference.

    ``reference`` is a domain-pack-style dict ``{"entities": [{"name": ...}],
    "relationships": [{"type": ..., "from"/"fromEntity": ..., "to"/"toEntity":
    ...}]}`` — e.g. a curated model or one parsed from a reference OWL/TTL.
    Names are normalized (lowercased, de-pluralized) before comparison so
    ``Users`` matches ``user``.

    Returns per-category PRF plus an ``overlap`` scalar (mean of entity F1 and
    relationship-type F1) suitable for folding into the health score.
    """
    pred_entities = entity_name_set(conceptual, include_labels=True)
    truth_entities = name_set(reference.get("entities"), "name")

    pred_rels = name_set(conceptual.get("relationships"), "type")
    truth_rels = name_set(reference.get("relationships"), "type")

    entities = prf(pred_entities, truth_entities, ndigits=4)
    relationships = prf(pred_rels, truth_rels, ndigits=4)
    overlap = round((entities["f1"] + relationships["f1"]) / 2.0, 4)

    return {
        "entities": entities,
        "relationships": relationships,
        "overlap": overlap,
        "referenceEntityCount": len(truth_entities),
        "referenceRelationshipCount": len(truth_rels),
    }


def compute_health_score(
    structural: dict[str, Any],
    grounding: dict[str, Any],
    confidence: float,
    gold: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fold the deterministic signals + confidence into a 0–100 health score.

    Components:

    * ``confidence`` — the analyzer's own scalar (always present).
    * ``connectivity`` — ``1 - orphanEntityRatio`` (only when entities and
      relationships both exist).
    * ``consistency`` — ``1 - danglingRelationshipRatio`` (only when
      relationships exist).
    * ``grounding`` — ``mappingGroundingRatio`` (only when the mapping
      references at least one collection).

    Non-applicable components are dropped and the remaining weights are
    renormalized, so e.g. a relationship-free reference schema is scored only
    on confidence + grounding rather than being dragged to zero.
    """
    components: dict[str, float] = {"confidence": max(0.0, min(1.0, confidence))}

    orphan_ratio = structural.get("orphanEntityRatio")
    if structural.get("relationshipCount", 0) > 0 and isinstance(orphan_ratio, (int, float)):
        components["connectivity"] = 1.0 - float(orphan_ratio)

    dangling_ratio = structural.get("danglingRelationshipRatio")
    if structural.get("relationshipCount", 0) > 0 and isinstance(dangling_ratio, (int, float)):
        components["consistency"] = 1.0 - float(dangling_ratio)

    grounding_ratio = grounding.get("mappingGroundingRatio")
    if grounding.get("mappedCollectionCount", 0) > 0 and isinstance(grounding_ratio, (int, float)):
        components["grounding"] = float(grounding_ratio)

    if gold is not None and isinstance(gold.get("overlap"), (int, float)):
        components["gold"] = max(0.0, min(1.0, float(gold["overlap"])))

    used_weight = sum(HEALTH_WEIGHTS[name] for name in components)
    if used_weight <= 0:
        score = 0
    else:
        weighted = sum(HEALTH_WEIGHTS[name] * value for name, value in components.items())
        score = round(100 * weighted / used_weight)

    return {
        "score": int(score),
        "components": {name: round(value, 4) for name, value in sorted(components.items())},
    }


def build_quality_block(
    conceptual: dict[str, Any],
    physical_mapping: dict[str, Any],
    snapshot: dict[str, Any],
    confidence: float,
    reference: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Compute the full quality block and the scalar health score.

    Returns ``(quality_metrics, health_score)`` where ``quality_metrics`` is a
    JSON-safe dict with ``structural``, ``grounding``, ``healthScoreComponents``
    (and, when ``reference`` is supplied, ``gold``) keys, and ``health_score``
    is the 0–100 integer. When a gold ``reference`` is given, its overlap folds
    into the health score as an additional weighted component.
    """
    structural = compute_structural_metrics(conceptual)
    grounding = compute_grounding_metrics(conceptual, physical_mapping, snapshot)
    gold = compute_gold_comparison(conceptual, reference) if isinstance(reference, dict) and reference else None
    health = compute_health_score(structural, grounding, confidence, gold=gold)
    quality_metrics: dict[str, Any] = {
        "structural": structural,
        "grounding": grounding,
        "healthScoreComponents": health["components"],
    }
    if gold is not None:
        quality_metrics["gold"] = gold
    return quality_metrics, health["score"]
