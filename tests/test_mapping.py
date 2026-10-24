import pytest

from schema_analyzer.mapping import PhysicalMapping


def test_aql_entity_match_collection():
    pm = PhysicalMapping(entities={"User": {"style": "COLLECTION", "collectionName": "users"}})
    out = pm.aql_entity_match(variable="u", entity_type="User")
    assert "FOR u IN @@collection" in out["query"]
    assert out["bind_vars"]["@collection"] == "users"


def test_aql_entity_match_label():
    pm = PhysicalMapping(
        entities={
            "Post": {
                "style": "LABEL",
                "collectionName": "entities",
                "typeField": "type",
                "typeValue": "post",
            }
        }
    )
    out = pm.aql_entity_match(variable="p", entity_type="Post")
    assert "FILTER p[@typeField] == @typeValue" in out["query"]
    assert out["bind_vars"]["typeField"] == "type"
    assert out["bind_vars"]["typeValue"] == "post"


def test_aql_relationship_traversal_dedicated():
    pm = PhysicalMapping(relationships={"FOLLOWS": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "follows"}})
    out = pm.aql_relationship_traversal(from_variable="u", rel_type="FOLLOWS", to_variable="v")
    assert "@@edgeCollection" in out["query"]
    assert out["bind_vars"]["@edgeCollection"] == "follows"


def test_aql_relationship_traversal_generic_with_type():
    pm = PhysicalMapping(
        relationships={
            "AUTHORED": {
                "style": "GENERIC_WITH_TYPE",
                "edgeCollectionName": "relationships",
                "typeField": "relation",
                "typeValue": "authored",
            }
        }
    )
    out = pm.aql_relationship_traversal(from_variable="u", rel_type="AUTHORED", to_variable="p")
    assert "FILTER e[@typeField] == @typeValue" in out["query"]
    assert out["bind_vars"]["@edgeCollection"] == "relationships"


def test_invalid_identifier_rejected():
    pm = PhysicalMapping(entities={"User": {"style": "COLLECTION", "collectionName": "users"}})
    with pytest.raises(ValueError):
        pm.aql_entity_match(variable="u; RETURN 1", entity_type="User")


def test_aql_entity_match_labels():
    pm = PhysicalMapping(entities={"Customer": {"style": "LABELS", "collectionName": "nodes", "labelsField": "labels"}})
    out = pm.aql_entity_match(variable="n", entity_type="Customer")
    assert out["query"] == "FOR n IN @@collection FILTER @label IN n[@labelsField]"
    assert out["bind_vars"] == {"@collection": "nodes", "labelsField": "labels", "label": "Customer"}
