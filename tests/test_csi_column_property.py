"""CSI ``columns[].property``: a relational column names the property it feeds (issue #42)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from schema_analyzer.csi import from_csi, to_csi, validate_csi
from schema_analyzer.errors import SchemaAnalyzerError
from schema_analyzer.tool import run_tool
from schema_analyzer.tool_contract_v1 import validate_response_v1

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_0_20_0 = Path(__file__).resolve().parent / "fixtures" / "csi.schema.v0.20.0.json"
BASE = "https://pub.example/csi/crm"

ANALYSIS: dict[str, Any] = {
    "conceptualSchema": {
        "entities": [{"name": "Customer", "properties": [{"name": "full_name"}, {"name": "email"}]}],
        "relationships": [{"type": "placed", "fromEntity": "Customer", "toEntity": "Customer"}],
        "properties": [],
    },
    "physicalMapping": {
        "entities": {
            "Customer": {
                "style": "COLLECTION",
                "collectionName": "customer",
                "relation": {"kind": "table", "schema": "sales", "name": "customer"},
                "properties": {"full_name": {"field": "name"}, "email": {"field": "email"}},
                "columns": [
                    {"name": "cust_full_name", "sqlType": "varchar(200)", "property": "full_name"},
                    {"name": "email", "sqlType": "varchar(255)", "property": "email"},
                    {"name": "legacy_code", "sqlType": "char(4)"},
                ],
            },
        },
        "relationships": {
            "placed": {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": "placed",
                "relation": {"kind": "table", "schema": "sales", "name": "customer_order"},
                "properties": {"at": {"field": "placed_at"}},
                "columns": [{"name": "placed_ts", "sqlType": "timestamp", "property": "at"}],
            },
        },
    },
}


def _analysis() -> dict[str, Any]:
    return copy.deepcopy(ANALYSIS)


def _columns(doc: dict[str, Any], kind: str, name: str) -> list[dict[str, Any]]:
    return doc["arangoPhysicalMapping"][kind][name]["columns"]


def test_an_entity_column_reference_is_valid():
    doc = to_csi(_analysis())
    assert validate_csi(doc) == []


def test_a_relationship_column_reference_is_valid():
    doc = to_csi(_analysis())
    assert _columns(doc, "relationships", "placed") == [{"name": "placed_ts", "sqlType": "timestamp", "property": "at"}]
    assert validate_csi(doc) == []


def test_a_column_without_property_claims_no_link():
    doc = to_csi(_analysis())
    assert "property" not in _columns(doc, "entities", "Customer")[2]
    assert validate_csi(doc) == []


@pytest.mark.parametrize("kind, name", [("entities", "Customer"), ("relationships", "placed")])
def test_a_dangling_reference_is_refused_by_the_validator(kind, name):
    doc = to_csi(_analysis())
    _columns(doc, kind, name)[0]["property"] = "nothing"
    assert validate_csi(doc) == [
        f"arangoPhysicalMapping.{kind}.{name}.columns[0].property 'nothing' names no property in properties"
    ]


@pytest.mark.parametrize("kind, name", [("entities", "Customer"), ("relationships", "placed")])
def test_to_csi_refuses_to_write_a_dangling_reference(kind, name):
    analysis = _analysis()
    analysis["physicalMapping"][kind][name]["columns"][0]["property"] = "nothing"
    with pytest.raises(SchemaAnalyzerError, match="dangling column references") as err:
        to_csi(analysis)
    assert err.value.code == "INVALID_REQUEST"


MALFORMED = pytest.mark.parametrize("bad", ["", None, 7, ["full_name"], {"name": "full_name"}])
BOTH_KINDS = pytest.mark.parametrize("kind, name", [("entities", "Customer"), ("relationships", "placed")])


@MALFORMED
@BOTH_KINDS
def test_a_malformed_reference_is_refused_by_the_validator(kind, name, bad):
    doc = to_csi(_analysis())
    _columns(doc, kind, name)[0]["property"] = bad
    issues = validate_csi(doc)
    assert f"arangoPhysicalMapping.{kind}.{name}.columns[0].property must be a non-empty string, not {bad!r}" in issues


@MALFORMED
@BOTH_KINDS
def test_to_csi_refuses_to_write_a_malformed_reference(kind, name, bad):
    analysis = _analysis()
    analysis["physicalMapping"][kind][name]["columns"][0]["property"] = bad
    with pytest.raises(SchemaAnalyzerError, match="must be a non-empty string") as err:
        to_csi(analysis)
    assert err.value.code == "INVALID_REQUEST"


def _tool_csi(analysis: dict[str, Any]) -> dict[str, Any]:
    # The tool's request schema requires analysis metadata, unlike to_csi.
    analysis["metadata"] = {
        "confidence": 0.9,
        "timestamp": "2026-11-18T00:00:00Z",
        "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 1},
        "detectedPatterns": [],
        "analysisCompletedAt": "2026-11-18T00:00:00Z",
    }
    return run_tool({"contractVersion": "1", "operation": "csi", "input": {"analysis": analysis}})


def test_the_tool_passes_a_valid_reference_through():
    resp = _tool_csi(_analysis())
    assert resp["ok"] is True, resp
    assert _columns(resp["result"]["csi"], "entities", "Customer")[0]["property"] == "fullName"


@pytest.mark.parametrize("bad", ["", "nothing"])
def test_the_tool_refuses_a_bad_reference(bad):
    analysis = _analysis()
    analysis["physicalMapping"]["entities"]["Customer"]["columns"][0]["property"] = bad
    resp = _tool_csi(analysis)
    assert resp["ok"] is False, resp
    assert validate_response_v1(resp) == []
    assert resp["error"]["code"] == "INVALID_REQUEST"
    assert "columns[0].property" in resp["error"]["message"]


def test_a_renamed_property_carries_its_column_reference():
    doc = to_csi(_analysis())
    customer = doc["arangoPhysicalMapping"]["entities"]["Customer"]
    assert "fullName" in customer["properties"] and "full_name" not in customer["properties"]
    assert customer["columns"][0] == {"name": "cust_full_name", "sqlType": "varchar(200)", "property": "fullName"}


def test_a_relationship_reference_stays_in_step_with_its_properties():
    # Naming renames the relationship type, not its property keys; the reference keeps naming one.
    doc = to_csi(_analysis())
    placed = doc["arangoPhysicalMapping"]["relationships"]["placed"]
    assert _columns(doc, "relationships", "placed")[0]["property"] in placed["properties"]


def test_a_rename_override_reaches_the_reference():
    doc = to_csi(_analysis(), property_overrides={"full_name": "displayName"})
    assert _columns(doc, "entities", "Customer")[0]["property"] == "displayName"
    assert validate_csi(doc) == []


def test_raw_naming_keeps_references_as_given():
    doc = to_csi(_analysis(), owl_naming=False)
    assert _columns(doc, "entities", "Customer")[0]["property"] == "full_name"
    assert validate_csi(doc, naming=False) == []


def test_with_a_publisher_the_reference_resolves_to_a_property_id():
    doc = to_csi(_analysis(), publisher_uri=BASE)
    assert validate_csi(doc) == []
    for kind, name in (("entities", "Customer"), ("relationships", "placed")):
        spec = doc["arangoPhysicalMapping"][kind][name]
        for column in spec["columns"]:
            if "property" in column:
                assert spec["propertyIds"][column["property"]].startswith(BASE + "/")
    assert doc["physicalMapping"] == doc["arangoPhysicalMapping"]


def test_without_a_publisher_the_reference_still_works():
    doc = to_csi(_analysis())
    assert "propertyIds" not in doc["arangoPhysicalMapping"]["entities"]["Customer"]
    assert "physicalMapping" not in doc
    assert _columns(doc, "entities", "Customer")[0]["property"] == "fullName"


def test_a_publisher_changes_only_identity_keys_not_columns():
    plain = to_csi(_analysis())
    published = to_csi(_analysis(), publisher_uri=BASE)
    for kind, name in (("entities", "Customer"), ("relationships", "placed")):
        assert _columns(published, kind, name) == _columns(plain, kind, name)


def test_a_rename_keeps_existing_property_ids_and_entry_ids():
    first = to_csi(_analysis(), publisher_uri=BASE)
    spec = first["arangoPhysicalMapping"]["entities"]["Customer"]
    entry_id, prop_id = spec["id"], spec["propertyIds"]["fullName"]
    again = to_csi(from_csi(first), property_overrides={"fullName": "displayName"})
    renamed = again["arangoPhysicalMapping"]["entities"]["Customer"]
    assert renamed["id"] == entry_id
    assert renamed["propertyIds"]["displayName"] == prop_id
    assert renamed["columns"][0] == {"name": "cust_full_name", "sqlType": "varchar(200)", "property": "displayName"}
    assert validate_csi(again) == []


@pytest.mark.parametrize("publisher", [None, BASE])
def test_references_survive_a_round_trip(publisher):
    doc = to_csi(_analysis(), publisher_uri=publisher)
    again = to_csi(from_csi(doc))
    assert again["arangoPhysicalMapping"] == doc["arangoPhysicalMapping"]
    assert validate_csi(again) == []


@pytest.mark.parametrize(
    "kind, name, given, final",
    [("entities", "Customer", "full_name", "fullName"), ("relationships", "placed", "at", "at")],
)
def test_several_columns_may_name_one_property(kind, name, given, final):
    analysis = _analysis()
    analysis["physicalMapping"][kind][name]["columns"].append({"name": "extra", "sqlType": "text", "property": given})
    doc = to_csi(analysis)
    assert [c.get("property") for c in _columns(doc, kind, name)].count(final) == 2
    assert validate_csi(doc) == []


def test_a_document_without_references_is_unchanged_and_valid():
    analysis = _analysis()
    for kind in ("entities", "relationships"):
        for spec in analysis["physicalMapping"][kind].values():
            for column in spec["columns"]:
                column.pop("property", None)
    doc = to_csi(analysis)
    assert all("property" not in c for c in _columns(doc, "entities", "Customer"))
    assert validate_csi(doc) == []


def test_a_field_equal_to_a_column_name_is_not_turned_into_a_reference():
    analysis = _analysis()
    analysis["physicalMapping"]["entities"]["Customer"]["columns"][1].pop("property")
    doc = to_csi(analysis)
    assert "property" not in _columns(doc, "entities", "Customer")[1]


def test_output_with_references_is_valid_for_a_0_20_0_reader():
    validator = Draft202012Validator(json.loads(SCHEMA_0_20_0.read_text()))
    doc = to_csi(_analysis(), publisher_uri=BASE)
    assert [e.message for e in validator.iter_errors(doc)] == []


def test_identity_checks_are_unchanged_by_references():
    doc = to_csi(_analysis(), publisher_uri=BASE)
    doc["arangoPhysicalMapping"]["entities"]["Customer"]["propertyIds"]["ghost"] = BASE + "/x"
    doc["physicalMapping"] = copy.deepcopy(doc["arangoPhysicalMapping"])
    assert validate_csi(doc) == [
        "arangoPhysicalMapping.entities.Customer.propertyIds.ghost names no property in properties"
    ]


def test_both_schema_copies_define_the_reference_identically():
    docs = ROOT / "docs" / "csi" / "v1" / "csi.schema.json"
    bundled = ROOT / "schema_analyzer" / "csi" / "v1" / "csi.schema.json"
    assert docs.read_bytes() == bundled.read_bytes()
    column = json.loads(bundled.read_text())["$defs"]["RelationalColumn"]
    assert column["properties"]["property"] == {"type": "string", "minLength": 1}
    assert "property" not in column["required"]
