"""Tests for incremental re-analysis + change-state detection (PRD §3.13.3)."""

from __future__ import annotations

from schema_analyzer.analyzer import AgenticSchemaAnalyzer
from schema_analyzer.incremental import (
    CHANGE_NO_CACHE,
    CHANGE_SHAPE_CHANGED,
    CHANGE_STATS_CHANGED,
    CHANGE_UNCHANGED,
    assess_change_state,
    coerce_prior,
)
from schema_analyzer.types import AnalysisResult


class _Col:
    def __init__(self, col_type=2, count=0, indexes=None):
        self._t = col_type
        self._c = count
        self._i = indexes or []

    def properties(self):
        return {"type": self._t}

    def count(self):
        return self._c

    def indexes(self):
        return self._i


class _FakeDB:
    """Fingerprint-able fake DB. ``counts``/``shape`` are tunable to drive states."""

    def __init__(self, *, name="db", cols=None):
        self.name = name
        self._cols = cols or {"users": _Col(2, count=3), "follows": _Col(3, count=2)}

    def collections(self):
        return [{"name": n, "type": c._t} for n, c in self._cols.items()]

    def collection(self, name):
        return self._cols[name]


def _fp(db):
    from schema_analyzer.snapshot import fingerprint_physical_counts, fingerprint_physical_shape

    return fingerprint_physical_shape(db), fingerprint_physical_counts(db)


def test_change_state_no_cache():
    db = _FakeDB()
    st = assess_change_state(db)
    assert st["status"] == CHANGE_NO_CACHE
    assert st["shapeFingerprint"] and st["countsFingerprint"]


def test_change_state_unchanged():
    db = _FakeDB()
    shape, counts = _fp(db)
    st = assess_change_state(db, prior_shape=shape, prior_counts=counts)
    assert st["status"] == CHANGE_UNCHANGED


def test_change_state_stats_changed():
    db = _FakeDB()
    shape, counts = _fp(db)
    db._cols["users"]._c = 99  # row count changes, shape identical
    st = assess_change_state(db, prior_shape=shape, prior_counts=counts)
    assert st["status"] == CHANGE_STATS_CHANGED
    assert st["shapeFingerprint"] == shape  # shape unchanged


def test_change_state_shape_changed():
    db = _FakeDB()
    shape, counts = _fp(db)
    db._cols["extra"] = _Col(2, count=1)  # new collection -> shape differs
    st = assess_change_state(db, prior_shape=shape, prior_counts=counts)
    assert st["status"] == CHANGE_SHAPE_CHANGED


def test_coerce_prior_from_dict():
    d = {
        "conceptualSchema": {"entities": [{"name": "User"}], "relationships": [], "properties": []},
        "physicalMapping": {
            "entities": {"User": {"style": "COLLECTION", "collectionName": "users"}},
            "relationships": {},
        },
        "metadata": {
            "confidence": 0.7,
            "timestamp": "2026-01-01T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 0},
            "detectedPatterns": [],
            "shapeFingerprint": "S",
            "countsFingerprint": "C",
        },
    }
    pr = coerce_prior(d)
    assert isinstance(pr, AnalysisResult)
    assert pr.metadata.shape_fingerprint == "S"
    assert pr.conceptual_schema["entities"][0]["name"] == "User"


# ── analyze_incremental orchestration ────────────────────────────────────


def _prior_result(db) -> AnalysisResult:
    # A real baseline analysis stamps shape/counts fingerprints.
    return AgenticSchemaAnalyzer().analyze_physical_schema(db, use_cache=False)


def test_incremental_unchanged_returns_prior_annotated():
    db = _FakeDB()
    prior = _prior_result(db)
    assert prior.metadata.shape_fingerprint is not None
    res = AgenticSchemaAnalyzer().analyze_incremental(db, prior=prior, use_cache=False)
    assert res.metadata.incremental_refresh == "unchanged"
    # conceptual/physical preserved verbatim
    assert res.conceptual_schema == prior.conceptual_schema
    assert res.physical_mapping == prior.physical_mapping


def test_incremental_stats_changed_refreshes_only_stats():
    db = _FakeDB()
    prior = _prior_result(db)
    db._cols["users"]._c = 500  # counts change, shape same
    res = AgenticSchemaAnalyzer().analyze_incremental(db, prior=prior, use_cache=False)
    assert res.metadata.incremental_refresh == "stats_only"
    assert res.conceptual_schema == prior.conceptual_schema  # preserved
    assert res.physical_mapping == prior.physical_mapping
    assert res.metadata.counts_fingerprint != prior.metadata.counts_fingerprint


def test_incremental_shape_changed_full_reanalysis():
    db = _FakeDB()
    prior = _prior_result(db)
    db._cols["orgs"] = _Col(2, count=1)  # new collection -> full re-analyze
    res = AgenticSchemaAnalyzer().analyze_incremental(db, prior=prior, use_cache=False)
    # full analysis does not set incremental_refresh
    assert res.metadata.incremental_refresh is None
    assert "Org" in res.physical_mapping["entities"] or "orgs" in {
        e.get("collectionName") for e in res.physical_mapping["entities"].values()
    }


def test_incremental_no_prior_is_full_analysis():
    db = _FakeDB()
    res = AgenticSchemaAnalyzer().analyze_incremental(db, use_cache=False)
    assert res.metadata.incremental_refresh is None
    assert res.metadata.shape_fingerprint is not None


# ── Temporal lineage across incremental branches (PRD §3.13.2) ───────────


def test_full_analysis_stamps_first_seen_and_last_validated():
    db = _FakeDB()
    res = AgenticSchemaAnalyzer().analyze_physical_schema(db, use_cache=False)
    ent = next(iter(res.physical_mapping["entities"].values()))
    assert isinstance(ent.get("firstSeenAt"), str)
    assert ent["lastValidatedAt"] == ent["firstSeenAt"]


def test_incremental_unchanged_revalidates_but_keeps_first_seen():
    db = _FakeDB()
    prior = _prior_result(db)
    ent0 = next(iter(prior.physical_mapping["entities"].values()))
    first_seen = ent0["firstSeenAt"]
    res = AgenticSchemaAnalyzer().analyze_incremental(db, prior=prior, use_cache=False)
    ent = next(iter(res.physical_mapping["entities"].values()))
    assert ent["firstSeenAt"] == first_seen
    assert isinstance(ent.get("lastValidatedAt"), str)
    assert ent["lastValidatedAt"] >= first_seen


def test_incremental_shape_changed_carries_first_seen_forward():
    db = _FakeDB()
    prior = _prior_result(db)
    surviving = next(iter(prior.physical_mapping["entities"]))
    first_seen = prior.physical_mapping["entities"][surviving]["firstSeenAt"]
    db._cols["orgs"] = _Col(2, count=1)  # shape change -> full re-analysis
    res = AgenticSchemaAnalyzer().analyze_incremental(db, prior=prior, use_cache=False)
    assert res.physical_mapping["entities"][surviving]["firstSeenAt"] == first_seen
    new_names = set(res.physical_mapping["entities"]) - set(prior.physical_mapping["entities"])
    for name in new_names:
        assert res.physical_mapping["entities"][name]["firstSeenAt"] >= first_seen


def test_incremental_does_not_mutate_prior_timestamps():
    # Revalidation must stamp the RETURNED result, never the caller's prior
    # (regression: earlier the unchanged/stats_only branches mutated prior in place).
    db = _FakeDB()
    prior = _prior_result(db)
    ent_key = next(iter(prior.physical_mapping["entities"]))
    prior_last_validated = prior.physical_mapping["entities"][ent_key]["lastValidatedAt"]

    res = AgenticSchemaAnalyzer().analyze_incremental(db, prior=prior, use_cache=False)
    # prior untouched...
    assert prior.physical_mapping["entities"][ent_key]["lastValidatedAt"] == prior_last_validated
    # ...result carries a (re)stamp and owns its own dict
    assert res.physical_mapping is not prior.physical_mapping
    assert isinstance(res.physical_mapping["entities"][ent_key]["lastValidatedAt"], str)


# ── Bitemporal stamping (PRD §3.13.5) ────────────────────────────────────────

_T0 = "2020-01-01T00:00:00Z"


def _pin_valid_from(result, frm=_T0):
    """Return a copy of ``result`` with an explicit older validTime.from, so the
    carry-back is unambiguous regardless of wall-clock resolution."""
    return AnalysisResult(
        conceptual_schema=result.conceptual_schema,
        physical_mapping=result.physical_mapping,
        metadata=result.metadata.model_copy(update={"valid_time": {"from": frm}, "valid_time_source": "observed"}),
    )


def test_valid_time_fresh_analysis_is_observed():
    res = AgenticSchemaAnalyzer().analyze_physical_schema(_FakeDB(), use_cache=False)
    m = res.metadata
    assert m.valid_time_source == "observed"
    assert m.valid_time == {"from": m.analysis_completed_at}
    assert m.transaction_time == m.analysis_completed_at
    assert m.predecessor_fingerprint is None


def test_valid_time_unchanged_carries_continuity():
    db = _FakeDB()
    prior = _pin_valid_from(_prior_result(db))
    res = AgenticSchemaAnalyzer().analyze_incremental(db, prior=prior, use_cache=False)
    assert res.metadata.incremental_refresh == "unchanged"
    assert res.metadata.valid_time == {"from": _T0}
    assert res.metadata.valid_time_source == "fingerprint-continuity"
    assert res.metadata.predecessor_fingerprint == prior.metadata.shape_fingerprint
    # transaction time still advances even though valid time is carried back
    assert res.metadata.transaction_time == res.metadata.analysis_completed_at


def test_valid_time_chain_three_unchanged_runs_stays_t0():
    db = _FakeDB()
    an = AgenticSchemaAnalyzer()
    r1 = _pin_valid_from(_prior_result(db))
    r2 = an.analyze_incremental(db, prior=r1, use_cache=False)
    r3 = an.analyze_incremental(db, prior=r2, use_cache=False)
    assert r3.metadata.valid_time == {"from": _T0}
    assert r3.metadata.valid_time_source == "fingerprint-continuity"


def test_valid_time_stats_changed_carries_continuity():
    db = _FakeDB()
    prior = _pin_valid_from(_prior_result(db))
    db._cols["users"]._c = 999  # counts move, shape identical -> stats_changed
    res = AgenticSchemaAnalyzer().analyze_incremental(db, prior=prior, use_cache=False)
    assert res.metadata.incremental_refresh == "stats_only"
    assert res.metadata.valid_time == {"from": _T0}
    assert res.metadata.valid_time_source == "fingerprint-continuity"
    assert res.metadata.predecessor_fingerprint == prior.metadata.shape_fingerprint


def test_valid_time_shape_changed_resets_to_observed():
    db = _FakeDB()
    prior = _pin_valid_from(_prior_result(db))
    prior_shape = prior.metadata.shape_fingerprint
    db._cols["orgs"] = _Col(2, count=1)  # new collection -> shape_changed (full re-analysis)
    res = AgenticSchemaAnalyzer().analyze_incremental(db, prior=prior, use_cache=False)
    assert res.metadata.valid_time_source == "observed"
    assert res.metadata.valid_time == {"from": res.metadata.analysis_completed_at}
    assert res.metadata.valid_time["from"] != _T0
    assert res.metadata.predecessor_fingerprint == prior_shape


def test_excluded_collections_never_trigger_a_rerun():
    """``exclude_collections`` holds across runs (PRD §3.13.3 / §4.1, REQ-058).

    A database-resident cache collection changes on every run. The fingerprints a
    result carries must be the ones its change state was compared with, or the next
    run sees a change that is only in the excluded collection.
    """
    db = _FakeDB(cols={"users": _Col(2, count=3), "follows": _Col(3, count=2), "cache": _Col(2, count=1)})
    analyzer = AgenticSchemaAnalyzer()
    first = analyzer.analyze_physical_schema(db, use_cache=False)  # whole-database fingerprints

    # Compared with the exclusion, the prior's fingerprints differ: one full re-analysis.
    second = analyzer.analyze_incremental(db, prior=first, exclude_collections=["cache"], use_cache=False)
    assert second.metadata.incremental_refresh is None

    db._cols["cache"]._c = 50  # only the excluded collection moved
    third = analyzer.analyze_incremental(db, prior=second, exclude_collections=["cache"], use_cache=False)
    assert third.metadata.incremental_refresh == "unchanged"

    db._cols["users"]._c = 30  # real data moved: stats-only refresh
    fourth = analyzer.analyze_incremental(db, prior=third, exclude_collections=["cache"], use_cache=False)
    assert fourth.metadata.incremental_refresh == "stats_only"

    db._cols["cache"]._c = 99  # only the excluded collection moved again
    fifth = analyzer.analyze_incremental(db, prior=fourth, exclude_collections=["cache"], use_cache=False)
    assert fifth.metadata.incremental_refresh == "unchanged"
