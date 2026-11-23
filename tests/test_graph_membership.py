"""Tests for named-graph membership annotation + graph_scope snapshot filter."""

from __future__ import annotations

import pytest

from schema_analyzer.errors import SchemaAnalyzerError
from schema_analyzer.graph_membership import compute_graph_membership
from schema_analyzer.snapshot import _summarize_graph_props, snapshot_physical_schema


def test_summarize_graph_props_python_arango_snake_shape():
    # python-arango normalizes to edge_definitions/edge_collection/*_vertex_collections.
    props = {
        "name": "g",
        "edge_definitions": [
            {"edge_collection": "wrote", "from_vertex_collections": ["users"], "to_vertex_collections": ["posts"]}
        ],
        "orphan_collections": ["badges"],
    }
    out = _summarize_graph_props(props)
    assert out["edge_definitions"] == [{"collection": "wrote", "from": ["users"], "to": ["posts"]}]
    assert out["orphan_collections"] == ["badges"]


def test_summarize_graph_props_raw_camel_shape():
    props = {
        "name": "g",
        "edgeDefinitions": [{"collection": "wrote", "from": ["users"], "to": ["posts"]}],
        "orphanCollections": ["badges"],
    }
    out = _summarize_graph_props(props)
    assert out["edge_definitions"] == [{"collection": "wrote", "from": ["users"], "to": ["posts"]}]
    assert out["orphan_collections"] == ["badges"]


def _data(entities=None, relationships=None):
    return {
        "physicalMapping": {"entities": entities or {}, "relationships": relationships or {}},
        "metadata": {},
    }


SNAPSHOT = {
    "graphs_detailed": [
        {
            "name": "social",
            "edge_definitions": [{"collection": "follows", "from": ["users"], "to": ["users"]}],
            "orphan_collections": ["badges"],
        },
        {
            "name": "content",
            "edge_definitions": [{"collection": "wrote", "from": ["users"], "to": ["posts"]}],
            "orphan_collections": [],
        },
    ]
}


def test_membership_labels_entities_and_relationships():
    data = _data(
        entities={
            "User": {"style": "COLLECTION", "collectionName": "users"},
            "Post": {"style": "COLLECTION", "collectionName": "posts"},
            "Badge": {"style": "COLLECTION", "collectionName": "badges"},
            "Loose": {"style": "COLLECTION", "collectionName": "loose"},
        },
        relationships={
            "FOLLOWS": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "follows"},
            "WROTE": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "wrote"},
        },
    )
    block = compute_graph_membership(data, SNAPSHOT)
    assert block["graphCount"] == 2
    # users is a vertex in BOTH graphs -> many-to-many
    assert data["physicalMapping"]["entities"]["User"]["graphs"] == ["content", "social"]
    assert data["physicalMapping"]["entities"]["Post"]["graphs"] == ["content"]
    assert data["physicalMapping"]["entities"]["Badge"]["graphs"] == ["social"]
    assert "graphs" not in data["physicalMapping"]["entities"]["Loose"]
    assert data["physicalMapping"]["relationships"]["FOLLOWS"]["graphs"] == ["social"]
    # ungraphed bucket
    assert block["ungraphed"]["entities"] == ["Loose"]
    assert block["ungraphed"]["relationships"] == []
    # per-graph summary
    assert block["graphs"]["social"]["edgeCollections"] == ["follows"]
    assert set(block["graphs"]["content"]["entities"]) == {"User", "Post"}


def test_no_named_graphs_returns_none():
    data = _data(entities={"User": {"style": "COLLECTION", "collectionName": "users"}})
    assert compute_graph_membership(data, {"graphs_detailed": []}) is None
    assert "graphs" not in data["physicalMapping"]["entities"]["User"]


# ── graph_scope snapshot filter ──────────────────────────────────────────


class _FakeCol:
    def __init__(self, col_type=2):
        self._type = col_type

    def properties(self):
        return {"type": self._type}

    def count(self):
        return 0

    def indexes(self):
        return []


class _FakeGraph:
    def __init__(self, props):
        self._props = props

    def properties(self):
        return self._props


class _ScopeFakeDB:
    """A DB with collections users/posts/follows (in graph 'g') plus an
    out-of-graph collection 'unrelated'."""

    def __init__(self, graph_exists=True):
        self._graph_exists = graph_exists

    def collections(self):
        return {n: _FakeCol(3 if n == "follows" else 2) for n in ("users", "posts", "follows", "unrelated")}

    def collection(self, name):
        return _FakeCol(3 if name == "follows" else 2)

    def graphs(self):
        return []

    def graph(self, name):
        if not self._graph_exists or name != "g":
            raise KeyError(name)
        return _FakeGraph(
            {
                "name": "g",
                "edgeDefinitions": [{"collection": "follows", "from": ["users"], "to": ["posts"]}],
                "orphanCollections": [],
            }
        )

    def has_graph(self, name):
        return self._graph_exists and name == "g"

    def properties(self):
        return {"name": "scopedb"}


def test_graph_scope_restricts_collections():
    snap = snapshot_physical_schema(_ScopeFakeDB(), graph_scope="g")
    names = sorted(c["name"] for c in snap["collections"])
    assert names == ["follows", "posts", "users"]  # 'unrelated' excluded


def test_no_graph_scope_includes_all():
    snap = snapshot_physical_schema(_ScopeFakeDB())
    names = sorted(c["name"] for c in snap["collections"])
    assert "unrelated" in names


def test_unknown_graph_scope_raises():
    with pytest.raises(SchemaAnalyzerError) as exc:
        snapshot_physical_schema(_ScopeFakeDB(), graph_scope="does_not_exist")
    assert exc.value.code == "INVALID_ARGUMENT"


def test_empty_vertex_names_dropped_like_sharding_profile():
    # Membership and the sharding profile read a graph's collections through one
    # helper; membership used to keep an empty name the sharding profile dropped.
    from schema_analyzer.sharding_profile import classify_sharding_profile

    graph = {
        "name": "G",
        "edge_definitions": [{"collection": "follows", "from": ["users", ""], "to": ["posts"]}],
        "orphan_collections": ["", "badges"],
    }
    snap = {
        "collections": [{"name": n, "type": "document"} for n in ("users", "posts", "badges")]
        + [{"name": "follows", "type": "edge"}],
        "graphs_detailed": [graph],
    }
    meta = compute_graph_membership(_data(), snap)
    profile = classify_sharding_profile(snap)
    assert meta is not None and profile is not None
    membership_vertices = meta["graphs"]["G"]["vertexCollections"]
    assert membership_vertices == ["badges", "posts", "users"]
    assert profile["graphs"][0]["vertexCollections"] == membership_vertices
