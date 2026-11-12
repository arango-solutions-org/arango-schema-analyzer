"""Attributes that hold a list are marked `multiValued`, and CSI says so in 1.2 (CDF #220).

A reader that binds a stored array as one value finds no element and counts documents
instead of values. These tests pin what the snapshot records, which mappings the
analysis marks, and how CSI, OWL and the SPARQL export carry the marker.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from schema_analyzer.csi import (
    CSI_VERSION,
    CSI_VERSION_LISTS,
    CSI_VERSION_NESTED,
    from_csi,
    to_csi,
    validate_csi,
)
from schema_analyzer.list_values import apply_list_values, classify_list, list_fields_of, observed_list_fields
from schema_analyzer.redaction import RedactionOptions, build_field_name_map, redact_snapshot_for_egress
from schema_analyzer.snapshot import snapshot_physical_schema
from schema_analyzer.tool import run_tool

from .test_csi_1_1 import _doc, _errors
from .test_nested_fields import NestedFakeDB

DOCS = [
    {"_key": "1", "name": "a", "questionsServed": ["Q1", "Q2"], "orders": [{"sku": "A"}], "point": [52.1, 4.3]},
    {"_key": "2", "name": "b", "questionsServed": []},
    {"_key": "3", "name": "c", "questionsServed": "Q3", "tags": ["x", ["y"]]},
]


# ── Classifying a sampled value ─────────────────────────────────────────────


def test_classify_list():
    assert classify_list(["a", 1, None, True]) == "scalars"
    assert classify_list([]) == "scalars"
    assert classify_list([{"sku": "A"}]) == "structures"
    assert classify_list([["a"]]) == "structures"
    assert classify_list("a") is None
    assert classify_list({"a": [1]}) is None


def test_a_list_of_scalars_in_any_document_makes_a_list_field():
    # A scalar in one document and a list in another is a list; an array holding an
    # object or an array anywhere is not; system fields are never listed.
    docs = [*DOCS, {"_key": "4", "_from": ["x"], "labels": ["L"]}]
    assert list_fields_of(docs) == ["labels", "point", "questionsServed"]
    assert list_fields_of(docs, exclude={"labels"}) == ["point", "questionsServed"]


# ── The snapshot ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("depth", [0, 1])
def test_snapshot_records_list_fields(depth):
    snap = snapshot_physical_schema(NestedFakeDB({"documents": DOCS}), nested_field_depth=depth)
    (col,) = snap["collections"]
    assert col["observed_fields"]["list_fields"] == ["point", "questionsServed"]


def test_a_collection_without_lists_snapshots_as_before():
    snap = snapshot_physical_schema(NestedFakeDB({"people": [{"_key": "1", "name": "a"}]}))
    (col,) = snap["collections"]
    assert col["observed_fields"] == {"fields": ["name"]}


def test_a_geo_or_vector_index_field_holds_one_value():
    col = {
        "observed_fields": {"fields": ["point", "embedding", "tags"], "list_fields": ["embedding", "point", "tags"]},
        "indexes": [
            {"type": "primary", "fields": ["_key"]},
            {"type": "geo", "fields": ["point"], "geoJson": False},
            {"type": "vector", "fields": ["embedding"]},
        ],
    }
    assert observed_list_fields(col) == {"tags"}


def test_redaction_masks_list_field_names():
    snap = snapshot_physical_schema(NestedFakeDB({"documents": DOCS}))
    name_map = build_field_name_map(snap["collections"])
    out = redact_snapshot_for_egress(snap, RedactionOptions(mask_field_names=True), field_name_map=name_map)
    (col,) = out["collections"]
    assert col["observed_fields"]["list_fields"] == sorted(name_map[f] for f in ("point", "questionsServed"))


# ── The analysis ────────────────────────────────────────────────────────────


def _analyze(monkeypatch, samples: dict[str, list[dict[str, Any]]], **options: Any) -> dict[str, Any]:
    import schema_analyzer.tool as tool_mod

    monkeypatch.setattr(tool_mod, "_connect_db", lambda conn: NestedFakeDB(samples))
    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": {"url": "http://localhost:8529", "database": "x", "username": "u", "password": "p"},
            "analysisOptions": {"useCache": False, **options},
        }
    )
    assert resp["ok"] is True, resp
    return resp["result"]["analysis"]


def test_analyze_marks_the_list_attribute_and_nothing_else(monkeypatch):
    analysis = _analyze(monkeypatch, {"documents": DOCS}, entityStrategy="collection")
    (entity,) = analysis["physicalMapping"]["entities"].values()
    props = entity["properties"]
    assert props["questionsServed"] == {"field": "questionsServed", "multiValued": True}
    assert props["point"] == {"field": "point", "multiValued": True}  # no geo index here
    for name in ("name", "orders", "tags"):
        assert "multiValued" not in props[name]
    (concept,) = analysis["conceptualSchema"]["entities"]
    by_name = {p["name"]: p for p in concept["properties"]}
    # `type` names one element; the marker says there are several.
    assert by_name["questionsServed"] == {"name": "questionsServed", "type": "string", "multiValued": True}
    assert "multiValued" not in by_name["name"]


def test_analyze_without_lists_is_unchanged(monkeypatch):
    analysis = _analyze(monkeypatch, {"people": [{"_key": "1", "name": "a"}]})
    (entity,) = analysis["physicalMapping"]["entities"].values()
    assert entity["properties"] == {"name": {"field": "name"}}
    csi = to_csi(analysis)
    assert csi["csiVersion"] == CSI_VERSION


def test_apply_list_values_reads_the_stored_field_and_skips_paths():
    # An LLM-written mapping names the stored field under another property name.
    data = {
        "conceptualSchema": {
            "entities": [{"name": "Doc", "properties": [{"name": "questions"}, {"name": "city"}, {"name": "title"}]}],
            "relationships": [],
        },
        "physicalMapping": {
            "entities": {
                "Doc": {
                    "style": "COLLECTION",
                    "collectionName": "documents",
                    "properties": {
                        "questions": {"field": "questionsServed"},
                        "city": {"path": ["questionsServed", "city"]},
                        "title": {"field": "title"},
                    },
                }
            },
            "relationships": {},
        },
    }
    snapshot = {"collections": [{"name": "documents", "observed_fields": {"list_fields": ["questionsServed"]}}]}
    assert apply_list_values(data, snapshot) == 1
    props = data["physicalMapping"]["entities"]["Doc"]["properties"]
    assert props["questions"]["multiValued"] is True
    assert "multiValued" not in props["city"] and "multiValued" not in props["title"]
    concept = {p["name"]: p for p in data["conceptualSchema"]["entities"][0]["properties"]}
    assert concept["questions"]["multiValued"] is True
    assert "multiValued" not in concept["title"]


def test_apply_list_values_writes_nothing_without_lists():
    data = {"physicalMapping": {"entities": {"Doc": {"collectionName": "d", "properties": {"t": {"field": "t"}}}}}}
    before = copy.deepcopy(data)
    assert apply_list_values(data, {"collections": [{"name": "d", "observed_fields": {"fields": ["t"]}}]}) == 0
    assert data == before


# ── CSI 1.2 ─────────────────────────────────────────────────────────────────


def test_version_1_2_carries_multi_valued():
    assert _errors(_doc("1.2", {"tags": {"field": "tags", "multiValued": True}})) == []
    assert _errors(_doc("1.2", {"city": {"path": ["address", "city"]}})) == []  # 1.2 includes 1.1


@pytest.mark.parametrize("version", ["1", "1.1"])
def test_multi_valued_needs_version_1_2(version):
    # A reader of 1 or 1.1 would ignore the key and bind the whole list as one value.
    assert _errors(_doc(version, {"tags": {"field": "tags", "multiValued": True}}))
    assert _errors(_doc(version, {"tags": {"field": "tags", "multiValued": False}}))


def test_multi_valued_is_a_boolean():
    assert _errors(_doc("1.2", {"tags": {"field": "tags", "multiValued": "true"}}))


def test_to_csi_writes_1_2_only_when_a_mapping_is_multi_valued(monkeypatch):
    analysis = _analyze(monkeypatch, {"documents": DOCS}, entityStrategy="collection")
    csi = to_csi(analysis, source={"kind": "arango", "ref": "x"})
    assert csi["csiVersion"] == CSI_VERSION_LISTS == "1.2"
    assert validate_csi(csi) == []
    (entity,) = csi["arangoPhysicalMapping"]["entities"].values()
    assert entity["properties"]["questionsServed"]["multiValued"] is True
    back = from_csi(csi)
    (entity_back,) = back["physicalMapping"]["entities"].values()
    assert entity_back["properties"]["questionsServed"]["multiValued"] is True


def test_to_csi_keeps_1_1_for_path_without_lists():
    analysis = {
        "conceptualSchema": {"entities": [{"name": "Person", "properties": [{"name": "city"}]}], "relationships": []},
        "physicalMapping": {
            "entities": {
                "Person": {
                    "style": "COLLECTION",
                    "collectionName": "people",
                    "properties": {"city": {"path": ["address", "city"]}},
                }
            },
            "relationships": {},
        },
        "metadata": {},
    }
    assert to_csi(analysis)["csiVersion"] == CSI_VERSION_NESTED


# ── OWL and the SPARQL export ───────────────────────────────────────────────


def test_owl_and_sparql_exports_mark_the_list_attribute(monkeypatch):
    from schema_analyzer import export_mapping
    from schema_analyzer.owl_export import export_conceptual_model_as_jsonld, export_conceptual_model_as_owl_turtle

    analysis = _analyze(monkeypatch, {"documents": DOCS}, entityStrategy="collection")
    ttl = export_conceptual_model_as_owl_turtle(analysis)
    assert "phys:multiValued a owl:AnnotationProperty ." in ttl
    assert ':questionsServed phys:multiValued "true" .' in ttl
    assert ":name phys:multiValued" not in ttl

    nodes = {n["@id"]: n for n in export_conceptual_model_as_jsonld(analysis)["@graph"]}
    assert nodes["questionsServed"]["phys:multiValued"] == "true"
    assert "phys:multiValued" not in nodes["name"]

    props = {p["label"]: p for p in export_mapping(analysis, "sparql")["datatypeProperties"]}
    assert props["questionsServed"]["multiValued"] is True
    assert "multiValued" not in props["name"]


def test_owl_marks_a_shared_property_when_one_entity_holds_a_list():
    # Reading a scalar as a one-element list loses nothing, so one entity is enough.
    from schema_analyzer.owl_export import export_conceptual_model_as_owl_turtle

    analysis = {
        "conceptualSchema": {
            "entities": [
                {"name": "A", "properties": [{"name": "tags"}]},
                {"name": "B", "properties": [{"name": "tags"}]},
            ],
            "relationships": [],
        },
        "physicalMapping": {
            "entities": {
                "A": {"style": "COLLECTION", "collectionName": "a", "properties": {"tags": {"field": "tags"}}},
                "B": {
                    "style": "COLLECTION",
                    "collectionName": "b",
                    "properties": {"tags": {"field": "tags", "multiValued": True}},
                },
            },
            "relationships": {},
        },
    }
    assert ':tags phys:multiValued "true" .' in export_conceptual_model_as_owl_turtle(analysis)
