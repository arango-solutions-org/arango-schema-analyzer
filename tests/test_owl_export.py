from __future__ import annotations

from schema_analyzer.owl_export import (
    export_conceptual_model_as_jsonld,
    export_conceptual_model_as_owl_turtle,
)

_SAMPLE = {
    "conceptualSchema": {
        "entities": [
            {"name": "User", "labels": ["User"], "properties": []},
        ],
        "relationships": [
            {"type": "FOLLOWS", "fromEntity": "User", "toEntity": "User", "properties": []},
        ],
    },
    "physicalMapping": {
        "entities": {
            "User": {"style": "COLLECTION", "collectionName": "users"},
        },
        "relationships": {
            "FOLLOWS": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "follows"},
        },
    },
}


def test_owl_export_contains_class():
    ttl = export_conceptual_model_as_owl_turtle(_SAMPLE)
    assert ":User a owl:Class" in ttl
    assert 'rdfs:label "User"' in ttl


def test_owl_export_contains_object_property():
    ttl = export_conceptual_model_as_owl_turtle(_SAMPLE)
    assert ":FOLLOWS a owl:ObjectProperty" in ttl
    assert "rdfs:domain :User" in ttl
    assert "rdfs:range :User" in ttl


def test_owl_export_contains_physical_annotations():
    ttl = export_conceptual_model_as_owl_turtle(_SAMPLE)
    assert 'phys:mappingStyle "COLLECTION"' in ttl
    assert 'phys:collectionName "users"' in ttl
    assert 'phys:edgeCollectionName "follows"' in ttl


def test_owl_export_custom_iris():
    ttl = export_conceptual_model_as_owl_turtle(
        _SAMPLE, base_iri="http://example.org/schema#", phys_iri="http://example.org/phys#"
    )
    assert "@prefix : <http://example.org/schema#>" in ttl
    assert "@prefix phys: <http://example.org/phys#>" in ttl


def test_owl_export_empty():
    ttl = export_conceptual_model_as_owl_turtle({"conceptualSchema": {}, "physicalMapping": {}})
    assert "owl:Ontology" in ttl
    assert ":User" not in ttl


# ── Richer OWL (PRD §6.3) ────────────────────────────────────────────────

_RICH = {
    "conceptualSchema": {
        "entities": [
            {"name": "IBEXDocument"},
            {"name": "MOR1KXDocument"},
            {"name": "Author"},
        ],
        "relationships": [
            {"type": "WROTE", "fromEntity": "Author", "toEntity": "IBEXDocument"},
            {"type": "AUTHORED_BY", "fromEntity": "IBEXDocument", "toEntity": "Author", "inverseOf": "WROTE"},
        ],
    },
    "physicalMapping": {
        "entities": {
            "IBEXDocument": {"style": "COLLECTION", "collectionName": "IBEX_Documents"},
            "MOR1KXDocument": {"style": "COLLECTION", "collectionName": "MOR1KX_Documents"},
            "Author": {"style": "COLLECTION", "collectionName": "authors"},
        },
        "relationships": {
            "WROTE": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "wrote"},
            "AUTHORED_BY": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "authored_by"},
        },
        "shardFamilies": [
            {
                "name": "Document",
                "members": [{"entity": "IBEXDocument"}, {"entity": "MOR1KXDocument"}],
            }
        ],
    },
    "metadata": {
        "statistics": {
            "relationships": {
                "WROTE": {"cardinality_pattern": "1:N"},
                "AUTHORED_BY": {"cardinality_pattern": "N:1"},
            }
        }
    },
}


def test_owl_subclassof_from_shard_families():
    ttl = export_conceptual_model_as_owl_turtle(_RICH)
    assert ":Document a owl:Class" in ttl
    assert ":IBEXDocument rdfs:subClassOf :Document ." in ttl
    assert ":MOR1KXDocument rdfs:subClassOf :Document ." in ttl


def test_owl_cardinality_characteristics():
    ttl = export_conceptual_model_as_owl_turtle(_RICH)
    # WROTE is 1:N -> inverse-functional, not functional
    assert ':WROTE phys:observedCardinality "1:N"' in ttl
    assert ":WROTE a owl:InverseFunctionalProperty ." in ttl
    # AUTHORED_BY is N:1 -> functional, not inverse-functional
    assert ":AUTHORED_BY a owl:FunctionalProperty ." in ttl


def test_owl_inverse_of():
    ttl = export_conceptual_model_as_owl_turtle(_RICH)
    assert ":AUTHORED_BY owl:inverseOf :WROTE ." in ttl


def test_jsonld_export_structure():
    doc = export_conceptual_model_as_jsonld(_RICH)
    assert "@context" in doc and "@graph" in doc
    by_id = {n["@id"]: n for n in doc["@graph"]}
    assert by_id["IBEXDocument"]["@type"] == "owl:Class"
    assert by_id["IBEXDocument"]["rdfs:subClassOf"] == {"@id": "Document"}
    assert "owl:FunctionalProperty" in by_id["AUTHORED_BY"]["@type"]
    assert by_id["AUTHORED_BY"]["owl:inverseOf"] == {"@id": "WROTE"}
    assert by_id["WROTE"]["rdfs:domain"] == {"@id": "Author"}


def test_owl_tool_op_jsonld_format():
    from schema_analyzer.tool import run_tool

    analysis = {
        "conceptualSchema": {"entities": [{"name": "User"}], "relationships": [], "properties": []},
        "physicalMapping": {
            "entities": {"User": {"style": "COLLECTION", "collectionName": "users"}},
            "relationships": {},
        },
        "metadata": {
            "confidence": 0.9,
            "timestamp": "2026-01-01T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 0},
            "detectedPatterns": [],
        },
    }
    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "owl",
            "input": {"analysis": analysis},
            "outputOptions": {"owlFormat": "jsonld"},
        }
    )
    assert resp["ok"] is True, resp
    assert "@graph" in resp["result"]["jsonld"]

    resp_ttl = run_tool({"contractVersion": "1", "operation": "owl", "input": {"analysis": analysis}})
    assert resp_ttl["ok"] is True
    assert "owl:Ontology" in resp_ttl["result"]["turtle"]


# ---------------------------------------------------------------------------
# phys: annotations — one table feeds the declarations, Turtle and JSON-LD, so the
# two formats carry the same annotations and Turtle declares every one it writes.
# ---------------------------------------------------------------------------

_EVERY_ANNOTATION = {
    "conceptualSchema": {
        "entities": [
            {"name": "User", "properties": [{"name": "city"}, {"name": "tags"}, {"name": "first name"}]},
            {"name": "Doc"},
            {"name": "Chunk"},
        ],
        "relationships": [
            {"type": "WROTE", "fromEntity": "User", "toEntity": "Doc"},
            {"type": "FOLLOWS", "fromEntity": "User", "toEntity": "User"},
        ],
    },
    "physicalMapping": {
        "entities": {
            "User": {
                "style": "COLLECTION",
                "collectionName": "users",
                "properties": {
                    "city": {"path": ["address", "city"]},
                    "tags": {"multiValued": True},
                    "first name": {"field": "fn"},
                },
            },
            "Doc": {"style": "LABEL", "collectionName": "nodes", "typeField": "type", "typeValue": "doc"},
            "Chunk": {"style": "LABELS", "collectionName": "nodes", "labelsField": "labels", "labelValue": "chunk"},
        },
        "relationships": {
            "WROTE": {
                "style": "GENERIC_WITH_TYPE",
                "edgeCollectionName": "edges",
                "typeField": "relation",
                "typeValue": "wrote",
            },
            "FOLLOWS": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "follows"},
        },
    },
    "metadata": {"statistics": {"relationships": {"WROTE": {"cardinality_pattern": "1:N"}}}},
}


def _turtle_phys_annotations(ttl: str) -> set[tuple[str, str, str]]:
    base, phys = _ns()
    return {
        (str(s)[len(str(base)) :], str(p)[len(str(phys)) :], str(o))
        for s, p, o in _graph(ttl)
        if str(p).startswith(str(phys))
    }


def test_turtle_declares_every_phys_annotation_it_writes():
    from rdflib import OWL, RDF

    _, phys = _ns()
    g = _graph(export_conceptual_model_as_owl_turtle(_EVERY_ANNOTATION))
    used = {p for _, p, _ in g if str(p).startswith(str(phys))}
    assert phys.observedCardinality in used
    assert phys.typeField in used and phys.labelValue in used and phys.attributePath in used
    undeclared = {p for p in used if (p, RDF.type, OWL.AnnotationProperty) not in g}
    assert not undeclared


def test_jsonld_carries_a_relationships_type_discriminator():
    by_id = {n["@id"]: n for n in export_conceptual_model_as_jsonld(_EVERY_ANNOTATION)["@graph"]}
    assert by_id["WROTE"]["phys:typeField"] == "relation"
    assert by_id["WROTE"]["phys:typeValue"] == "wrote"
    assert "phys:typeField" not in by_id["FOLLOWS"]


def test_jsonld_and_turtle_carry_the_same_phys_annotations():
    jsonld = {
        (node["@id"], key.removeprefix("phys:"), value)
        for node in export_conceptual_model_as_jsonld(_EVERY_ANNOTATION)["@graph"]
        for key, value in node.items()
        if key.startswith("phys:")
    }
    assert jsonld == _turtle_phys_annotations(export_conceptual_model_as_owl_turtle(_EVERY_ANNOTATION))


# ---------------------------------------------------------------------------
# Datatype properties (issue #13: the export had classes and relationships but
# no field at all — prod.demo IAM: 1,576 properties, 0 owl:DatatypeProperty)
# ---------------------------------------------------------------------------

_FIELDS = {
    "conceptualSchema": {
        "entities": [
            {
                "name": "Chunk",
                "properties": [
                    {"name": "content", "type": "string"},
                    {"name": "position", "type": "integer"},
                    {"name": "score", "type": "number"},
                ],
            },
            {
                "name": "Document",
                "properties": [
                    {"name": "content", "type": "string"},
                    {"name": "file-name", "type": "string"},
                    {"name": "pages", "type": "integer"},
                    {"name": "score", "type": "string"},
                    {"name": "PART_OF", "type": "string"},
                    {"name": "Chunk", "type": "string"},
                ],
            },
            {"name": "Note", "properties": [{"name": "file_name", "type": "string"}]},
        ],
        "relationships": [{"type": "PART_OF", "fromEntity": "Chunk", "toEntity": "Document"}],
    },
    "physicalMapping": {
        "entities": {
            "Chunk": {"style": "COLLECTION", "collectionName": "chunks"},
            "Document": {
                "style": "COLLECTION",
                "collectionName": "documents",
                "properties": {"pages": {"field": "page_count"}},
            },
            "Note": {"style": "COLLECTION", "collectionName": "notes"},
        },
        "relationships": {"PART_OF": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "part_of"}},
    },
}


def _graph(ttl: str):
    import rdflib

    g = rdflib.Graph()
    g.parse(data=ttl, format="turtle")
    return g


def _ns():
    import rdflib

    from schema_analyzer.defaults import DEFAULT_OWL_BASE_IRI, DEFAULT_OWL_PHYSICAL_IRI

    return rdflib.Namespace(DEFAULT_OWL_BASE_IRI), rdflib.Namespace(DEFAULT_OWL_PHYSICAL_IRI)


def test_every_field_is_a_datatype_property_with_its_class_as_domain():
    from rdflib import OWL, RDF, RDFS, XSD

    g = _graph(export_conceptual_model_as_owl_turtle(_FIELDS))
    S, _ = _ns()
    assert (S.position, RDF.type, OWL.DatatypeProperty) in g
    assert (S.position, RDFS.domain, S.Chunk) in g
    assert (S.position, RDFS.range, XSD.integer) in g
    assert len(set(g.subjects(RDF.type, OWL.DatatypeProperty))) == 4  # content, position, pages, score


def test_a_field_shared_by_classes_has_a_union_domain():
    """Two rdfs:domain triples would mean the *intersection* of the classes."""
    from rdflib import OWL, RDFS
    from rdflib.collection import Collection

    g = _graph(export_conceptual_model_as_owl_turtle(_FIELDS))
    S, _ = _ns()
    (domain,) = list(g.objects(S.content, RDFS.domain))
    members = set(Collection(g, g.value(domain, OWL.unionOf)))
    assert members == {S.Chunk, S.Document}


def test_the_baselines_default_string_type_is_not_asserted_as_a_range():
    """The baseline types every property "string" without looking at the data."""
    from rdflib import RDFS

    g = _graph(export_conceptual_model_as_owl_turtle(_FIELDS))
    S, _ = _ns()
    assert list(g.objects(S.content, RDFS.range)) == []


def test_entities_that_disagree_on_a_type_get_no_range():
    from rdflib import RDFS

    g = _graph(export_conceptual_model_as_owl_turtle(_FIELDS))
    S, _ = _ns()
    assert list(g.objects(S.score, RDFS.range)) == []  # number on Chunk, string on Document


def test_a_renamed_field_carries_its_stored_name():
    g = _graph(export_conceptual_model_as_owl_turtle(_FIELDS))
    S, P = _ns()
    assert str(g.value(S.pages, P.attributeName)) == "page_count"
    assert g.value(S.position, P.attributeName) is None  # the local name is the field


def test_names_that_would_pun_or_collapse_are_left_out():
    """`PART_OF` / `Chunk` are already a property / class IRI; `file-name` and
    `file_name` would both become :file_name, which could then mean either field."""
    from rdflib import OWL, RDF

    g = _graph(export_conceptual_model_as_owl_turtle(_FIELDS))
    S, _ = _ns()
    assert (S.PART_OF, RDF.type, OWL.DatatypeProperty) not in g
    assert (S.Chunk, RDF.type, OWL.DatatypeProperty) not in g
    assert (S.file_name, RDF.type, OWL.DatatypeProperty) not in g


def test_a_sanitized_name_maps_back_to_its_field():
    data = {
        "conceptualSchema": {"entities": [{"name": "Doc", "properties": [{"name": "file-name"}]}]},
        "physicalMapping": {"entities": {"Doc": {"style": "COLLECTION", "collectionName": "docs"}}},
    }
    g = _graph(export_conceptual_model_as_owl_turtle(data))
    S, P = _ns()
    assert str(g.value(S.file_name, P.attributeName)) == "file-name"


def test_the_jsonld_export_carries_the_same_datatype_properties():
    doc = export_conceptual_model_as_jsonld(_FIELDS)
    by_id = {n["@id"]: n for n in doc["@graph"]}
    assert by_id["position"]["@type"] == "owl:DatatypeProperty"
    assert by_id["position"]["rdfs:domain"] == {"@id": "Chunk"}
    assert by_id["position"]["rdfs:range"] == {"@id": "xsd:integer"}
    assert by_id["content"]["rdfs:domain"] == {
        "@type": "owl:Class",
        "owl:unionOf": {"@list": [{"@id": "Chunk"}, {"@id": "Document"}]},
    }
    assert by_id["pages"]["phys:attributeName"] == "page_count"
    assert "file_name" not in by_id and by_id["PART_OF"]["@type"] != "owl:DatatypeProperty"


def test_a_schema_without_fields_exports_as_before():
    ttl = export_conceptual_model_as_owl_turtle(_SAMPLE)
    assert "owl:DatatypeProperty" not in ttl
