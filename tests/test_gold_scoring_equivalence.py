"""The gold comparison (``quality.py``) and the eval scorer share one set of helpers.

Before they did, each module had its own copy of the name normalization, the name-set
extraction and precision/recall/F1. The frozen copies below are those pre-merge
implementations; random inputs must give the same numbers from the shared code,
including ``compute_gold_comparison``'s rounding to four places and the eval scorer's
unrounded values.
"""

from __future__ import annotations

import random
from typing import Any

from schema_analyzer.eval.generator import PhysicalVariant
from schema_analyzer.eval.scoring import (
    expected_mapping_from_domain,
    score_against_domain,
    score_domain_range,
    score_mapping_style,
)
from schema_analyzer.quality import compute_gold_comparison
from schema_analyzer.utils import singularize

# --- Frozen pre-merge implementations -------------------------------------------------


def _old_norm(s: str) -> str:
    x = "".join(ch.lower() for ch in s if ch.isalnum() or ch in ("_", "-")).replace("-", "_")
    return singularize(x)


def _old_name_set(items: Any, key: str) -> set[str]:
    out: set[str] = set()
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and isinstance(it.get(key), str) and it[key]:
                out.add(_old_norm(it[key]))
    return out


def _old_quality_entity_name_set(conceptual: dict[str, Any], *, include_labels: bool) -> set[str]:
    out: set[str] = set()
    for e in conceptual.get("entities", []) or []:
        if not isinstance(e, dict):
            continue
        if isinstance(e.get("name"), str) and e["name"]:
            out.add(_old_norm(e["name"]))
        if include_labels and isinstance(e.get("labels"), list):
            out.update(_old_norm(x) for x in e["labels"] if isinstance(x, str) and x)
    return out


def _old_prf(pred: set[str], truth: set[str], rounded: bool) -> dict[str, float]:
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
    if rounded:
        return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4)}
    return {"precision": p, "recall": r, "f1": f1}


def _old_compute_gold_comparison(conceptual: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    pred_entities = _old_quality_entity_name_set(conceptual, include_labels=True)
    truth_entities = _old_name_set(reference.get("entities"), "name")
    pred_rels = _old_name_set(conceptual.get("relationships"), "type")
    truth_rels = _old_name_set(reference.get("relationships"), "type")
    entities = _old_prf(pred_entities, truth_entities, rounded=True)
    relationships = _old_prf(pred_rels, truth_rels, rounded=True)
    return {
        "entities": entities,
        "relationships": relationships,
        "overlap": round((entities["f1"] + relationships["f1"]) / 2.0, 4),
        "referenceEntityCount": len(truth_entities),
        "referenceRelationshipCount": len(truth_rels),
    }


def _old_eval_entity_names(data: dict[str, Any], *, include_labels: bool = False) -> set[str]:
    ents = data.get("entities") or []
    out: list[str] = []
    if isinstance(ents, list):
        for e in ents:
            if isinstance(e, dict):
                n = e.get("name")
                if isinstance(n, str) and n:
                    out.append(n)
                if include_labels:
                    labels = e.get("labels")
                    if isinstance(labels, list):
                        out.extend(x for x in labels if isinstance(x, str))
    return {_old_norm(x) for x in out if isinstance(x, str) and x}


def _old_score_against_domain(domain_spec: dict[str, Any], conceptual: dict[str, Any]) -> dict[str, Any]:
    pred_e = _old_eval_entity_names(conceptual, include_labels=True)
    tru_e = _old_eval_entity_names(domain_spec)
    pred_r = _old_name_set(conceptual.get("relationships") or [], "type")
    tru_r = _old_name_set(domain_spec.get("relationships") or [], "type")
    return {
        "entities": _old_prf(pred_e, tru_e, rounded=False),
        "relationships": _old_prf(pred_r, tru_r, rounded=False),
        "counts": {
            "pred_entities": len(pred_e),
            "truth_entities": len(tru_e),
            "pred_relationship_types": len(pred_r),
            "truth_relationship_types": len(tru_r),
        },
    }


def _old_ok(exp: dict[str, Any], got: Any, single_key: str, single_style: str) -> bool:
    if not isinstance(got, dict):
        return False
    if got.get("style") != exp.get("style"):
        return False
    if exp["style"] == single_style:
        return got.get(single_key) == exp.get(single_key)
    return (
        got.get(single_key) == exp.get(single_key)
        and got.get("typeField") == exp.get("typeField")
        and str(got.get("typeValue")) == str(exp.get("typeValue"))
    )


# --- Random inputs --------------------------------------------------------------------

_NAMES = [
    "User",
    "users",
    "Post",
    "posts",
    "Order-Item",
    "order_items",
    "Category",
    "categories",
    "Bus",
    "",
    "  ",
    "AUTHORED",
    "authored",
    "Has-Tag",
]
_JUNK: list[Any] = [None, 1, 2.5, ["x"], {"k": "v"}]


def _name(rng: random.Random) -> Any:
    return rng.choice(_JUNK) if rng.random() < 0.1 else rng.choice(_NAMES)


def _items(rng: random.Random, key: str) -> Any:
    roll = rng.random()
    if roll < 0.05:
        return None
    if roll < 0.08:
        return {"User": {}}
    out: list[Any] = []
    for _ in range(rng.randint(0, 6)):
        if rng.random() < 0.08:
            out.append(rng.choice(_JUNK))
            continue
        item: dict[str, Any] = {key: _name(rng)} if rng.random() < 0.9 else {}
        if key == "name" and rng.random() < 0.4:
            item["labels"] = [_name(rng) for _ in range(rng.randint(0, 3))] if rng.random() < 0.9 else "User"
        if key == "type":
            for k in rng.sample(["from", "to", "fromEntity", "toEntity"], rng.randint(0, 4)):
                item[k] = _name(rng)
        out.append(item)
    return out


def _schema(rng: random.Random) -> dict[str, Any]:
    return {"entities": _items(rng, "name"), "relationships": _items(rng, "type")}


def test_compute_gold_comparison_matches_pre_merge_implementation():
    rng = random.Random(20261123)
    for _ in range(2000):
        conceptual, reference = _schema(rng), _schema(rng)
        assert compute_gold_comparison(conceptual, reference) == _old_compute_gold_comparison(conceptual, reference)


def test_eval_scorer_matches_pre_merge_implementation():
    rng = random.Random(7)
    for _ in range(2000):
        domain, conceptual = _schema(rng), _schema(rng)
        assert score_against_domain(domain, conceptual) == _old_score_against_domain(domain, conceptual)


def test_domain_range_scoring_still_unrounded():
    # One of three: an unrounded 1/3 shows the eval scorer did not pick up the gold rounding.
    domain = {
        "relationships": [
            {"type": "A", "from": "X", "to": "Y"},
            {"type": "B", "from": "X", "to": "Y"},
            {"type": "C", "from": "X", "to": "Y"},
        ]
    }
    conceptual = {"relationships": [{"type": "A", "fromEntity": "X", "toEntity": "Y"}]}
    s = score_domain_range(domain, conceptual)
    assert s["recall"] == 1 / 3
    assert compute_gold_comparison(conceptual, domain)["relationships"]["recall"] == 0.3333


def test_mapping_style_checker_matches_pre_merge_implementation():
    rng = random.Random(11)
    domain = {
        "entities": [{"name": n} for n in ("User", "Post")],
        "relationships": [{"type": "AUTHORED"}, {"type": "LIKES"}],
    }
    variants = [
        PhysicalVariant(
            name="a", entity_style="COLLECTION", rel_style="DEDICATED_COLLECTION", entity_collection_prefix="p_"
        ),
        PhysicalVariant(name="b", entity_style="GENERIC_WITH_TYPE", rel_style="GENERIC_WITH_TYPE"),
    ]
    values = [
        "users",
        "p_users",
        "p_posts",
        "entities",
        "relationships",
        "authored",
        "type",
        "relation",
        "User",
        "AUTHORED",
        None,
        1,
    ]
    styles = ["COLLECTION", "LABEL", "DEDICATED_COLLECTION", "GENERIC_WITH_TYPE", None]
    for _ in range(2000):
        variant = rng.choice(variants)
        expected = expected_mapping_from_domain(domain, variant)

        def guess(exp: dict[str, Any], keys: tuple[str, ...]) -> Any:
            roll = rng.random()
            if roll < 0.1:
                return rng.choice(_JUNK)
            if roll < 0.6:  # the expected mapping, maybe with one key changed or one extra key
                got = dict(exp)
                if rng.random() < 0.7:
                    got[rng.choice(("style", *keys))] = rng.choice(values + styles)
                return got
            return {"style": rng.choice(styles), **{k: rng.choice(values) for k in keys if rng.random() < 0.8}}

        ent_keys = ("collectionName", "typeField", "typeValue")
        rel_keys = ("edgeCollectionName", "typeField", "typeValue")
        mapping = {
            "entities": {e: guess(exp, ent_keys) for e, exp in expected["entities"].items()},
            "relationships": {r: guess(exp, rel_keys) for r, exp in expected["relationships"].items()},
        }
        ent_ok = sum(
            _old_ok(exp, mapping["entities"].get(et), "collectionName", "COLLECTION")
            for et, exp in expected["entities"].items()
        )
        rel_ok = sum(
            _old_ok(exp, mapping["relationships"].get(rt), "edgeCollectionName", "DEDICATED_COLLECTION")
            for rt, exp in expected["relationships"].items()
        )
        s = score_mapping_style(domain, mapping, variant)
        assert (s["entities"]["ok"], s["relationships"]["ok"]) == (ent_ok, rel_ok)
