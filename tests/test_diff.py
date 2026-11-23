"""Tests for the analysis diff (PRD §3.13.3)."""

from __future__ import annotations

from schema_analyzer.diff import diff_analyses


def _analysis(entities, relationships, pm_entities, pm_rels, health=None):
    meta = {"confidence": 0.9}
    if health is not None:
        meta["healthScore"] = health
    return {
        "conceptualSchema": {
            "entities": entities,
            "relationships": relationships,
            "properties": [],
        },
        "physicalMapping": {"entities": pm_entities, "relationships": pm_rels},
        "metadata": meta,
    }


def test_diff_identical_reports_no_change():
    a = _analysis(
        [{"name": "User"}],
        [],
        {"User": {"style": "COLLECTION", "collectionName": "users"}},
        {},
    )
    d = diff_analyses(a, a)
    assert d["changed"] is False
    assert d["summary"]["entitiesAdded"] == 0


def test_diff_added_and_removed_entities():
    prev = _analysis([{"name": "User"}, {"name": "Old"}], [], {}, {})
    curr = _analysis([{"name": "User"}, {"name": "New"}], [], {}, {})
    d = diff_analyses(prev, curr)
    assert d["entities"]["added"] == ["New"]
    assert d["entities"]["removed"] == ["Old"]
    assert d["changed"] is True


def test_diff_changed_entity_properties():
    prev = _analysis([{"name": "User", "properties": [{"name": "email"}]}], [], {}, {})
    curr = _analysis([{"name": "User", "properties": [{"name": "email"}, {"name": "age"}]}], [], {}, {})
    d = diff_analyses(prev, curr)
    assert d["entities"]["changed"] == ["User"]


def test_diff_relationship_endpoint_change():
    prev = _analysis(
        [{"name": "A"}, {"name": "B"}],
        [{"type": "R", "fromEntity": "A", "toEntity": "B"}],
        {},
        {},
    )
    curr = _analysis(
        [{"name": "A"}, {"name": "B"}],
        [{"type": "R", "fromEntity": "B", "toEntity": "A"}],
        {},
        {},
    )
    d = diff_analyses(prev, curr)
    assert d["relationships"]["changed"] == ["R"]


def test_diff_mapping_style_flip():
    prev = _analysis(
        [{"name": "User"}],
        [],
        {"User": {"style": "COLLECTION", "collectionName": "users"}},
        {},
    )
    curr = _analysis(
        [{"name": "User"}],
        [],
        {"User": {"style": "LABEL", "collectionName": "nodes", "typeField": "t", "typeValue": "User"}},
        {},
    )
    d = diff_analyses(prev, curr)
    assert d["mappingStyles"]["entities"] == [{"name": "User", "from": "COLLECTION", "to": "LABEL"}]
    assert d["changed"] is True


def test_diff_health_score_delta():
    prev = _analysis([{"name": "User"}], [], {}, {}, health=60)
    curr = _analysis([{"name": "User"}], [], {}, {}, health=85)
    d = diff_analyses(prev, curr)
    assert d["healthScore"] == {"previous": 60, "current": 85, "delta": 25}


def test_diff_health_score_delta_none_when_missing():
    prev = _analysis([{"name": "User"}], [], {}, {})
    curr = _analysis([{"name": "User"}], [], {}, {}, health=85)
    d = diff_analyses(prev, curr)
    assert d["healthScore"]["delta"] is None


def test_diff_sees_property_changes_in_every_property_shape():
    """``properties`` as bare names or a dict keyed by name (``utils.entity_property_names``)."""
    for before, after in (
        (["email"], ["email", "phone"]),
        ({"email": {}}, {"email": {}, "phone": {}}),
    ):
        prev = _analysis([{"name": "User", "properties": before}], [], {}, {})
        curr = _analysis([{"name": "User", "properties": after}], [], {}, {})
        d = diff_analyses(prev, curr)
        assert d["entities"]["changed"] == ["User"], before
        assert d["changed"] is True
    same = _analysis([{"name": "User", "properties": ["email"]}], [], {}, {})
    assert diff_analyses(same, same)["changed"] is False
