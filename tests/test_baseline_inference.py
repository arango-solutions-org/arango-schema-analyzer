import pytest

from schema_analyzer.baseline import infer_baseline_from_snapshot
from schema_analyzer.utils import singularize


def test_baseline_creates_entity_per_document_collection_and_rel_per_edge_collection():
    snapshot = {
        "version": 1,
        "generated_at": "x",
        "collections": [
            {"name": "users", "type": "document", "inferred_entity_type": "User"},
            {"name": "follows", "type": "edge", "inferred_relationship_type": "FOLLOWS"},
        ],
        "graphs": [],
    }
    out = infer_baseline_from_snapshot(snapshot)
    assert out["conceptualSchema"]["entities"][0]["name"] == "User"
    assert out["physicalMapping"]["entities"]["User"]["style"] == "COLLECTION"
    assert out["conceptualSchema"]["relationships"][0]["type"] == "FOLLOWS"
    assert out["physicalMapping"]["relationships"]["FOLLOWS"]["style"] == "DEDICATED_COLLECTION"


def test_baseline_uses_label_mapping_when_type_field_has_multiple_values():
    snapshot = {
        "version": 1,
        "collections": [
            {
                "name": "entities",
                "type": "document",
                "candidate_type_fields": ["type"],
                "sample_field_value_counts": {
                    "type": [{"value": "Person", "count": 2}, {"value": "Company", "count": 1}]
                },
            }
        ],
        "graphs": [],
    }
    out = infer_baseline_from_snapshot(snapshot)
    ents = {e["name"] for e in out["conceptualSchema"]["entities"]}
    assert "Person" in ents
    assert "Company" in ents
    assert out["physicalMapping"]["entities"]["Person"]["style"] == "LABEL"
    assert out["physicalMapping"]["entities"]["Person"]["collectionName"] == "entities"


def test_baseline_uses_generic_with_type_for_edge_collections_with_relation_field():
    snapshot = {
        "version": 1,
        "collections": [
            {
                "name": "edges",
                "type": "edge",
                "candidate_type_fields": ["relation"],
                "sample_field_value_counts": {
                    "relation": [{"value": "KNOWS", "count": 2}, {"value": "WORKS_AT", "count": 1}]
                },
            }
        ],
        "graphs": [],
    }
    out = infer_baseline_from_snapshot(snapshot)
    rels = {r["type"] for r in out["conceptualSchema"]["relationships"]}
    assert "KNOWS" in rels
    assert out["physicalMapping"]["relationships"]["KNOWS"]["style"] == "GENERIC_WITH_TYPE"
    assert out["physicalMapping"]["relationships"]["KNOWS"]["edgeCollectionName"] == "edges"


# ── Singularization ────────────────────────────────────────────────────


def test_singularize_movies_gives_movie():
    assert singularize("movies") == "movie"


def test_singularize_standard_ies_to_y():
    assert singularize("cities") == "city"
    assert singularize("categories") == "category"
    assert singularize("entities") == "entity"
    assert singularize("companies") == "company"
    assert singularize("activities") == "activity"
    assert singularize("properties") == "property"


def test_singularize_ie_roots_just_strip_s():
    assert singularize("cookies") == "cookie"
    assert singularize("zombies") == "zombie"
    assert singularize("brownies") == "brownie"
    assert singularize("calories") == "calorie"


def test_singularize_regular_plural():
    assert singularize("users") == "user"
    assert singularize("persons") == "person"
    assert singularize("nodes") == "node"


def test_singularize_sses():
    assert singularize("addresses") == "address"
    assert singularize("classes") == "class"


# ── Full LPG scenario (neo4j_movies_lpg) ───────────────────────────────


def _neo4j_movies_lpg_snapshot():
    """Synthetic snapshot matching the neo4j_movies_lpg_test database."""
    return {
        "version": 1,
        "generated_at": "2024-01-01T00:00:00Z",
        "collections": [
            {
                "name": "nodes",
                "type": "document",
                "count": 173,
                "inferred_entity_type": "Node",
                "candidate_type_fields": ["type", "labels"],
                "sample_field_value_counts": {
                    "type": [
                        {"value": "Movie", "count": 39},
                        {"value": "Person", "count": 134},
                    ],
                    "labels": [
                        {"value": ["Movie"], "count": 39},
                        {"value": ["Person"], "count": 134},
                    ],
                },
                "observed_fields": {
                    "by_type": {
                        "Movie": ["released", "tagline", "title"],
                        "Person": ["born", "name"],
                    }
                },
            },
            {
                "name": "edges",
                "type": "edge",
                "count": 257,
                "inferred_relationship_type": "EDGES",
                "candidate_type_fields": ["relation"],
                "sample_field_value_counts": {
                    "relation": [
                        {"value": "ACTED_IN", "count": 175},
                        {"value": "DIRECTED", "count": 45},
                        {"value": "FOLLOWS", "count": 3},
                        {"value": "PRODUCED", "count": 15},
                        {"value": "REVIEWED", "count": 9},
                        {"value": "WROTE", "count": 10},
                    ],
                },
                "edge_endpoints": {
                    "from_collections": ["nodes"],
                    "to_collections": ["nodes"],
                    "entity_types_by_relation": {
                        "ACTED_IN": {"from_entity_types": ["Person"], "to_entity_types": ["Movie"]},
                        "DIRECTED": {"from_entity_types": ["Person"], "to_entity_types": ["Movie"]},
                        "FOLLOWS": {"from_entity_types": ["Person"], "to_entity_types": ["Person"]},
                        "PRODUCED": {"from_entity_types": ["Person"], "to_entity_types": ["Movie"]},
                        "REVIEWED": {"from_entity_types": ["Person"], "to_entity_types": ["Movie"]},
                        "WROTE": {"from_entity_types": ["Person"], "to_entity_types": ["Movie"]},
                    },
                },
                "observed_fields": {
                    "by_type": {
                        "ACTED_IN": ["roles"],
                        "DIRECTED": [],
                        "FOLLOWS": [],
                        "PRODUCED": [],
                        "REVIEWED": ["rating", "summary"],
                        "WROTE": [],
                    }
                },
            },
        ],
        "graphs": [],
        "graphs_detailed": [],
    }


def test_lpg_baseline_extracts_correct_entity_types():
    out = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())
    entities = {e["name"] for e in out["conceptualSchema"]["entities"]}
    assert entities == {"Movie", "Person"}
    assert "Node" not in entities


def test_lpg_baseline_extracts_correct_relationship_types():
    out = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())
    rels = {r["type"] for r in out["conceptualSchema"]["relationships"]}
    assert rels == {"ACTED_IN", "DIRECTED", "FOLLOWS", "PRODUCED", "REVIEWED", "WROTE"}
    assert "EDGES" not in rels


def test_lpg_baseline_physical_mapping_uses_label_style():
    out = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())
    pm = out["physicalMapping"]
    assert pm["entities"]["Movie"]["style"] == "LABEL"
    assert pm["entities"]["Movie"]["collectionName"] == "nodes"
    assert pm["entities"]["Movie"]["typeField"] == "type"
    assert pm["entities"]["Movie"]["typeValue"] == "Movie"

    assert pm["entities"]["Person"]["style"] == "LABEL"
    assert pm["entities"]["Person"]["collectionName"] == "nodes"


def test_lpg_baseline_physical_mapping_uses_generic_with_type():
    out = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())
    pm = out["physicalMapping"]
    for rel_type in ["ACTED_IN", "DIRECTED", "FOLLOWS", "PRODUCED", "REVIEWED", "WROTE"]:
        assert pm["relationships"][rel_type]["style"] == "GENERIC_WITH_TYPE"
        assert pm["relationships"][rel_type]["edgeCollectionName"] == "edges"
        assert pm["relationships"][rel_type]["typeField"] == "relation"
        assert pm["relationships"][rel_type]["typeValue"] == rel_type


def test_lpg_baseline_resolves_domain_range():
    out = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())
    rels = {r["type"]: r for r in out["conceptualSchema"]["relationships"]}

    assert rels["ACTED_IN"]["fromEntity"] == "Person"
    assert rels["ACTED_IN"]["toEntity"] == "Movie"
    assert rels["DIRECTED"]["fromEntity"] == "Person"
    assert rels["DIRECTED"]["toEntity"] == "Movie"
    assert rels["FOLLOWS"]["fromEntity"] == "Person"
    assert rels["FOLLOWS"]["toEntity"] == "Person"
    assert rels["REVIEWED"]["fromEntity"] == "Person"
    assert rels["REVIEWED"]["toEntity"] == "Movie"


def test_lpg_baseline_extracts_entity_properties():
    out = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())
    ents = {e["name"]: e for e in out["conceptualSchema"]["entities"]}

    movie_props = {p["name"] for p in ents["Movie"]["properties"]}
    assert movie_props == {"title", "released", "tagline"}

    person_props = {p["name"] for p in ents["Person"]["properties"]}
    assert person_props == {"name", "born"}


def test_lpg_baseline_extracts_relationship_properties():
    out = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())
    rels = {r["type"]: r for r in out["conceptualSchema"]["relationships"]}

    acted_in_props = {p["name"] for p in rels["ACTED_IN"]["properties"]}
    assert acted_in_props == {"roles"}

    reviewed_props = {p["name"] for p in rels["REVIEWED"]["properties"]}
    assert reviewed_props == {"summary", "rating"}

    assert rels["DIRECTED"]["properties"] == []


def test_lpg_baseline_sets_detected_patterns():
    out = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())
    patterns = out.get("detectedPatterns", [])
    assert "LPG_LABEL" in patterns
    assert "LPG_GENERIC_EDGE" in patterns


# ── PG scenario (neo4j_movies_pg) with domain/range ────────────────────


def _neo4j_movies_pg_snapshot():
    """Synthetic snapshot matching the neo4j_movies_pg_test database."""
    return {
        "version": 1,
        "generated_at": "2024-01-01T00:00:00Z",
        "collections": [
            {
                "name": "movies",
                "type": "document",
                "count": 39,
                "inferred_entity_type": "Movie",
                "candidate_type_fields": [],
                "sample_field_value_counts": {},
                "observed_fields": {"fields": ["released", "tagline", "title"]},
            },
            {
                "name": "persons",
                "type": "document",
                "count": 134,
                "inferred_entity_type": "Person",
                "candidate_type_fields": [],
                "sample_field_value_counts": {},
                "observed_fields": {"fields": ["born", "name"]},
            },
            {
                "name": "acted_in",
                "type": "edge",
                "count": 175,
                "inferred_relationship_type": "ACTED_IN",
                "candidate_type_fields": [],
                "sample_field_value_counts": {},
                "edge_endpoints": {
                    "from_collections": ["persons"],
                    "to_collections": ["movies"],
                },
                "observed_fields": {"fields": ["roles"]},
            },
            {
                "name": "directed",
                "type": "edge",
                "count": 45,
                "inferred_relationship_type": "DIRECTED",
                "candidate_type_fields": [],
                "sample_field_value_counts": {},
                "edge_endpoints": {
                    "from_collections": ["persons"],
                    "to_collections": ["movies"],
                },
                "observed_fields": {"fields": []},
            },
            {
                "name": "follows",
                "type": "edge",
                "count": 3,
                "inferred_relationship_type": "FOLLOWS",
                "candidate_type_fields": [],
                "sample_field_value_counts": {},
                "edge_endpoints": {
                    "from_collections": ["persons"],
                    "to_collections": ["persons"],
                },
                "observed_fields": {"fields": []},
            },
        ],
        "graphs": [],
        "graphs_detailed": [],
    }


def test_pg_baseline_extracts_correct_entity_types():
    out = infer_baseline_from_snapshot(_neo4j_movies_pg_snapshot())
    entities = {e["name"] for e in out["conceptualSchema"]["entities"]}
    assert entities == {"Movie", "Person"}


def test_pg_baseline_resolves_domain_range_from_edge_endpoints():
    out = infer_baseline_from_snapshot(_neo4j_movies_pg_snapshot())
    rels = {r["type"]: r for r in out["conceptualSchema"]["relationships"]}

    assert rels["ACTED_IN"]["fromEntity"] == "Person"
    assert rels["ACTED_IN"]["toEntity"] == "Movie"
    assert rels["DIRECTED"]["fromEntity"] == "Person"
    assert rels["DIRECTED"]["toEntity"] == "Movie"
    assert rels["FOLLOWS"]["fromEntity"] == "Person"
    assert rels["FOLLOWS"]["toEntity"] == "Person"


def test_pg_baseline_extracts_entity_properties():
    out = infer_baseline_from_snapshot(_neo4j_movies_pg_snapshot())
    ents = {e["name"]: e for e in out["conceptualSchema"]["entities"]}
    movie_props = {p["name"] for p in ents["Movie"]["properties"]}
    assert movie_props == {"title", "released", "tagline"}
    person_props = {p["name"] for p in ents["Person"]["properties"]}
    assert person_props == {"name", "born"}


def test_pg_baseline_sets_detected_patterns():
    out = infer_baseline_from_snapshot(_neo4j_movies_pg_snapshot())
    patterns = out.get("detectedPatterns", [])
    assert "PG_ENTITY_COLLECTION" in patterns
    assert "PG_DEDICATED_EDGE" in patterns


def test_pg_and_lpg_produce_same_ontology():
    """Both physical styles should produce the same entity/relationship names."""
    pg = infer_baseline_from_snapshot(_neo4j_movies_pg_snapshot())
    lpg = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())

    pg_entities = {e["name"] for e in pg["conceptualSchema"]["entities"]}
    lpg_entities = {e["name"] for e in lpg["conceptualSchema"]["entities"]}
    assert pg_entities == lpg_entities

    pg_common_rels = {"ACTED_IN", "DIRECTED", "FOLLOWS"}
    lpg_rels = {r["type"] for r in lpg["conceptualSchema"]["relationships"]}
    assert pg_common_rels.issubset(lpg_rels)


# ── Hybrid scenario (PG entities + LPG shared edges) ──────────────────


def _neo4j_movies_hybrid_snapshot():
    """Synthetic snapshot: PG entity collections + LPG shared edge collection."""
    return {
        "version": 1,
        "generated_at": "2024-01-01T00:00:00Z",
        "collections": [
            {
                "name": "movies",
                "type": "document",
                "count": 39,
                "inferred_entity_type": "Movie",
                "candidate_type_fields": [],
                "sample_field_value_counts": {},
                "observed_fields": {"fields": ["released", "tagline", "title"]},
            },
            {
                "name": "persons",
                "type": "document",
                "count": 134,
                "inferred_entity_type": "Person",
                "candidate_type_fields": [],
                "sample_field_value_counts": {},
                "observed_fields": {"fields": ["born", "name"]},
            },
            {
                "name": "edges",
                "type": "edge",
                "count": 257,
                "inferred_relationship_type": "EDGES",
                "candidate_type_fields": ["relation"],
                "sample_field_value_counts": {
                    "relation": [
                        {"value": "ACTED_IN", "count": 175},
                        {"value": "DIRECTED", "count": 45},
                        {"value": "FOLLOWS", "count": 3},
                        {"value": "PRODUCED", "count": 15},
                        {"value": "REVIEWED", "count": 9},
                        {"value": "WROTE", "count": 10},
                    ],
                },
                "edge_endpoints": {
                    "from_collections": ["persons"],
                    "to_collections": ["movies", "persons"],
                    "collections_by_relation": {
                        "ACTED_IN": {"from_collections": ["persons"], "to_collections": ["movies"]},
                        "DIRECTED": {"from_collections": ["persons"], "to_collections": ["movies"]},
                        "FOLLOWS": {"from_collections": ["persons"], "to_collections": ["persons"]},
                        "PRODUCED": {"from_collections": ["persons"], "to_collections": ["movies"]},
                        "REVIEWED": {"from_collections": ["persons"], "to_collections": ["movies"]},
                        "WROTE": {"from_collections": ["persons"], "to_collections": ["movies"]},
                    },
                },
                "observed_fields": {
                    "by_type": {
                        "ACTED_IN": ["roles"],
                        "DIRECTED": [],
                        "FOLLOWS": [],
                        "PRODUCED": [],
                        "REVIEWED": ["rating", "summary"],
                        "WROTE": [],
                    }
                },
            },
        ],
        "graphs": [],
        "graphs_detailed": [],
    }


def test_hybrid_baseline_extracts_correct_entities():
    out = infer_baseline_from_snapshot(_neo4j_movies_hybrid_snapshot())
    entities = {e["name"] for e in out["conceptualSchema"]["entities"]}
    assert entities == {"Movie", "Person"}


def test_hybrid_baseline_extracts_correct_relationships():
    out = infer_baseline_from_snapshot(_neo4j_movies_hybrid_snapshot())
    rels = {r["type"] for r in out["conceptualSchema"]["relationships"]}
    assert rels == {"ACTED_IN", "DIRECTED", "FOLLOWS", "PRODUCED", "REVIEWED", "WROTE"}
    assert "EDGES" not in rels


def test_hybrid_baseline_resolves_domain_range():
    out = infer_baseline_from_snapshot(_neo4j_movies_hybrid_snapshot())
    rels = {r["type"]: r for r in out["conceptualSchema"]["relationships"]}
    assert rels["ACTED_IN"]["fromEntity"] == "Person"
    assert rels["ACTED_IN"]["toEntity"] == "Movie"
    assert rels["FOLLOWS"]["fromEntity"] == "Person"
    assert rels["FOLLOWS"]["toEntity"] == "Person"


def test_hybrid_has_pg_entity_collection_and_lpg_generic_edge_patterns():
    out = infer_baseline_from_snapshot(_neo4j_movies_hybrid_snapshot())
    patterns = out.get("detectedPatterns", [])
    assert "PG_ENTITY_COLLECTION" in patterns
    assert "LPG_GENERIC_EDGE" in patterns


def test_hybrid_physical_mapping_mixes_styles():
    out = infer_baseline_from_snapshot(_neo4j_movies_hybrid_snapshot())
    pm = out["physicalMapping"]
    assert pm["entities"]["Movie"]["style"] == "COLLECTION"
    assert pm["entities"]["Person"]["style"] == "COLLECTION"
    for rel in ["ACTED_IN", "DIRECTED", "FOLLOWS", "PRODUCED", "REVIEWED", "WROTE"]:
        assert pm["relationships"][rel]["style"] == "GENERIC_WITH_TYPE"
        assert pm["relationships"][rel]["edgeCollectionName"] == "edges"


def test_all_three_styles_produce_same_ontology():
    """PG, LPG, and hybrid must produce identical entity and relationship sets."""
    pg = infer_baseline_from_snapshot(_neo4j_movies_pg_snapshot())
    lpg = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())
    hybrid = infer_baseline_from_snapshot(_neo4j_movies_hybrid_snapshot())

    pg_entities = {e["name"] for e in pg["conceptualSchema"]["entities"]}
    lpg_entities = {e["name"] for e in lpg["conceptualSchema"]["entities"]}
    hybrid_entities = {e["name"] for e in hybrid["conceptualSchema"]["entities"]}
    assert pg_entities == lpg_entities == hybrid_entities

    all_rels = {"ACTED_IN", "DIRECTED", "FOLLOWS", "PRODUCED", "REVIEWED", "WROTE"}
    lpg_rels = {r["type"] for r in lpg["conceptualSchema"]["relationships"]}
    hybrid_rels = {r["type"] for r in hybrid["conceptualSchema"]["relationships"]}
    assert lpg_rels == all_rels
    assert hybrid_rels == all_rels


# ── Single-value edge discriminator (cypher_hybrid_fixture scenario) ────


def _cypher_hybrid_fixture_snapshot():
    """Snapshot matching the cypher_hybrid_fixture database from arango-cypher-py.

    PG-style entity collection ``users`` with no type field, plus an LPG-style
    edge collection ``edges`` where the ``type`` discriminator has only ONE
    distinct value (``FOLLOWS``).  The analyzer must still detect this as
    GENERIC_WITH_TYPE, not fall back to DEDICATED_COLLECTION.
    """
    return {
        "version": 1,
        "collections": [
            {
                "name": "users",
                "type": "document",
                "inferred_entity_type": "User",
                "count": 6,
                "observed_fields": {
                    "fields": ["id", "name", "city", "state", "age", "active"],
                },
            },
            {
                "name": "edges",
                "type": "edge",
                "count": 3,
                "candidate_type_fields": ["type"],
                "sample_field_value_counts": {
                    "type": [{"value": "FOLLOWS", "count": 3}],
                },
                "edge_endpoints": {
                    "from_collections": ["users"],
                    "to_collections": ["users"],
                },
                "observed_fields": {
                    "by_type": {"FOLLOWS": []},
                },
            },
        ],
        "graphs": [],
    }


def test_single_value_edge_discriminator_detected_as_generic():
    """Edge collection with one distinct type value must still be GENERIC_WITH_TYPE."""
    out = infer_baseline_from_snapshot(_cypher_hybrid_fixture_snapshot())
    rels = {r["type"]: r for r in out["conceptualSchema"]["relationships"]}
    assert "FOLLOWS" in rels, f"Expected FOLLOWS, got {list(rels.keys())}"
    assert "EDGES" not in rels, "Should not produce collection-name-derived 'EDGES'"


def test_single_value_edge_discriminator_physical_mapping():
    out = infer_baseline_from_snapshot(_cypher_hybrid_fixture_snapshot())
    pm_rels = out["physicalMapping"]["relationships"]
    assert "FOLLOWS" in pm_rels
    assert pm_rels["FOLLOWS"]["style"] == "GENERIC_WITH_TYPE"
    assert pm_rels["FOLLOWS"]["edgeCollectionName"] == "edges"
    assert pm_rels["FOLLOWS"]["typeField"] == "type"
    assert pm_rels["FOLLOWS"]["typeValue"] == "FOLLOWS"


def test_single_value_edge_discriminator_domain_range():
    out = infer_baseline_from_snapshot(_cypher_hybrid_fixture_snapshot())
    rels = {r["type"]: r for r in out["conceptualSchema"]["relationships"]}
    assert rels["FOLLOWS"]["fromEntity"] == "User"
    assert rels["FOLLOWS"]["toEntity"] == "User"


def test_single_value_edge_discriminator_detected_patterns():
    out = infer_baseline_from_snapshot(_cypher_hybrid_fixture_snapshot())
    patterns = out.get("detectedPatterns", [])
    assert "PG_ENTITY_COLLECTION" in patterns
    assert "LPG_GENERIC_EDGE" in patterns
    assert "PG_DEDICATED_EDGE" not in patterns


# ── Collection-per-entity strategy (pure property-graph schemas) ───────
# Motivating bug: on a PG schema (one collection per type) with a uniform `type`
# field, the auto heuristic mistakes a high-cardinality attribute (e.g. region)
# for a type discriminator and shatters a collection into value-classes.


def _pg_attribute_misfire_snapshot():
    return {
        "version": 1,
        "collections": [
            {
                "name": "aws_s3_bucket",
                "type": "document",
                "candidate_type_fields": ["region"],
                "sample_field_value_counts": {
                    "region": [
                        {"value": "us-east-1", "count": 3},
                        {"value": "eu-west-1", "count": 2},
                        {"value": "ap-south-1", "count": 1},
                    ]
                },
            },
            {
                "name": "CAN_ACCESS",
                "type": "edge",
                "inferred_relationship_type": "CAN_ACCESS",
                "candidate_type_fields": ["destination_sub_type"],
                "sample_field_value_counts": {
                    "destination_sub_type": [
                        {"value": "aws_s3_bucket", "count": 4},
                        {"value": "aws_ec2_instance", "count": 2},
                    ]
                },
            },
        ],
        "graphs": [],
    }


def test_auto_strategy_misfires_on_pg_attribute():
    """Documents the motivating bug: auto mode treats an attribute as a type tag."""
    out = infer_baseline_from_snapshot(_pg_attribute_misfire_snapshot())
    ents = out["conceptualSchema"]["entities"]
    assert len(ents) > 1  # region shattered the collection into value-classes


def test_collection_strategy_forces_one_entity_per_collection():
    out = infer_baseline_from_snapshot(_pg_attribute_misfire_snapshot(), collection_per_entity=True)
    ents = out["conceptualSchema"]["entities"]
    assert len(ents) == 1
    pm_ent = next(iter(out["physicalMapping"]["entities"].values()))
    assert pm_ent["style"] == "COLLECTION"
    assert pm_ent["collectionName"] == "aws_s3_bucket"
    rels = out["conceptualSchema"]["relationships"]
    assert len(rels) == 1
    pm_rel = next(iter(out["physicalMapping"]["relationships"].values()))
    assert pm_rel["style"] == "DEDICATED_COLLECTION"
    assert pm_rel["edgeCollectionName"] == "CAN_ACCESS"
    patterns = out.get("detectedPatterns", [])
    assert "PG_ENTITY_COLLECTION" in patterns
    assert "PG_DEDICATED_EDGE" in patterns
    assert "LPG_LABEL" not in patterns


def test_collection_strategy_overrides_genuine_lpg_discriminator():
    """Even a real LPG type field is ignored under collection_per_entity."""
    out = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot(), collection_per_entity=True)
    ents = {e["name"] for e in out["conceptualSchema"]["entities"]}
    assert ents == {"Node"}
    assert out["physicalMapping"]["entities"]["Node"]["style"] == "COLLECTION"


def test_collection_strategy_default_is_auto_no_behavior_change():
    default = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot())
    explicit = infer_baseline_from_snapshot(_neo4j_movies_lpg_snapshot(), collection_per_entity=False)
    d = {e["name"] for e in default["conceptualSchema"]["entities"]}
    a = {e["name"] for e in explicit["conceptualSchema"]["entities"]}
    assert d == a == {"Movie", "Person"}


def test_collection_per_entity_unions_by_type_properties():
    """A heterogeneous collection profiled by discriminator must not lose its
    properties under collection_per_entity: the entity IS the collection, so
    its property set is the union of every type bucket. Regression: Document
    came back with zero properties (losing the accountId join key) because
    the snapshot stored only observed_fields.by_type."""
    snapshot = {
        "version": 1,
        "collections": [
            {
                "name": "documents",
                "type": "document",
                "inferred_entity_type": "Document",
                "candidate_type_fields": ["source"],
                "observed_fields": {
                    "by_type": {
                        "email": ["account_id", "citable_url", "subject"],
                        "slack": ["account_id", "citable_url", "channel"],
                    }
                },
            }
        ],
        "graphs": [],
    }
    out = infer_baseline_from_snapshot(snapshot, collection_per_entity=True)
    ents = {e["name"]: e for e in out["conceptualSchema"]["entities"]}
    assert set(ents) == {"Document"}
    props = [p["name"] for p in ents["Document"]["properties"]]
    assert props == ["account_id", "channel", "citable_url", "source", "subject"]  # sorted union + discriminator
    assert out["physicalMapping"]["entities"]["Document"]["style"] == "COLLECTION"


def _iam_demo_snapshot():
    """Shape of the IAM demo database: dedicated edge collections whose documents
    carry ``source_sub_type`` / ``destination_sub_type`` copies of the endpoint
    collection names, with endpoints declared only by the named graph."""

    def edge(name, count, frm, to):
        return {
            "name": name,
            "type": "edge",
            "count": count,
            "candidate_type_fields": ["destination_sub_type", "source_sub_type"],
            "sample_field_value_counts": {
                "destination_sub_type": [{"value": c, "count": count // len(to)} for c in to],
                "source_sub_type": [{"value": c, "count": count // len(frm)} for c in frm],
            },
            "observed_fields": {"fields": ["destination_sub_type", "source_sub_type", "org_id"]},
        }

    def doc(name, count):
        return {
            "name": name,
            "type": "document",
            "count": count,
            "candidate_type_fields": [],
            "sample_field_value_counts": {},
            "observed_fields": {"fields": ["name", "org_id"]},
        }

    return {
        "version": 1,
        "generated_at": "2026-10-05T00:00:00Z",
        "collections": [
            doc("aws_iam_role", 400),
            doc("aws_iam_user", 300),
            doc("aws_ec2_instance", 500),
            doc("aws_s3_bucket", 200),
            edge("CAN_ACCESS", 900, ["aws_iam_role", "aws_iam_user"], ["aws_ec2_instance", "aws_s3_bucket"]),
            edge("CAN_ASSUME", 300, ["aws_iam_role", "aws_iam_user"], ["aws_iam_role"]),
        ],
        "graphs": ["IAM_DEMO"],
        "graphs_detailed": [
            {
                "name": "IAM_DEMO",
                "edge_definitions": [
                    {
                        "collection": "CAN_ACCESS",
                        "from": ["aws_iam_role", "aws_iam_user"],
                        "to": ["aws_ec2_instance", "aws_s3_bucket"],
                    },
                    {"collection": "CAN_ASSUME", "from": ["aws_iam_role", "aws_iam_user"], "to": ["aws_iam_role"]},
                ],
            }
        ],
    }


def test_endpoint_mirror_fields_leave_edge_collections_dedicated():
    """Regression (IAM demo, 2026-10-05): ``destination_sub_type`` was read as the
    relationship type, so CAN_ACCESS became relationships named aws_ec2_instance,
    aws_s3_bucket, … and Cypher could not express ``[:CAN_ACCESS]``."""
    out = infer_baseline_from_snapshot(_iam_demo_snapshot())
    rel_types = {r["type"] for r in out["conceptualSchema"]["relationships"]}
    assert rel_types == {"CAN_ACCESS", "CAN_ASSUME"}
    pm = out["physicalMapping"]["relationships"]
    assert pm["CAN_ACCESS"]["style"] == "DEDICATED_COLLECTION"
    assert pm["CAN_ASSUME"]["style"] == "DEDICATED_COLLECTION"
    assert "LPG_GENERIC_EDGE" not in out.get("metadata", {}).get("detectedPatterns", [])


@pytest.mark.parametrize(
    ("plural", "singular"),
    [
        # Upper-case names (graphs loaded from Snowflake or Oracle keep them) used
        # to come back unchanged; the suffix rules matched lower case only.
        ("CUSTOMERS", "CUSTOMER"),
        ("ORDERS", "ORDER"),
        ("USAGE_METRICS", "USAGE_METRIC"),
        ("CATEGORIES", "CATEGORY"),
        ("COOKIES", "COOKIE"),
        ("ADDRESSES", "ADDRESS"),
        ("CLASS", "CLASS"),
        ("Categories", "Category"),
        ("Orders", "Order"),
    ],
)
def test_singularize_is_case_insensitive(plural, singular):
    assert singularize(plural) == singular


def test_edge_endpoints_from_graphs_first_graph_wins():
    # One edge collection declared by two graphs with different endpoints: the
    # lexicographically first graph supplies them (utils.index_edge_definitions_by_collection),
    # as in the sharding profile; baseline used to take the last.
    snapshot = _neo4j_movies_pg_snapshot()
    snapshot["collections"] = [c for c in snapshot["collections"] if c["name"] in ("movies", "persons")] + [
        {"name": "rated", "type": "edge", "inferred_relationship_type": "RATED", "observed_fields": {"fields": []}}
    ]
    snapshot["graphs_detailed"] = [
        {"name": "g_a", "edge_definitions": [{"collection": "rated", "from": ["persons"], "to": ["movies"]}]},
        {"name": "g_b", "edge_definitions": [{"collection": "rated", "from": ["movies"], "to": ["persons"]}]},
    ]
    out = infer_baseline_from_snapshot(snapshot)
    rels = {r["type"]: r for r in out["conceptualSchema"]["relationships"]}
    assert rels["RATED"]["fromEntity"] == "Person"
    assert rels["RATED"]["toEntity"] == "Movie"
