"""Structural diff between two analyses (PRD §3.13.3).

Consumers re-analyzing a database after a schema change need to know *what*
moved — which conceptual entities/relationships appeared or vanished, and which
physical mapping styles flipped (e.g. a concept that used to live in its own
collection now shares a generic collection with a type discriminator). This
module compares two ``AnalysisResult`` payloads (or their JSON dicts) and
returns a deterministic, JSON-safe diff.

It is intentionally schema-derived and side-effect free: no database, no LLM,
no clock. The OWL/Turtle exports of two analyses can be diffed by first running
each through the analyzer and diffing the results here.
"""

from __future__ import annotations

from typing import Any

from .utils import meta_get, normalize_analysis_dict


def _entities_by_name(conceptual: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in conceptual.get("entities", []) or []:
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"]:
            out[e["name"]] = e
    return out


def _relationships_by_type(conceptual: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in conceptual.get("relationships", []) or []:
        if isinstance(r, dict) and isinstance(r.get("type"), str) and r["type"]:
            out[r["type"]] = r
    return out


def _mapping_entries(physical_mapping: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    entries = physical_mapping.get(key)
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _entity_property_names(entity: dict[str, Any]) -> set[str]:
    props = entity.get("properties")
    names: set[str] = set()
    if isinstance(props, list):
        for p in props:
            if isinstance(p, dict) and isinstance(p.get("name"), str):
                names.add(p["name"])
    return names


def _style_changes(prev: dict[str, dict[str, Any]], curr: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for name in sorted(set(prev) & set(curr)):
        prev_style = prev[name].get("style")
        curr_style = curr[name].get("style")
        if prev_style != curr_style:
            changes.append({"name": name, "from": prev_style, "to": curr_style})
    return changes


def _added_removed(prev_keys: set[str], curr_keys: set[str]) -> tuple[list[str], list[str]]:
    return sorted(curr_keys - prev_keys), sorted(prev_keys - curr_keys)


def diff_analyses(
    previous: Any,
    current: Any,
) -> dict[str, Any]:
    """Compare two analyses and return added/removed/changed elements.

    The result has stable, sorted lists so two diffs of the same inputs are
    byte-identical. ``changed`` entities/relationships are those present in both
    analyses whose conceptual definition differs (property set or, for
    relationships, endpoints). Mapping-style flips are reported separately under
    ``mappingStyles``.
    """
    prev = normalize_analysis_dict(previous)
    curr = normalize_analysis_dict(current)

    prev_cs = prev.get("conceptualSchema") or {}
    curr_cs = curr.get("conceptualSchema") or {}
    prev_pm = prev.get("physicalMapping") or {}
    curr_pm = curr.get("physicalMapping") or {}

    prev_entities = _entities_by_name(prev_cs)
    curr_entities = _entities_by_name(curr_cs)
    ent_added, ent_removed = _added_removed(set(prev_entities), set(curr_entities))
    ent_changed: list[str] = []
    for name in sorted(set(prev_entities) & set(curr_entities)):
        if _entity_property_names(prev_entities[name]) != _entity_property_names(curr_entities[name]):
            ent_changed.append(name)

    prev_rels = _relationships_by_type(prev_cs)
    curr_rels = _relationships_by_type(curr_cs)
    rel_added, rel_removed = _added_removed(set(prev_rels), set(curr_rels))
    rel_changed: list[str] = []
    for rtype in sorted(set(prev_rels) & set(curr_rels)):
        p, c = prev_rels[rtype], curr_rels[rtype]
        if p.get("fromEntity") != c.get("fromEntity") or p.get("toEntity") != c.get("toEntity"):
            rel_changed.append(rtype)

    entity_style_changes = _style_changes(_mapping_entries(prev_pm, "entities"), _mapping_entries(curr_pm, "entities"))
    rel_style_changes = _style_changes(
        _mapping_entries(prev_pm, "relationships"), _mapping_entries(curr_pm, "relationships")
    )

    prev_health = _health_score(prev)
    curr_health = _health_score(curr)
    delta = curr_health - prev_health if prev_health is not None and curr_health is not None else None

    changed = bool(
        ent_added
        or ent_removed
        or ent_changed
        or rel_added
        or rel_removed
        or rel_changed
        or entity_style_changes
        or rel_style_changes
    )

    return {
        "changed": changed,
        "entities": {"added": ent_added, "removed": ent_removed, "changed": ent_changed},
        "relationships": {"added": rel_added, "removed": rel_removed, "changed": rel_changed},
        "mappingStyles": {"entities": entity_style_changes, "relationships": rel_style_changes},
        "healthScore": {"previous": prev_health, "current": curr_health, "delta": delta},
        "summary": {
            "entitiesAdded": len(ent_added),
            "entitiesRemoved": len(ent_removed),
            "entitiesChanged": len(ent_changed),
            "relationshipsAdded": len(rel_added),
            "relationshipsRemoved": len(rel_removed),
            "relationshipsChanged": len(rel_changed),
            "entityStyleChanges": len(entity_style_changes),
            "relationshipStyleChanges": len(rel_style_changes),
        },
    }


def _health_score(analysis: dict[str, Any]) -> int | None:
    meta = analysis.get("metadata")
    if not isinstance(meta, dict):
        return None
    score = meta_get(meta, "healthScore", "health_score")
    return int(score) if isinstance(score, (int, float)) else None
