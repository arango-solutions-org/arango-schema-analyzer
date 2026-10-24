"""Label arrays as the entity discriminator in LPG graphs (issue #28).

r2g's LPG layout puts every node in one collection with ``labels: [primary, *extra]``.
The primary label is the node's identity; a secondary label on every node of a primary
is an alias, one on only some is a subtype candidate, and one that rides on several
different primaries is a cross-cutting tag (provenance, tenant, status) — not a subtype.
"""

from schema_analyzer.baseline import infer_baseline_from_snapshot
from schema_analyzer.taxonomy import merge_taxonomy_into_analysis
from schema_analyzer.tool_contract_v1 import validate_response_v1
from schema_analyzer.type_detection import EDGE_FROM_TYPE_FIELDS, EDGE_TO_TYPE_FIELDS


def _snapshot(cooccurrence: dict, totals: dict) -> dict:
    """A one-collection LPG snapshot as the snapshot phase now records it."""
    return {
        "version": 2,
        "collections": [
            {
                "name": "nodes",
                "type": "document",
                "count": sum(totals.values()),
                "indexes": [],
                "candidate_type_fields": ["labels"],
                # Primary labels: arrays are grouped by their first element.
                "sample_field_value_counts": {"labels": [{"value": k, "count": v} for k, v in totals.items()]},
                "sample_field_distinct_counts": {"labels": len(totals)},
                "label_cooccurrence": {"labels": cooccurrence},
                "observed_fields": {"fields": ["labels", "name"]},
            }
        ],
        "graphs": [],
    }


def _entities(out: dict) -> dict:
    return {e["name"]: e for e in out["conceptualSchema"]["entities"]}


def test_primary_label_is_identity_and_a_universal_label_is_an_alias():
    out = infer_baseline_from_snapshot(
        _snapshot({"Account": {"Customer": 30}}, {"Account": 30, "Order": 40, "Product": 20})
    )
    pm = out["physicalMapping"]["entities"]
    assert {n: (m["style"], m["typeField"], m["typeValue"]) for n, m in pm.items()} == {
        "Account": ("LABEL", "labels", "Account"),
        "Order": ("LABEL", "labels", "Order"),
        "Product": ("LABEL", "labels", "Product"),
    }
    # Mirrors r2g's forward CSI: the extra label joins the conceptual labels; typeValue stays primary.
    assert _entities(out)["Account"]["labels"] == ["Account", "Customer"]
    assert "subClassOfProposals" not in out["conceptualSchema"]


def test_a_label_on_some_nodes_of_one_type_is_a_subtype_proposal():
    out = infer_baseline_from_snapshot(
        _snapshot({"Account": {"Customer": 30, "Premium": 10}}, {"Account": 30, "Order": 40})
    )
    account = _entities(out)["Account"]
    assert account["labels"] == ["Account", "Customer"]
    assert account["partialLabels"] == {"Premium": {"count": 10, "of": 30}}
    (proposal,) = out["conceptualSchema"]["subClassOfProposals"]
    assert (proposal["subClass"], proposal["superClass"], proposal["method"]) == (
        "Premium",
        "Account",
        "secondary_label",
    )


def test_a_label_cutting_across_types_is_a_facet_not_a_subtype():
    # Provenance tags: some nodes of *every* type carry a source label. Read as subtypes
    # they would make SourceCRM a subclass of Account, Order and Product at once.
    cooccurrence = {
        "Account": {"Premium": 10, "SourceCRM": 10, "SourceERP": 10},
        "Order": {"SourceCRM": 14, "SourceERP": 13},
        "Product": {"SourceCRM": 7},
    }
    out = infer_baseline_from_snapshot(_snapshot(cooccurrence, {"Account": 30, "Order": 40, "Product": 20}))
    ents = _entities(out)
    assert [p["subClass"] for p in out["conceptualSchema"]["subClassOfProposals"]] == ["Premium"]
    assert ents["Account"]["facetLabels"] == {
        "SourceCRM": {"count": 10, "of": 30},
        "SourceERP": {"count": 10, "of": 30},
    }
    assert set(ents["Product"]["facetLabels"]) == {"SourceCRM"}
    assert "partialLabels" not in ents["Order"]


def test_a_label_that_is_some_nodes_primary_is_not_proposed_as_a_subtype():
    # "Order" is an entity in its own right; seeing it as an extra label on a few
    # Accounts does not make Order a subclass of Account.
    out = infer_baseline_from_snapshot(_snapshot({"Account": {"Order": 3}}, {"Account": 30, "Order": 40}))
    assert "subClassOfProposals" not in out["conceptualSchema"]


def test_taxonomy_merge_keeps_label_derived_proposals():
    def data():
        return {
            "conceptualSchema": {
                "entities": [{"name": "Account"}],
                "relationships": [],
                "properties": [],
                "subClassOfProposals": [{"subClass": "Premium", "superClass": "Account", "method": "secondary_label"}],
            }
        }

    # Taxonomy discovery finds something else; the label-derived proposal must survive.
    merged = merge_taxonomy_into_analysis(
        data(), {"abstractClasses": [], "subClassOf": [{"subClass": "Savings", "superClass": "Account"}]}
    )
    pairs = [(p["subClass"], p["superClass"]) for p in merged["conceptualSchema"]["subClassOfProposals"]]
    assert pairs == [("Premium", "Account"), ("Savings", "Account")]

    # Both mechanisms proposing the same edge yields it once.
    merged = merge_taxonomy_into_analysis(
        data(), {"abstractClasses": [], "subClassOf": [{"subClass": "Premium", "superClass": "Account"}]}
    )
    assert len(merged["conceptualSchema"]["subClassOfProposals"]) == 1


def test_r2g_endpoint_label_fields_are_recognised():
    assert "fromLabels" in EDGE_FROM_TYPE_FIELDS and "toLabels" in EDGE_TO_TYPE_FIELDS
    assert EDGE_FROM_TYPE_FIELDS[0] == "_fromType"  # preference order unchanged


def test_response_schema_admits_subclass_proposals_and_abstract_classes():
    # Regression: conceptualSchema was closed, so taxonomy output (and these proposals)
    # failed response validation and the request returned INTERNAL_ERROR.
    analysis = {
        "conceptualSchema": {
            "entities": [{"name": "Account", "labels": ["Account"], "properties": []}],
            "relationships": [],
            "properties": [],
            "abstractClasses": [{"conceptualClass": "Party"}],
            "subClassOfProposals": [{"subClass": "Premium", "superClass": "Account"}],
        },
        "physicalMapping": {
            "entities": {
                "Account": {"style": "LABEL", "collectionName": "nodes", "typeField": "labels", "typeValue": "Account"}
            },
            "relationships": {},
        },
        "metadata": {
            "confidence": 0.1,
            "timestamp": "2026-10-12T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 0},
            "detectedPatterns": [],
        },
    }
    resp = {"contractVersion": "1", "operation": "analyze", "ok": True, "result": {"analysis": analysis}}
    assert validate_response_v1(resp) == []


def test_labels_strategy_maps_label_arrays_to_labels_style():
    snap = _snapshot({"Account": {"Customer": 30}}, {"Account": 30, "Order": 40})
    out = infer_baseline_from_snapshot(snap, multi_label=True)
    pm = out["physicalMapping"]["entities"]
    account = pm["Account"]
    assert (account["style"], account["collectionName"], account["labelsField"], account["typeValue"]) == (
        "LABELS",
        "nodes",
        "labels",
        "Account",
    )
    assert "typeField" not in account
    assert pm["Order"]["style"] == "LABELS"
    # Conceptual output is the same as under the default strategy.
    assert out["conceptualSchema"] == infer_baseline_from_snapshot(snap)["conceptualSchema"]


def test_labels_strategy_leaves_scalar_type_fields_as_label():
    # No sampled node with more than one label: nothing to gain from LABELS.
    out = infer_baseline_from_snapshot(_snapshot({}, {"Account": 30, "Order": 40}), multi_label=True)
    assert {m["style"] for m in out["physicalMapping"]["entities"].values()} == {"LABEL"}
