from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..quality import entity_name_set, name_set, normalize_name, prf
from .generator import entity_collection_name, relationship_collection_name


def _norm_rel_sig(rel_type: str, from_ent: str, to_ent: str) -> str:
    return f"{normalize_name(rel_type)}|{normalize_name(from_ent)}|{normalize_name(to_ent)}"


@dataclass(frozen=True)
class PRF:
    precision: float
    recall: float
    f1: float


def _prf(pred: set[str], truth: set[str]) -> PRF:
    return PRF(**prf(pred, truth))


def extract_predicted_entities(conceptual_schema: dict[str, Any]) -> set[str]:
    return entity_name_set(conceptual_schema, include_labels=True)


def extract_truth_entities(domain_spec: dict[str, Any]) -> set[str]:
    return entity_name_set(domain_spec, include_labels=False)


def extract_relationship_types(data: dict[str, Any]) -> set[str]:
    return name_set(data.get("relationships"), "type")


extract_predicted_relationship_types = extract_relationship_types
extract_truth_relationship_types = extract_relationship_types


def _extract_relationship_signatures(
    data: dict[str, Any],
    *,
    from_keys: tuple[str, ...] = ("from",),
    to_keys: tuple[str, ...] = ("to",),
) -> set[str]:
    rels = data.get("relationships") or []
    out: set[str] = set()
    if isinstance(rels, list):
        for r in rels:
            if not isinstance(r, dict):
                continue
            t = r.get("type")
            frm = next((r.get(k) for k in from_keys if r.get(k)), None)
            to = next((r.get(k) for k in to_keys if r.get(k)), None)
            if isinstance(t, str) and t and isinstance(frm, str) and frm and isinstance(to, str) and to:
                out.add(_norm_rel_sig(t, frm, to))
    return out


def extract_truth_relationship_signatures(domain_spec: dict[str, Any]) -> set[str]:
    return _extract_relationship_signatures(domain_spec)


def extract_predicted_relationship_signatures(conceptual_schema: dict[str, Any]) -> set[str]:
    return _extract_relationship_signatures(
        conceptual_schema,
        from_keys=("fromEntity", "from"),
        to_keys=("toEntity", "to"),
    )


def score_domain_range(domain_spec: dict[str, Any], conceptual_schema: dict[str, Any]) -> dict[str, Any]:
    """
    Scores correctness of relationship endpoints (domain/range) using (type, from, to) signatures.
    """
    pred = extract_predicted_relationship_signatures(conceptual_schema)
    tru = extract_truth_relationship_signatures(domain_spec)
    s = _prf(pred, tru)
    return {"precision": s.precision, "recall": s.recall, "f1": s.f1, "pred": len(pred), "truth": len(tru)}


def expected_mapping_from_domain(domain_spec: dict[str, Any], variant: Any) -> dict[str, Any]:
    """
    Construct the expected PhysicalMapping from our generator conventions + variant definition.
    """
    entities = [e.get("name") for e in (domain_spec.get("entities") or []) if isinstance(e, dict)]
    rels = [r.get("type") for r in (domain_spec.get("relationships") or []) if isinstance(r, dict)]

    expected_entities: dict[str, dict[str, Any]] = {}
    if getattr(variant, "entity_style", None) == "GENERIC_WITH_TYPE":
        col = getattr(variant, "entity_generic_collection", "entities")
        tf = getattr(variant, "entity_type_field", "type")
        for et in entities:
            if isinstance(et, str) and et:
                expected_entities[et] = {
                    "style": "LABEL",
                    "collectionName": col,
                    "typeField": tf,
                    "typeValue": et,
                }
    else:
        prefix = getattr(variant, "entity_collection_prefix", "")
        for et in entities:
            if isinstance(et, str) and et:
                expected_entities[et] = {"style": "COLLECTION", "collectionName": entity_collection_name(prefix, et)}

    expected_rels: dict[str, dict[str, Any]] = {}
    if getattr(variant, "rel_style", None) == "GENERIC_WITH_TYPE":
        col = getattr(variant, "rel_generic_collection", "relationships")
        tf = getattr(variant, "rel_type_field", "relation")
        for rt in rels:
            if isinstance(rt, str) and rt:
                expected_rels[rt] = {
                    "style": "GENERIC_WITH_TYPE",
                    "edgeCollectionName": col,
                    "typeField": tf,
                    "typeValue": rt,
                }
    else:
        prefix = getattr(variant, "rel_collection_prefix", "")
        for rt in rels:
            if isinstance(rt, str) and rt:
                expected_rels[rt] = {
                    "style": "DEDICATED_COLLECTION",
                    "edgeCollectionName": relationship_collection_name(prefix, rt),
                }

    return {"entities": expected_entities, "relationships": expected_rels}


def score_mapping_style(domain_spec: dict[str, Any], physical_mapping: dict[str, Any], variant: Any) -> dict[str, Any]:
    """
    Scores whether predicted mapping styles/fields match the expected mapping implied by the variant.
    """
    expected = expected_mapping_from_domain(domain_spec, variant)
    pred_entities = (physical_mapping.get("entities") or {}) if isinstance(physical_mapping, dict) else {}
    pred_rels = (physical_mapping.get("relationships") or {}) if isinstance(physical_mapping, dict) else {}

    def ok(exp: dict[str, Any], got: Any) -> bool:
        # Every key the expected mapping carries must match; ``typeValue`` is compared as text.
        return isinstance(got, dict) and all(
            (str(got.get(k)) == str(v)) if k == "typeValue" else got.get(k) == v for k, v in exp.items()
        )

    ent_total = len(expected["entities"])
    rel_total = len(expected["relationships"])
    ent_ok = sum(1 for et, exp in expected["entities"].items() if ok(exp, pred_entities.get(et)))
    rel_ok = sum(1 for rt, exp in expected["relationships"].items() if ok(exp, pred_rels.get(rt)))

    return {
        "entities": {"accuracy": (ent_ok / ent_total) if ent_total else 1.0, "ok": ent_ok, "total": ent_total},
        "relationships": {"accuracy": (rel_ok / rel_total) if rel_total else 1.0, "ok": rel_ok, "total": rel_total},
    }


def score_against_domain(domain_spec: dict[str, Any], conceptual_schema: dict[str, Any]) -> dict[str, Any]:
    pred_e = extract_predicted_entities(conceptual_schema)
    tru_e = extract_truth_entities(domain_spec)
    pred_r = extract_predicted_relationship_types(conceptual_schema)
    tru_r = extract_truth_relationship_types(domain_spec)

    ent = _prf(pred_e, tru_e)
    rel = _prf(pred_r, tru_r)
    return {
        "entities": {"precision": ent.precision, "recall": ent.recall, "f1": ent.f1},
        "relationships": {"precision": rel.precision, "recall": rel.recall, "f1": rel.f1},
        "counts": {
            "pred_entities": len(pred_e),
            "truth_entities": len(tru_e),
            "pred_relationship_types": len(pred_r),
            "truth_relationship_types": len(tru_r),
        },
    }
