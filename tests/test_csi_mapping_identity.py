"""Issue #42: ``physicalMapping`` as an equal alias, relational source details, and
publisher-minted mapping ids — all additive on CSI version "1"."""

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

#: The CSI schema exactly as released in 0.20.0, committed so CI needs no git tags.
SCHEMA_0_20_0 = Path(__file__).resolve().parent / "fixtures" / "csi.schema.v0.20.0.json"
BASE = "https://pub.example/csi/crm"

ANALYSIS: dict[str, Any] = {
    "conceptualSchema": {
        "entities": [
            {"name": "Customer", "properties": [{"name": "email"}]},
            {"name": "Order", "properties": [{"name": "total"}]},
        ],
        "relationships": [
            {"type": "placed", "fromEntity": "Customer", "toEntity": "Order"},
            {"type": "ownedBy", "fromEntity": "Order", "toEntity": "Customer"},
        ],
        "properties": [],
    },
    "physicalMapping": {
        "entities": {
            "Customer": {
                "style": "COLLECTION",
                "collectionName": "customer",
                "relation": {"kind": "table", "schema": "sales", "name": "customer"},
                "columns": [{"name": "email", "sqlType": "varchar(255)", "nullable": False}],
            },
            "Order": {
                "style": "COLLECTION",
                "collectionName": "orders",
                "relation": {"kind": "view", "schema": "sales", "name": "v_orders"},
                "columns": [{"name": "total", "sqlType": "numeric(10,2)"}],
            },
        },
        "relationships": {
            "placed": {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": "placed",
                "properties": {"at": {"field": "placed_at"}},
            },
            "ownedBy": {
                "style": "FOREIGN_KEY",
                "fromCollection": "orders",
                "fromFields": ["customer_id"],
                "toCollection": "customer",
                "toKeyFields": ["_key"],
                "method": "declared",
            },
        },
    },
    # The analysis metadata the tool contract requires (same fields as tests/test_csi.py).
    "metadata": {
        "confidence": 0.9,
        "timestamp": "2026-11-18T00:00:00Z",
        "analyzedCollectionCounts": {"documentCollections": 2, "edgeCollections": 1},
        "detectedPatterns": [],
        "analysisCompletedAt": "2026-11-18T00:00:00Z",
    },
}


def _all_ids(doc: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for specs in doc["arangoPhysicalMapping"].values():
        for spec in specs.values():
            out.add(spec["id"])
            out.update(spec.get("propertyIds", {}).values())
    out.update(r["reference"]["id"] for r in doc["conceptualModel"]["relationships"] if "reference" in r)
    return out


# ── Legacy compatibility ────────────────────────────────────────────────────


def test_default_output_has_no_alias_and_no_ids():
    csi = to_csi(ANALYSIS)
    assert "physicalMapping" not in csi
    assert "publisher" not in csi["provenance"]
    assert csi["csiVersion"] == "1"
    assert all("id" not in s and "propertyIds" not in s for s in csi["arangoPhysicalMapping"]["entities"].values())
    assert validate_csi(csi) == []


def test_relational_metadata_does_not_need_a_publisher():
    customer = to_csi(ANALYSIS)["arangoPhysicalMapping"]["entities"]["Customer"]
    assert customer["relation"] == {"kind": "table", "schema": "sales", "name": "customer"}
    assert customer["columns"][0]["sqlType"] == "varchar(255)"


def test_enhanced_output_is_valid_for_a_0_20_0_reader():
    validator = Draft202012Validator(json.loads(SCHEMA_0_20_0.read_text()))
    assert [e.message for e in validator.iter_errors(to_csi(ANALYSIS, publisher_uri=BASE))] == []


# ── Relational tables and views ─────────────────────────────────────────────


def test_tables_and_views_validate():
    csi = to_csi(ANALYSIS)
    assert csi["arangoPhysicalMapping"]["entities"]["Order"]["relation"]["kind"] == "view"
    assert validate_csi(csi) == []


@pytest.mark.parametrize(
    "relation, columns",
    [
        ({"kind": "procedure", "name": "x"}, None),
        ({"kind": "table"}, None),
        ({"kind": "table", "name": "x", "owner": "y"}, None),
        (None, [{"name": "email"}]),
    ],
)
@pytest.mark.parametrize("kind, name", [("entities", "Customer"), ("relationships", "placed")])
def test_bad_relational_metadata_is_refused(kind, name, relation, columns):
    csi = to_csi(ANALYSIS)
    spec = csi["arangoPhysicalMapping"][kind][name]
    if relation is not None:
        spec["relation"] = relation
    if columns is not None:
        spec["columns"] = columns
    assert validate_csi(csi) != []


def test_a_join_table_relationship_can_name_its_table():
    analysis = copy.deepcopy(ANALYSIS)
    placed = analysis["physicalMapping"]["relationships"]["placed"]
    placed["relation"] = {"kind": "table", "schema": "sales", "name": "customer_order"}
    placed["columns"] = [{"name": "placed_at", "sqlType": "timestamp"}]
    assert validate_csi(to_csi(analysis)) == []
    assert validate_csi(to_csi(analysis, publisher_uri=BASE)) == []


@pytest.mark.parametrize("relation, columns", [({"kind": "sequence", "nonsense": 1}, None), (None, "not-a-list")])
def test_arthur_s_relationship_examples_are_refused(relation, columns):
    csi = to_csi(ANALYSIS)
    spec = csi["arangoPhysicalMapping"]["relationships"]["placed"]
    if relation is not None:
        spec["relation"] = relation
    if columns is not None:
        spec["columns"] = columns
    assert validate_csi(csi) != []


# ── Alias ───────────────────────────────────────────────────────────────────


def test_enhanced_output_carries_an_equal_alias():
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    assert csi["physicalMapping"] == csi["arangoPhysicalMapping"]
    assert csi["csiVersion"] == "1"
    assert validate_csi(csi) == []


def test_alias_conflict_is_refused():
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    csi["physicalMapping"]["entities"]["Customer"]["collectionName"] = "other"
    assert "physicalMapping differs from arangoPhysicalMapping" in validate_csi(csi)
    with pytest.raises(ValueError):
        from_csi(csi)


def test_the_old_key_stays_required():
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    del csi["arangoPhysicalMapping"]
    assert validate_csi(csi) != []
    assert from_csi(csi)["physicalMapping"]["entities"]["Customer"]["collectionName"] == "customer"


def test_an_alias_without_ids_is_valid():
    csi = to_csi(ANALYSIS)
    csi["physicalMapping"] = copy.deepcopy(csi["arangoPhysicalMapping"])
    assert validate_csi(csi) == []


# ── Identity coverage ───────────────────────────────────────────────────────


def test_every_mapping_entry_gets_an_id():
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    pm = csi["arangoPhysicalMapping"]
    assert csi["provenance"]["publisher"] == {"uri": BASE}
    assert pm["entities"]["Customer"]["id"] == f"{BASE}/mapping/entity/Customer"
    assert pm["entities"]["Customer"]["propertyIds"] == {"email": f"{BASE}/mapping/entity/Customer/property/email"}
    assert pm["relationships"]["placed"]["id"] == f"{BASE}/mapping/relationship/placed"
    assert pm["relationships"]["placed"]["propertyIds"] == {"at": f"{BASE}/mapping/relationship/placed/property/at"}
    (owned,) = [r for r in csi["conceptualModel"]["relationships"] if r["type"] == "ownedBy"]
    assert owned["reference"]["id"] == f"{BASE}/mapping/relationship/ownedBy/reference"


@pytest.mark.parametrize("drop", ["entity", "property", "relationship", "relProperty", "reference"])
def test_partial_coverage_is_refused(drop):
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    pm = csi["arangoPhysicalMapping"]
    if drop == "entity":
        del pm["entities"]["Order"]["id"]
    elif drop == "property":
        del pm["entities"]["Order"]["propertyIds"]["total"]
    elif drop == "relationship":
        del pm["relationships"]["placed"]["id"]
    elif drop == "relProperty":
        del pm["relationships"]["placed"]["propertyIds"]
    else:
        for r in csi["conceptualModel"]["relationships"]:
            r.get("reference", {}).pop("id", None)
    csi["physicalMapping"] = copy.deepcopy(pm)
    assert any("has no mapping id" in i for i in validate_csi(csi))


def test_a_property_id_for_no_property_is_refused():
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    csi["arangoPhysicalMapping"]["entities"]["Order"]["propertyIds"]["ghost"] = f"{BASE}/x"
    csi["physicalMapping"] = copy.deepcopy(csi["arangoPhysicalMapping"])
    assert any("names no property" in i for i in validate_csi(csi))


@pytest.mark.parametrize("stray", [f"{BASE}/x", ""])
def test_a_stray_property_id_alone_claims_identity(stray):
    csi = to_csi(ANALYSIS)
    csi["arangoPhysicalMapping"]["entities"]["Order"]["propertyIds"] = {"ghost": stray}
    issues = validate_csi(csi)
    assert "mapping ids are present but provenance.publisher.uri is missing" in issues
    assert any("names no property" in i for i in issues)


def test_an_empty_id_alone_claims_identity():
    csi = to_csi(ANALYSIS)
    csi["arangoPhysicalMapping"]["entities"]["Order"]["id"] = ""
    issues = validate_csi(csi)
    assert "mapping ids are present but provenance.publisher.uri is missing" in issues
    assert any("Order has no mapping id" in i for i in issues)


def test_existing_property_ids_are_never_overwritten():
    analysis = copy.deepcopy(ANALYSIS)
    order = analysis["physicalMapping"]["entities"]["Order"]
    order["properties"] = {"total": {"field": "total"}}
    order["propertyIds"] = {"total": "", "ghost": f"{BASE}/stray"}
    before = copy.deepcopy(analysis)
    with pytest.raises(SchemaAnalyzerError) as exc:  # refused, neither replaced nor dropped
        to_csi(analysis, publisher_uri=BASE)
    assert exc.value.code == "INVALID_REQUEST"
    assert "propertyIds.total has no mapping id" in str(exc.value)
    assert "propertyIds.ghost names no property" in str(exc.value)
    assert analysis == before


def _remove_customer_email(analysis: dict[str, Any]) -> None:
    """Remove a published property from both layers, leaving its propertyIds entry behind."""
    analysis["physicalMapping"]["entities"]["Customer"]["properties"].pop("email")
    (customer,) = [e for e in analysis["conceptualSchema"]["entities"] if e["name"] == "Customer"]
    customer["properties"] = []


def test_a_removed_property_s_id_is_refused_not_dropped():
    """Arthur's case 2: a published property is removed but its propertyIds entry stays."""
    analysis = from_csi(to_csi(ANALYSIS, publisher_uri=BASE))
    _remove_customer_email(analysis)
    with pytest.raises(SchemaAnalyzerError) as exc:
        to_csi(analysis)
    assert exc.value.code == "INVALID_REQUEST"
    assert "arangoPhysicalMapping.entities.Customer.propertyIds.email names no property" in str(exc.value)
    assert analysis["physicalMapping"]["entities"]["Customer"]["propertyIds"]["email"].startswith(BASE)


# ── Publisher namespace and duplicates ──────────────────────────────────────


@pytest.mark.parametrize("bad", ["relative/id", f"{BASE}/x#frag", "https://other.example/csi/x"])
def test_bad_ids_are_refused(bad):
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    for key in ("arangoPhysicalMapping", "physicalMapping"):
        csi[key]["entities"]["Customer"]["id"] = bad
    assert validate_csi(csi) != []


def test_ids_without_a_publisher_are_refused():
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    del csi["provenance"]["publisher"]
    assert "mapping ids are present but provenance.publisher.uri is missing" in validate_csi(csi)


def test_duplicate_ids_are_refused():
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    for key in ("arangoPhysicalMapping", "physicalMapping"):
        csi[key]["entities"]["Order"]["id"] = csi[key]["entities"]["Customer"]["id"]
    assert any(i.startswith("duplicate mapping id") for i in validate_csi(csi))


def test_bad_publisher_uri_is_refused():
    with pytest.raises(SchemaAnalyzerError) as exc:
        to_csi(ANALYSIS, publisher_uri="not-a-uri")
    assert exc.value.code == "INVALID_REQUEST"
    assert "publisher_uri" in str(exc.value)


@pytest.mark.parametrize("bad", ["not-a-uri", 42])
def test_bad_publisher_uri_in_metadata_is_refused(bad):
    analysis = copy.deepcopy(ANALYSIS)
    analysis["metadata"]["publisherUri"] = bad
    with pytest.raises(SchemaAnalyzerError) as exc:
        to_csi(analysis)
    assert exc.value.code == "INVALID_REQUEST"
    assert "metadata.publisherUri" in str(exc.value)


def test_a_re_export_under_another_publisher_is_refused():
    """Arthur's case 1: carried ids are never re-minted; the caller is told both publishers."""
    other = "https://pub.example/csi/erp"
    analysis = from_csi(to_csi(ANALYSIS, publisher_uri=BASE))
    before = copy.deepcopy(analysis)
    with pytest.raises(SchemaAnalyzerError) as exc:
        to_csi(analysis, publisher_uri=other)
    assert exc.value.code == "INVALID_REQUEST"
    assert repr(BASE) in str(exc.value) and repr(other) in str(exc.value)
    assert "metadata.publisherUri" in str(exc.value)
    assert analysis == before
    assert validate_csi(to_csi(analysis)) == []  # the original publisher still exports


def test_carried_ids_without_publisher_metadata_are_still_refused():
    """With metadata.publisherUri gone, the identity check still refuses foreign ids."""
    analysis = from_csi(to_csi(ANALYSIS, publisher_uri=BASE))
    del analysis["metadata"]["publisherUri"]
    with pytest.raises(SchemaAnalyzerError) as exc:
        to_csi(analysis, publisher_uri="https://pub.example/csi/erp")
    assert exc.value.code == "INVALID_REQUEST"
    assert f"is not under publisher {'https://pub.example/csi/erp'!r}" in str(exc.value)


def _with_metadata_publisher(uri: Any) -> dict[str, Any]:
    analysis = copy.deepcopy(ANALYSIS)
    analysis["metadata"]["publisherUri"] = uri
    return analysis


def test_matching_explicit_and_metadata_publishers_proceed():
    csi = to_csi(_with_metadata_publisher(BASE + "/"), publisher_uri=BASE)
    assert csi == to_csi(ANALYSIS, publisher_uri=BASE)
    assert validate_csi(csi) == []


def test_metadata_publisher_alone_is_used():
    assert to_csi(_with_metadata_publisher(BASE)) == to_csi(ANALYSIS, publisher_uri=BASE)


def test_conflicting_publishers_are_refused_even_without_ids():
    other = "https://pub.example/csi/erp"
    with pytest.raises(SchemaAnalyzerError) as exc:
        to_csi(_with_metadata_publisher(BASE), publisher_uri=other)
    assert exc.value.code == "INVALID_REQUEST"
    assert repr(BASE) in str(exc.value) and repr(other) in str(exc.value)


@pytest.mark.parametrize(
    "explicit, in_metadata, field",
    [
        ("not-a-uri", BASE, "publisher_uri"),
        (BASE, "not-a-uri", "metadata.publisherUri"),
        (BASE, 42, "metadata.publisherUri"),
    ],
)
def test_a_malformed_publisher_is_refused_whichever_side_it_is_on(explicit, in_metadata, field):
    with pytest.raises(SchemaAnalyzerError) as exc:
        to_csi(_with_metadata_publisher(in_metadata), publisher_uri=explicit)
    assert exc.value.code == "INVALID_REQUEST"
    assert str(exc.value).startswith(f"{field} must be")


def test_no_publisher_keeps_the_legacy_document():
    csi = to_csi(ANALYSIS)
    assert "physicalMapping" not in csi and "publisher" not in csi["provenance"]
    assert csi == to_csi(from_csi(csi))


def test_two_documents_get_disjoint_ids():
    a = _all_ids(to_csi(ANALYSIS, publisher_uri=BASE))
    b = _all_ids(to_csi(ANALYSIS, publisher_uri="https://pub.example/csi/erp"))
    assert a and not a & b


def test_trailing_slash_on_the_publisher_is_ignored():
    assert to_csi(ANALYSIS, publisher_uri=BASE + "/") == to_csi(ANALYSIS, publisher_uri=BASE)


# ── Stability ───────────────────────────────────────────────────────────────


def test_repeated_exports_mint_the_same_ids():
    assert to_csi(ANALYSIS, publisher_uri=BASE) == to_csi(ANALYSIS, publisher_uri=BASE)


def test_reordering_and_unrelated_additions_keep_ids():
    before = to_csi(ANALYSIS, publisher_uri=BASE)
    other = copy.deepcopy(ANALYSIS)
    pm = other["physicalMapping"]
    pm["entities"] = dict(reversed(list(pm["entities"].items())))
    pm["entities"]["Invoice"] = {"style": "COLLECTION", "collectionName": "invoice"}
    other["conceptualSchema"]["entities"].append({"name": "Invoice"})
    after = to_csi(other, publisher_uri=BASE)
    assert _all_ids(before) <= _all_ids(after)


def test_a_rename_without_a_carried_id_mints_a_new_one():
    other = copy.deepcopy(ANALYSIS)
    other["physicalMapping"]["entities"]["Client"] = other["physicalMapping"]["entities"].pop("Customer")
    other["conceptualSchema"]["entities"][0]["name"] = "Client"
    ids = _all_ids(to_csi(other, publisher_uri=BASE))
    assert f"{BASE}/mapping/entity/Client" in ids
    assert f"{BASE}/mapping/entity/Customer" not in ids


def test_a_rename_keeps_a_carried_id():
    analysis = from_csi(to_csi(ANALYSIS, publisher_uri=BASE))
    pm = analysis["physicalMapping"]
    pm["entities"]["Client"] = pm["entities"].pop("Customer")
    analysis["conceptualSchema"]["entities"][0]["name"] = "Client"
    client = to_csi(analysis)["arangoPhysicalMapping"]["entities"]["Client"]
    assert client["id"] == f"{BASE}/mapping/entity/Customer"
    assert client["propertyIds"]["email"] == f"{BASE}/mapping/entity/Customer/property/email"


def test_a_property_rename_by_owl_naming_keeps_its_id():
    analysis = copy.deepcopy(ANALYSIS)
    analysis["conceptualSchema"]["entities"][0]["properties"] = [{"name": "email_address"}]
    customer = analysis["physicalMapping"]["entities"]["Customer"]
    customer["properties"] = {"email_address": {"field": "email_address"}}
    customer["propertyIds"] = {"email_address": f"{BASE}/kept"}
    out = to_csi(analysis, publisher_uri=BASE)["arangoPhysicalMapping"]["entities"]["Customer"]
    assert out["propertyIds"] == {"emailAddress": f"{BASE}/kept"}


def test_existing_ids_are_never_overwritten():
    analysis = copy.deepcopy(ANALYSIS)
    analysis["physicalMapping"]["entities"]["Customer"]["id"] = f"{BASE}/custom/1"
    analysis["physicalMapping"]["relationships"]["ownedBy"]["id"] = f"{BASE}/custom/2"
    csi = to_csi(analysis, publisher_uri=BASE)
    assert csi["arangoPhysicalMapping"]["entities"]["Customer"]["id"] == f"{BASE}/custom/1"
    (owned,) = [r for r in csi["conceptualModel"]["relationships"] if r["type"] == "ownedBy"]
    assert owned["reference"]["id"] == f"{BASE}/custom/2"
    assert validate_csi(csi) == []


# ── Round trip ──────────────────────────────────────────────────────────────


def test_from_csi_to_csi_keeps_publisher_ids_and_relational_metadata():
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    analysis = from_csi(csi)
    assert analysis["metadata"]["publisherUri"] == BASE
    owned_by = analysis["physicalMapping"]["relationships"]["ownedBy"]
    assert owned_by["id"] == f"{BASE}/mapping/relationship/ownedBy/reference"
    again = to_csi(analysis)
    assert again["provenance"]["publisher"] == {"uri": BASE}
    assert again["physicalMapping"] == again["arangoPhysicalMapping"] == csi["arangoPhysicalMapping"]
    assert again["conceptualModel"]["relationships"] == csi["conceptualModel"]["relationships"]


def test_legacy_round_trip_is_unchanged():
    csi = to_csi(ANALYSIS)
    assert "publisherUri" not in from_csi(csi)["metadata"]
    assert "physicalMapping" not in to_csi(from_csi(csi))


# ── Version compatibility ───────────────────────────────────────────────────


def test_path_still_needs_version_1_1():
    csi = to_csi(ANALYSIS, publisher_uri=BASE)
    for key in ("arangoPhysicalMapping", "physicalMapping"):
        csi[key]["entities"]["Customer"]["properties"]["email"] = {"path": ["a", "b"]}
    issues = validate_csi(csi)
    assert "physicalMapping differs from arangoPhysicalMapping" not in issues
    assert any("should not be valid" in i for i in issues)  # the csiVersion "1" gate refused `path`


def test_path_raises_the_version_with_ids_too():
    analysis = copy.deepcopy(ANALYSIS)
    analysis["physicalMapping"]["entities"]["Customer"]["properties"] = {"email": {"path": ["contact", "email"]}}
    csi = to_csi(analysis, publisher_uri=BASE)
    assert csi["csiVersion"] == "1.1"
    assert validate_csi(csi) == []


# ── Tool ────────────────────────────────────────────────────────────────────


def _csi_op(output_options: dict[str, Any] | None = None, analysis: dict[str, Any] = ANALYSIS) -> dict[str, Any]:
    req: dict[str, Any] = {"contractVersion": "1", "operation": "csi", "input": {"analysis": analysis}}
    if output_options is not None:
        req["outputOptions"] = output_options
    resp = run_tool(req)
    assert resp["ok"] is True, resp
    assert validate_response_v1(resp) == []
    return resp["result"]["csi"]


def _csi_op_error(analysis: dict[str, Any], output_options: dict[str, Any] | None = None) -> dict[str, Any]:
    req: dict[str, Any] = {"contractVersion": "1", "operation": "csi", "input": {"analysis": analysis}}
    if output_options is not None:
        req["outputOptions"] = output_options
    resp = run_tool(req)
    assert resp["ok"] is False, resp
    assert "result" not in resp
    assert validate_response_v1(resp) == []
    assert resp["error"]["code"] == "INVALID_REQUEST"
    return resp["error"]


def test_tool_refuses_a_re_export_under_another_publisher():
    analysis = {**ANALYSIS, **from_csi(to_csi(ANALYSIS, publisher_uri=BASE))}
    analysis["metadata"] = {**ANALYSIS["metadata"], "publisherUri": BASE}
    error = _csi_op_error(analysis, {"csiPublisherUri": "https://pub.example/csi/erp"})
    assert BASE in error["message"] and "https://pub.example/csi/erp" in error["message"]


def test_tool_refuses_a_removed_property_s_id():
    analysis = {**ANALYSIS, **from_csi(to_csi(ANALYSIS, publisher_uri=BASE))}
    analysis["metadata"] = {**ANALYSIS["metadata"], "publisherUri": BASE}
    _remove_customer_email(analysis)
    assert "propertyIds.email names no property" in _csi_op_error(analysis)["message"]


def test_tool_refuses_a_bad_publisher_uri_in_metadata():
    analysis = copy.deepcopy(ANALYSIS)
    analysis["metadata"]["publisherUri"] = "not a uri"
    assert "metadata.publisherUri" in _csi_op_error(analysis)["message"]


def test_tool_refuses_conflicting_publishers():
    other = "https://pub.example/csi/erp"
    error = _csi_op_error(_with_metadata_publisher(BASE), {"csiPublisherUri": other})
    assert BASE in error["message"] and other in error["message"]


def test_tool_refuses_a_malformed_metadata_publisher_beside_a_valid_option():
    error = _csi_op_error(_with_metadata_publisher("not a uri"), {"csiPublisherUri": BASE})
    assert error["message"].startswith("metadata.publisherUri must be")


def test_tool_accepts_matching_publishers():
    csi = _csi_op({"csiPublisherUri": BASE}, _with_metadata_publisher(BASE))
    assert csi == to_csi(ANALYSIS, publisher_uri=BASE)


def test_tool_without_publisher_is_unchanged():
    assert _csi_op() == to_csi(ANALYSIS)


def test_tool_with_publisher_emits_ids():
    csi = _csi_op({"csiPublisherUri": BASE})
    assert csi == to_csi(ANALYSIS, publisher_uri=BASE)
    assert validate_csi(csi) == []


def test_tool_refuses_a_bad_publisher_uri():
    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "csi",
            "input": {"analysis": ANALYSIS},
            "outputOptions": {"csiPublisherUri": "not a uri"},
        }
    )
    assert resp["ok"] is False
