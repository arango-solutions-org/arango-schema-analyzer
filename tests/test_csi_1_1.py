"""CSI 1.1: `path` for values inside nested objects, and strict property mappings (issue #33).

The reader side was settled in arango-solutions-org/arango-sparql-py#84; these tests pin
the canonical schema and ``to_csi`` / ``from_csi`` to what that reader accepts and refuses.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from schema_analyzer.csi import (
    CSI_VERSION,
    CSI_VERSION_NESTED,
    PROPERTY_DESCRIPTIVE_KEYS,
    from_csi,
    load_csi_schema_v1,
    to_csi,
    validate_csi,
)
from schema_analyzer.tool import run_tool
from schema_analyzer.tool_contract_v1 import validate_response_v1

from .test_nested_fields import PEOPLE, NestedFakeDB


def _doc(version: str, prop_map: dict[str, Any], *, rel_props: dict[str, Any] | None = None) -> dict[str, Any]:
    rel: dict[str, Any] = {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "knows"}
    if rel_props is not None:
        rel["properties"] = rel_props
    return {
        "csiVersion": version,
        "conceptualModel": {"entities": [{"name": "Person"}], "relationships": []},
        "arangoPhysicalMapping": {
            "entities": {"Person": {"style": "COLLECTION", "collectionName": "people", "properties": prop_map}},
            "relationships": {"KNOWS": rel},
        },
        "provenance": {"producer": "test", "direction": "reverse", "source": {"kind": "arangodb"}},
    }


def _errors(doc: dict[str, Any]) -> list[str]:
    return validate_csi(doc, naming=False)


# ── The canonical schema ────────────────────────────────────────────────────


def test_version_1_1_with_path_is_valid():
    assert _errors(_doc("1.1", {"city": {"path": ["address", "city"], "indexed": True}})) == []


def test_version_1_1_without_path_is_valid():
    assert _errors(_doc("1.1", {"name": {"field": "name"}})) == []


@pytest.mark.parametrize("bad", ["2", "1.3", "", 1, 1.1, 1.2, None])
def test_other_versions_are_refused(bad):
    assert _errors(_doc(bad, {"name": {"field": "name"}}))


def test_a_version_1_document_must_not_use_path():
    assert _errors(_doc("1", {"city": {"path": ["address", "city"]}}))
    # Key presence, as the reader checks it: even a null path is refused in "1".
    assert _errors(_doc("1", {"city": {"path": None}}))


def test_field_and_path_together_are_refused():
    assert _errors(_doc("1.1", {"city": {"field": "address.city", "path": ["address", "city"]}}))


def test_neither_field_nor_path_is_allowed():
    # The reader then uses the property name as the attribute.
    assert _errors(_doc("1", {"name": {"indexed": True}})) == []


@pytest.mark.parametrize(
    "path",
    [[], ["address", ""], ["a`b"], ["a\\b"], ["a\x00b"], ["a\nb"], ["x" * 257], [1], "address.city"],
)
def test_bad_paths_are_refused(path):
    assert _errors(_doc("1.1", {"p": {"path": path}}))


def test_a_path_segment_may_contain_a_dot():
    # ["meta", "region.code"]: region.code is one attribute inside meta, still one level.
    assert _errors(_doc("1.1", {"p": {"path": ["meta", "region.code"]}})) == []


def test_unknown_property_mapping_keys_are_refused():
    errs = _errors(_doc("1", {"name": {"field": "name", "column": "NAME"}}))
    assert errs and any("column" in e for e in errs)


def test_descriptive_and_x_keys_are_allowed():
    entry = {
        "field": "name",
        "type": "string",
        "indexed": True,
        "unique": False,
        "required": True,
        "role": "name",
        "sentinelValues": ["N/A"],
        "numericLike": False,
        "sampleValues": ["Ann", 3],
        "multiValued": True,
        "x-vendor": {"anything": True},
    }
    assert set(entry) - {"field", "x-vendor"} == set(PROPERTY_DESCRIPTIVE_KEYS)
    # multiValued needs csiVersion 1.2 (test_csi_1_2.py); every other key is version 1.
    assert _errors(_doc("1.2", {"name": entry})) == []
    del entry["multiValued"]
    assert _errors(_doc("1", {"name": entry})) == []


def test_relationship_property_mappings_keep_the_additive_rule():
    # The reader checks entity property mappings only.
    assert _errors(_doc("1", {"name": {"field": "name"}}, rel_props={"since": {"field": "since", "note": "x"}})) == []


def test_the_rest_of_the_document_stays_additive():
    doc = _doc("1", {"name": {"field": "name"}})
    doc["arangoPhysicalMapping"]["entities"]["Person"]["someEnrichment"] = {"any": 1}
    doc["someTopLevelExtra"] = True
    assert _errors(doc) == []


def test_bundled_schema_is_draft_2020_12_valid():
    Draft202012Validator.check_schema(load_csi_schema_v1())


# ── to_csi / from_csi ───────────────────────────────────────────────────────


def _analysis(prop_map: dict[str, Any], *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "conceptualSchema": {
            "entities": [{"name": "Person", "labels": ["Person"], "properties": [{"name": n} for n in prop_map]}],
            "relationships": [],
            "properties": [],
        },
        "physicalMapping": {
            "entities": {"Person": {"style": "COLLECTION", "collectionName": "people", "properties": prop_map}},
            "relationships": {},
        },
        "metadata": {
            "confidence": 0.5,
            "timestamp": "2026-10-26T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 0},
            "detectedPatterns": [],
            **(metadata or {}),
        },
    }


def test_to_csi_writes_version_1_when_nothing_uses_path():
    csi = to_csi(_analysis({"name": {"field": "name"}, "address.city": {"field": "address.city"}}))
    assert csi["csiVersion"] == CSI_VERSION == "1"  # an analyzer upgrade never breaks "1" readers
    assert validate_csi(csi) == []


def test_to_csi_writes_version_1_1_when_a_mapping_uses_path():
    analysis = _analysis({"name": {"field": "name"}, "address.city": {"path": ["address", "city"]}})
    csi = to_csi(analysis)
    assert csi["csiVersion"] == CSI_VERSION_NESTED == "1.1"
    props = csi["arangoPhysicalMapping"]["entities"]["Person"]["properties"]
    # OWL naming renames the property but must not add `field` beside `path`.
    assert props["addressCity"] == {"path": ["address", "city"]}
    assert props["name"] == {"field": "name"}
    assert validate_csi(csi) == []


def test_to_csi_keeps_unknown_keys_under_x_prefix():
    analysis = _analysis({"name": {"field": "name", "description": "the name"}})
    before = copy.deepcopy(analysis)
    csi = to_csi(analysis)
    assert csi["arangoPhysicalMapping"]["entities"]["Person"]["properties"]["name"] == {
        "field": "name",
        "x-description": "the name",
    }
    assert analysis == before  # the caller's analysis is never mutated
    assert validate_csi(csi) == []


def test_to_csi_carries_unresolved_locations_into_provenance():
    conflict = {
        "collection": "people",
        "name": "address.city",
        "locations": [{"path": ["address", "city"]}, {"field": "address.city"}],
    }
    csi = to_csi(
        _analysis(
            {"name": {"field": "name"}}, metadata={"nestedFields": {"maxDepth": 1, "paths": 0, "conflicts": [conflict]}}
        )
    )
    assert csi["provenance"]["unresolvedFieldLocations"] == [conflict]
    assert csi["csiVersion"] == "1"


def test_path_round_trips_through_from_csi():
    analysis = _analysis({"address.city": {"path": ["address", "city"], "indexed": True}})
    back = from_csi(to_csi(analysis, owl_naming=False))
    assert back["physicalMapping"]["entities"]["Person"]["properties"]["address.city"] == {
        "path": ["address", "city"],
        "indexed": True,
    }


def test_from_csi_accepts_both_versions():
    for version in ("1", "1.1"):
        out = from_csi(_doc(version, {"name": {"field": "name"}}))
        assert out["physicalMapping"]["entities"]["Person"]["properties"] == {"name": {"field": "name"}}


# ── Through the contract: analyze (nestedFieldDepth) -> csi, as a consumer calls it ──


def test_nested_lookup_is_reachable_through_run_tool(monkeypatch):
    import schema_analyzer.tool as tool_mod

    db = NestedFakeDB({"people": PEOPLE})
    monkeypatch.setattr(tool_mod, "_connect_db", lambda conn: db)
    conn = {"url": "http://localhost:8529", "database": "x", "username": "u", "password": "p"}

    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": conn,
            "analysisOptions": {"nestedFieldDepth": 2, "useCache": False},
        }
    )
    assert resp["ok"] is True, resp
    assert validate_response_v1(resp) == []
    analysis = resp["result"]["analysis"]
    (person,) = [e for e in analysis["physicalMapping"]["entities"].values() if e.get("collectionName") == "people"]
    assert person["properties"]["address.city"] == {"path": ["address", "city"]}
    assert analysis["metadata"]["nestedFields"] == {
        "maxDepth": 2,
        "paths": 2,
        "conflicts": [],
        "arraysSkipped": [{"collection": "people", "path": ["orders"]}],
    }

    csi_resp = run_tool({"contractVersion": "1", "operation": "csi", "input": {"analysis": analysis}})
    assert csi_resp["ok"] is True, csi_resp
    csi = csi_resp["result"]["csi"]
    assert csi["csiVersion"] == "1.1"
    assert validate_csi(csi) == []

    snap = run_tool(
        {
            "contractVersion": "1",
            "operation": "snapshot",
            "connection": conn,
            "analysisOptions": {"nestedFieldDepth": 1},
        }
    )
    assert snap["ok"] is True, snap
    assert snap["result"]["snapshot"]["nested_field_depth"] == 1


def test_default_analyze_through_run_tool_is_unchanged(monkeypatch):
    import schema_analyzer.tool as tool_mod

    monkeypatch.setattr(tool_mod, "_connect_db", lambda conn: NestedFakeDB({"people": PEOPLE}))
    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": {"url": "http://localhost:8529", "database": "x", "username": "u", "password": "p"},
            "analysisOptions": {"useCache": False},
        }
    )
    assert resp["ok"] is True, resp
    analysis = resp["result"]["analysis"]
    assert analysis["metadata"]["nestedFields"] is None  # like every unused block (vci, ...)
    (person,) = analysis["physicalMapping"]["entities"].values()
    assert set(person["properties"]) == {"address", "name", "orders"}
    assert (
        run_tool({"contractVersion": "1", "operation": "csi", "input": {"analysis": analysis}})["result"]["csi"][
            "csiVersion"
        ]
        == "1"
    )


def test_out_of_range_depth_is_refused_by_the_contract():
    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": {"url": "http://localhost:8529", "database": "x", "username": "u", "password": "p"},
            "analysisOptions": {"nestedFieldDepth": 6},
        }
    )
    assert resp["ok"] is False


def test_owl_export_writes_attribute_path_for_a_nested_value():
    import json

    from schema_analyzer.owl_export import export_conceptual_model_as_jsonld, export_conceptual_model_as_owl_turtle

    analysis = _analysis(
        {"address.city": {"path": ["address", "city"]}, "full_name": {"field": "fullName"}, "nick": {"path": ["nk"]}}
    )
    ttl = export_conceptual_model_as_owl_turtle(analysis)
    assert 'phys:attributeName "fullName"' in ttl
    assert 'attributeName "address.city"' not in ttl  # no single stored attribute to name
    # Without a location, a reader (arango-sparql-py#84) would fall back to the local
    # name and read a top-level `address_city` that does not exist.
    assert ':address_city phys:attributePath "[\\"address\\", \\"city\\"]" .' in ttl
    assert "phys:attributePath a owl:AnnotationProperty ." in ttl
    assert ':nick phys:attributeName "nk" .' in ttl  # a one-level path is just a field

    nodes = {n["@id"]: n for n in export_conceptual_model_as_jsonld(analysis)["@graph"]}
    assert json.loads(nodes["address_city"]["phys:attributePath"]) == ["address", "city"]
    assert "phys:attributeName" not in nodes["address_city"]


def test_sparql_export_carries_the_path_of_a_nested_value():
    from schema_analyzer import export_mapping

    out = export_mapping(
        _analysis({"address.city": {"path": ["address", "city"]}, "name": {"field": "name"}}), "sparql"
    )
    props = {p["label"]: p for p in out["datatypeProperties"]}
    assert props["address.city"]["path"] == ["address", "city"]
    assert "path" not in props["name"]
    resp = {"contractVersion": "1", "operation": "export", "ok": True, "result": {"export": out}}
    assert validate_response_v1(resp) == []


def test_to_csi_never_overwrites_an_existing_x_key():
    csi = to_csi(
        _analysis({"name": {"field": "name", "x-description": "mine", "description": "llm"}}), owl_naming=False
    )
    assert csi["arangoPhysicalMapping"]["entities"]["Person"]["properties"]["name"] == {
        "field": "name",
        "x-description": "mine",
        "x-x-description": "llm",
    }
    assert validate_csi(csi) == []
