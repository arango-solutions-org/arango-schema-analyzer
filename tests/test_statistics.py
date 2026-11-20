"""
Tests for ``schema_analyzer.statistics`` — issue #3.

Covers the six acceptance criteria from the issue:

  AC1. A ``statistics`` block is emitted when a live DB handle is present,
       absent otherwise (``statistics_status == "skipped_no_db"``).
  AC2. Per-collection counts and ``is_edge`` flags are computed from a
       single ``LENGTH`` AQL per collection.
  AC3. Per-entity ``estimated_count`` tracks ``style`` (``COLLECTION``
       uses total, ``LABEL`` uses a filtered ``COLLECT``).
  AC4. Per-relationship bundle carries ``edge_count``, ``source_count``,
       ``target_count``, ``avg_out_degree``, ``avg_in_degree``,
       ``cardinality_pattern`` and ``selectivity``.
  AC5. Cardinality classification follows the documented thresholds.
  AC6. Zero-source / zero-target guards collapse to the documented
       defaults (``0.0`` degrees, selectivity=1.0) rather than blowing up.
"""

from __future__ import annotations

import pytest

from schema_analyzer.statistics import (
    CARDINALITY_THRESHOLD,
    STATISTICS_STATUS_OK,
    STATISTICS_STATUS_SKIPPED_NO_DB,
    _classify_cardinality,
    compute_statistics,
)

# ── In-memory fake DB ─────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeAQL:
    """Minimal AQL shim that recognises the two queries ``statistics.py`` issues."""

    def __init__(self, totals: dict[str, int], filtered: dict[tuple[str, str, object], int]):
        self._totals = totals
        self._filtered = filtered
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, bind_vars=None):
        bv = bind_vars or {}
        self.calls.append((query, bv))
        if "LENGTH(@@c)" in query:
            col = bv["@c"]
            return _FakeCursor([self._totals[col]])
        if "COLLECT WITH COUNT" in query:
            key = (bv["@c"], bv["field"], bv["val"])
            return _FakeCursor([self._filtered[key]])
        raise AssertionError(f"unexpected AQL: {query!r}")


class _FakeDB:
    def __init__(self, totals, filtered=None):
        self.aql = _FakeAQL(totals, filtered or {})


# ── _classify_cardinality ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("avg_out", "avg_in", "expected"),
    [
        (1.0, 1.0, "1:1"),
        (CARDINALITY_THRESHOLD, CARDINALITY_THRESHOLD, "1:1"),
        (5.0, 1.0, "1:N"),
        (1.0, 5.0, "N:1"),
        (5.0, 5.0, "N:M"),
        (0.0, 0.0, "1:1"),
    ],
)
def test_classify_cardinality(avg_out: float, avg_in: float, expected: str) -> None:
    assert _classify_cardinality(avg_out, avg_in) == expected


# ── compute_statistics — no DB branch ────────────────────────────────


def test_compute_statistics_returns_none_when_db_is_none() -> None:
    assert compute_statistics(None, {"collections": []}, {"entities": {}, "relationships": {}}) is None


def test_compute_statistics_returns_none_when_all_aql_calls_fail() -> None:
    class _NoAQL:
        pass

    snapshot = {"collections": [{"name": "x", "type": "document"}]}
    assert compute_statistics(_NoAQL(), snapshot, {"entities": {}, "relationships": {}}) is None


# ── compute_statistics — happy path ─────────────────────────────────


def test_compute_statistics_covers_entities_and_relationships() -> None:
    snapshot = {
        "collections": [
            {"name": "Person", "type": "document"},
            {"name": "Movie", "type": "document"},
            {"name": "ACTED_IN", "type": "edge"},
        ]
    }
    totals = {"Person": 100, "Movie": 50, "ACTED_IN": 200}
    db = _FakeDB(totals)

    physical_mapping = {
        "entities": {
            "Person": {"style": "COLLECTION", "collectionName": "Person"},
            "Movie": {"style": "COLLECTION", "collectionName": "Movie"},
        },
        "relationships": {
            "ACTED_IN": {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": "ACTED_IN",
            }
        },
    }
    conceptual_schema = {"relationships": [{"type": "ACTED_IN", "fromEntity": "Person", "toEntity": "Movie"}]}

    out = compute_statistics(db, snapshot, physical_mapping, conceptual_schema)
    assert out is not None
    assert out["status"] == STATISTICS_STATUS_OK
    assert "computed_at" in out

    cols = out["collections"]
    assert cols["Person"] == {"count": 100, "is_edge": False}
    assert cols["Movie"] == {"count": 50, "is_edge": False}
    assert cols["ACTED_IN"] == {"count": 200, "is_edge": True}

    assert out["entities"] == {
        "Person": {"estimated_count": 100},
        "Movie": {"estimated_count": 50},
    }

    rel = out["relationships"]["ACTED_IN"]
    assert rel["edge_count"] == 200
    assert rel["source_count"] == 100
    assert rel["target_count"] == 50
    # 200 / 100 = 2.0, 200 / 50 = 4.0 → both above threshold → "N:M"
    assert rel["avg_out_degree"] == 2.0
    assert rel["avg_in_degree"] == 4.0
    assert rel["cardinality_pattern"] == "N:M"
    # 200 / (100 * 50) = 0.04
    assert rel["selectivity"] == 0.04


def test_compute_statistics_label_style_entity_uses_filtered_count() -> None:
    snapshot = {
        "collections": [
            {"name": "Node", "type": "document"},
        ]
    }
    totals = {"Node": 300}
    filtered = {("Node", "_type", "Person"): 120}
    db = _FakeDB(totals, filtered)

    physical_mapping = {
        "entities": {
            "Person": {
                "style": "LABEL",
                "collectionName": "Node",
                "typeField": "_type",
                "typeValue": "Person",
            }
        },
        "relationships": {},
    }

    out = compute_statistics(db, snapshot, physical_mapping, {"relationships": []})
    assert out is not None
    assert out["collections"]["Node"]["count"] == 300
    assert out["entities"]["Person"]["estimated_count"] == 120


def test_compute_statistics_labels_style_entity_counts_its_label_only() -> None:
    # A LABELS entity shares its collection with other labels; the collection total
    # (45 here) would overstate it.
    # The stored label (labelValue, "FIN_ACCOUNT") differs from the entity key, so the
    # count proves labelValue is what is counted; Order has none and falls back to its key.
    db = _FakeDB({"nodes": 45}, {("nodes", "labels", "FIN_ACCOUNT"): 25, ("nodes", "labels", "Order"): 15})
    physical_mapping = {
        "entities": {
            "FinAccount": {
                "style": "LABELS",
                "collectionName": "nodes",
                "labelsField": "labels",
                "labelValue": "FIN_ACCOUNT",
            },
            "Order": {"style": "LABELS", "collectionName": "nodes", "labelsField": "labels"},
        },
        "relationships": {},
    }
    out = compute_statistics(db, {"collections": [{"name": "nodes", "type": "document"}]}, physical_mapping, {})
    assert out is not None
    assert out["entities"]["FinAccount"]["estimated_count"] == 25
    assert out["entities"]["Order"]["estimated_count"] == 15
    assert any("@val IN d[@field]" in q for q, _ in db.aql.calls)


def test_compute_statistics_generic_with_type_uses_filtered_edge_count() -> None:
    snapshot = {
        "collections": [
            {"name": "Person", "type": "document"},
            {"name": "Movie", "type": "document"},
            {"name": "REL", "type": "edge"},
        ]
    }
    totals = {"Person": 10, "Movie": 5, "REL": 40}
    filtered = {("REL", "type", "ACTED_IN"): 25}
    db = _FakeDB(totals, filtered)

    physical_mapping = {
        "entities": {
            "Person": {"style": "COLLECTION", "collectionName": "Person"},
            "Movie": {"style": "COLLECTION", "collectionName": "Movie"},
        },
        "relationships": {
            "ACTED_IN": {
                "style": "GENERIC_WITH_TYPE",
                "edgeCollectionName": "REL",
                "typeField": "type",
                "typeValue": "ACTED_IN",
            }
        },
    }
    conceptual_schema = {"relationships": [{"type": "ACTED_IN", "fromEntity": "Person", "toEntity": "Movie"}]}

    out = compute_statistics(db, snapshot, physical_mapping, conceptual_schema)
    rel = out["relationships"]["ACTED_IN"]
    assert rel["edge_count"] == 25  # not the edge collection total of 40
    assert rel["source_count"] == 10
    assert rel["target_count"] == 5


def test_compute_statistics_zero_source_collapses_to_defaults() -> None:
    snapshot = {
        "collections": [
            {"name": "A", "type": "document"},
            {"name": "B", "type": "document"},
            {"name": "E", "type": "edge"},
        ]
    }
    totals = {"A": 0, "B": 5, "E": 0}
    db = _FakeDB(totals)

    physical_mapping = {
        "entities": {
            "A": {"style": "COLLECTION", "collectionName": "A"},
            "B": {"style": "COLLECTION", "collectionName": "B"},
        },
        "relationships": {"R": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "E"}},
    }
    conceptual_schema = {"relationships": [{"type": "R", "fromEntity": "A", "toEntity": "B"}]}
    out = compute_statistics(db, snapshot, physical_mapping, conceptual_schema)
    rel = out["relationships"]["R"]
    assert rel["edge_count"] == 0
    assert rel["source_count"] == 0
    assert rel["avg_out_degree"] == 0.0
    assert rel["avg_in_degree"] == 0.0
    assert rel["selectivity"] == 1.0


def test_compute_statistics_cardinality_one_to_one() -> None:
    snapshot = {
        "collections": [
            {"name": "A", "type": "document"},
            {"name": "B", "type": "document"},
            {"name": "E", "type": "edge"},
        ]
    }
    totals = {"A": 100, "B": 100, "E": 100}
    db = _FakeDB(totals)
    pm = {
        "entities": {
            "A": {"style": "COLLECTION", "collectionName": "A"},
            "B": {"style": "COLLECTION", "collectionName": "B"},
        },
        "relationships": {"R": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "E"}},
    }
    cs = {"relationships": [{"type": "R", "fromEntity": "A", "toEntity": "B"}]}
    out = compute_statistics(db, snapshot, pm, cs)
    rel = out["relationships"]["R"]
    assert rel["avg_out_degree"] == 1.0
    assert rel["avg_in_degree"] == 1.0
    assert rel["cardinality_pattern"] == "1:1"


def test_compute_statistics_cardinality_one_to_many_and_many_to_one() -> None:
    snapshot = {
        "collections": [
            {"name": "A", "type": "document"},
            {"name": "B", "type": "document"},
            {"name": "E1", "type": "edge"},
            {"name": "E2", "type": "edge"},
        ]
    }
    totals = {"A": 10, "B": 1000, "E1": 200, "E2": 200}
    db = _FakeDB(totals)
    pm = {
        "entities": {
            "A": {"style": "COLLECTION", "collectionName": "A"},
            "B": {"style": "COLLECTION", "collectionName": "B"},
        },
        "relationships": {
            "R1N": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "E1"},
            "RN1": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "E2"},
        },
    }
    cs = {
        "relationships": [
            {"type": "R1N", "fromEntity": "A", "toEntity": "B"},
            {"type": "RN1", "fromEntity": "B", "toEntity": "A"},
        ]
    }
    out = compute_statistics(db, snapshot, pm, cs)
    # A (10) → B (100), 200 edges: avg_out=20, avg_in=2.0 → "1:N"
    assert out["relationships"]["R1N"]["cardinality_pattern"] == "1:N"
    # B (100) → A (10), 200 edges: avg_out=2.0, avg_in=20 → "N:1"
    assert out["relationships"]["RN1"]["cardinality_pattern"] == "N:1"


# ── Analyzer integration ────────────────────────────────────────────


def test_analyzer_stamps_statistics_status_when_fake_db_has_no_aql(monkeypatch) -> None:
    """When the DB handle doesn't support AQL, the analyzer must still produce
    a result — the statistics block is simply absent and ``statistics_status``
    falls back to ``"skipped_no_db"``.
    """
    from schema_analyzer.analyzer import AgenticSchemaAnalyzer

    class _FakeCollection:
        def properties(self):
            return {"type": 2, "isSystem": False}

        def count(self):
            return 0

        def indexes(self):
            return []

    class _NoAQLDB:
        def __init__(self):
            self._cols = {"Person": _FakeCollection()}

        def collections(self):
            return self._cols

        def graphs(self):
            return []

    analyzer = AgenticSchemaAnalyzer()  # no provider → baseline path
    res = analyzer.analyze_physical_schema(_NoAQLDB(), sample_limit_per_collection=0, use_cache=False)
    assert res.metadata.statistics is None
    assert res.metadata.statistics_status == STATISTICS_STATUS_SKIPPED_NO_DB


# ── Polymorphic endpoints and supertypes (LDBC-shaped mappings) ──────────


def _polymorphic_db() -> _FakeDB:
    totals = {"post": 30, "comment": 20, "person": 5, "has_creator": 50, "reply_of": 20}
    return _FakeDB(totals)


_POLYMORPHIC_SNAPSHOT = {
    "collections": [
        {"name": "post", "type": "document"},
        {"name": "comment", "type": "document"},
        {"name": "person", "type": "document"},
        {"name": "has_creator", "type": "edge"},
        {"name": "reply_of", "type": "edge"},
    ]
}
_POLYMORPHIC_MAPPING = {
    "entities": {
        "Post": {"style": "COLLECTION", "collectionName": "post"},
        "Comment": {"style": "COLLECTION", "collectionName": "comment"},
        "Person": {"style": "COLLECTION", "collectionName": "person"},
        "Message": {"style": "UNION", "members": ["Post", "Comment"]},
    },
    "relationships": {
        "HAS_CREATOR": {
            "style": "DEDICATED_COLLECTION",
            "edgeCollectionName": "has_creator",
            "domain": ["Post", "Comment"],
            "range": "Person",
        },
        "REPLY_OF": {
            "style": "DEDICATED_COLLECTION",
            "edgeCollectionName": "reply_of",
            "domain": "Comment",
            "range": ["Post", "Comment"],
        },
    },
}


def test_a_list_valued_endpoint_counts_every_label() -> None:
    # A list endpoint was looked up as a dict key: TypeError, and the whole block was lost.
    out = compute_statistics(_polymorphic_db(), _POLYMORPHIC_SNAPSHOT, _POLYMORPHIC_MAPPING, {})
    assert out is not None and out["status"] == STATISTICS_STATUS_OK
    creator = out["relationships"]["HAS_CREATOR"]
    assert creator["source_count"] == 50  # 30 posts + 20 comments
    assert creator["avg_in_degree"] == 10.0  # 50 messages over 5 people
    assert out["relationships"]["REPLY_OF"]["target_count"] == 50


def test_conceptual_list_endpoints_are_read_too() -> None:
    mapping = {
        **_POLYMORPHIC_MAPPING,
        "relationships": {
            k: {kk: vv for kk, vv in v.items() if kk not in ("domain", "range")}
            for k, v in _POLYMORPHIC_MAPPING["relationships"].items()
        },
    }
    conceptual = {"relationships": [{"type": "HAS_CREATOR", "fromEntity": ["Post", "Comment"], "toEntity": "Person"}]}
    out = compute_statistics(_polymorphic_db(), _POLYMORPHIC_SNAPSHOT, mapping, conceptual)
    assert out["relationships"]["HAS_CREATOR"]["source_count"] == 50


def test_a_union_supertype_is_its_members_total() -> None:
    out = compute_statistics(_polymorphic_db(), _POLYMORPHIC_SNAPSHOT, _POLYMORPHIC_MAPPING, {})
    assert out["entities"]["Message"]["estimated_count"] == 50


def test_a_union_with_its_members_in_the_conceptual_schema() -> None:
    mapping = {**_POLYMORPHIC_MAPPING, "entities": {**_POLYMORPHIC_MAPPING["entities"], "Message": {"style": "UNION"}}}
    conceptual = {"entities": [{"name": "Message", "subtypes": ["Post", "Comment"]}]}
    out = compute_statistics(_polymorphic_db(), _POLYMORPHIC_SNAPSHOT, mapping, conceptual)
    assert out["entities"]["Message"]["estimated_count"] == 50


def test_a_multi_label_supertype_counts_its_label_like_labels() -> None:
    # arango-query-core's MULTI_LABEL: a supertype in a shared collection, found by its label.
    db = _FakeDB({"nodes": 90}, {("nodes", "labels", "Message"): 50})
    mapping = {
        "entities": {
            "Message": {
                "style": "MULTI_LABEL",
                "collectionName": "nodes",
                "labelsField": "labels",
                "labelValue": "Message",
            }
        },
        "relationships": {},
    }
    out = compute_statistics(db, {"collections": [{"name": "nodes", "type": "document"}]}, mapping, {})
    assert out["entities"]["Message"]["estimated_count"] == 50  # not the collection's 90
