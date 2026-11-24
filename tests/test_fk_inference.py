"""Foreign-key inference over scalar attributes (PRD §6.2).

Ported from `relational_schema_analyzer.fk_inference`; these tests cover the port plus the
ArangoDB-specific additions (`_id`-shape resolution, `_key` targets, edge-backed skip).
"""

import pytest

from schema_analyzer.fk_inference import (
    CollectionShape,
    InferenceOptions,
    infer_foreign_keys,
)
from schema_analyzer.fk_sampler import ArangoValueSampler


def chinook() -> dict[str, CollectionShape]:
    """Chinook's shape: FK columns retained on documents, no edge collections."""
    return {
        "Artist": CollectionShape(name="Artist", fields={"_key": "string", "Name": "string"}, count=275),
        "Album": CollectionShape(
            name="Album",
            fields={"_key": "string", "Title": "string", "ArtistId": "string"},
            count=347,
        ),
        "Track": CollectionShape(
            name="Track",
            fields={"_key": "string", "Name": "string", "AlbumId": "string", "GenreId": "string"},
            count=3503,
        ),
        "Genre": CollectionShape(name="Genre", fields={"_key": "string", "Name": "string"}, count=25),
    }


def _pairs(results):
    return {(r.collection, r.fields[0], r.foreign_collection) for r in results if r.fields}


# ── name-based candidate generation ──────────────────────────────────────────


def test_finds_fk_attributes_with_no_edge_collections():
    """The gap that motivated this: relationships invisible to edge introspection."""
    found = _pairs(infer_foreign_keys(chinook()))
    assert ("Album", "ArtistId", "Artist") in found
    assert ("Track", "AlbumId", "Album") in found
    assert ("Track", "GenreId", "Genre") in found


def test_key_field_is_never_an_fk_origin():
    """`_key` is the referenced side, not a reference."""
    assert not any(r.fields == ["_key"] for r in infer_foreign_keys(chinook()))


def test_targets_the_key_field():
    album = next(r for r in infer_foreign_keys(chinook()) if r.collection == "Album")
    assert album.foreign_fields == ["_key"]


@pytest.mark.parametrize(
    "field_name,expected_method",
    [
        ("artist_id", "name_suffix"),
        ("ArtistId", "camel_suffix"),
        ("artistId", "camel_suffix"),
        ("artistID", "camel_suffix"),
        ("artist_key", "name_suffix"),
        ("artistKey", "camel_suffix"),
    ],
)
def test_snake_and_camel_reference_names_score_equally(field_name, expected_method):
    """ArangoDB uses both conventions, so neither may be treated as the weaker signal.

    RSA scores underscore-less names at 0.45 because SQL identifiers are conventionally
    snake_case and `userid` reads as sloppiness. In a document store `artistId` is a
    deliberate reference — the capital is as much a separator as an underscore.
    """
    shapes = chinook()
    shapes["Album"].fields = {"_key": "string", field_name: "string"}
    result = next(r for r in infer_foreign_keys(shapes) if r.collection == "Album")

    assert result.method == expected_method
    assert result.foreign_collection == "Artist"
    # 0.75 base, with no identical-type bonus: the target is `_key`, whose type carries no
    # information (always string) and so is exempt from the type gate entirely.
    assert result.confidence >= 0.75


def test_missing_separator_is_still_the_weak_pattern():
    """`artistid` has no boundary of any kind — the case the weak pattern is actually for."""
    shapes = chinook()
    shapes["Album"].fields = {"_key": "string", "artistid": "string"}
    result = next(r for r in infer_foreign_keys(shapes) if r.collection == "Album")

    assert result.method == "name_no_underscore"
    assert result.confidence < 0.7


def test_no_self_reference():
    shapes = chinook()
    shapes["Album"].fields["AlbumId"] = "string"
    assert not any(r.collection == "Album" and r.foreign_collection == "Album" for r in infer_foreign_keys(shapes))


def test_incompatible_types_are_rejected():
    """Only against a real target field — `_key` is exempt (see the B3 regressions below)."""
    shapes = chinook()
    shapes["Artist"].fields["ArtistCode"] = "string"
    shapes["Artist"].key_fields = ["ArtistCode"]
    shapes["Album"].fields["ArtistCode"] = "boolean"
    assert ("Album", "ArtistCode", "Artist") not in _pairs(infer_foreign_keys(shapes))


def test_skips_relationships_already_backed_by_an_edge_collection():
    """An attribute duplicating an existing edge is denormalization, not a relationship."""
    found = _pairs(infer_foreign_keys(chinook(), existing_relationships={("Album", "Artist")}))
    assert ("Album", "ArtistId", "Artist") not in found
    assert ("Track", "AlbumId", "Album") in found


def test_confidence_ordering_is_deterministic():
    first = [(r.collection, r.fields, r.confidence) for r in infer_foreign_keys(chinook())]
    second = [(r.collection, r.fields, r.confidence) for r in infer_foreign_keys(chinook())]
    assert first == second
    assert first == sorted(first, key=lambda r: (-r[2], r[0], r[1]))


# ── ARANGO: _id-shaped values ────────────────────────────────────────────────


def test_id_shaped_values_resolve_the_target_directly():
    """`Artist/42` names its collection — no relational analogue, stronger than any name."""
    shapes = chinook()
    shapes["Album"].fields["owner"] = "string"
    shapes["Album"].sample_values["owner"] = ["Artist/1", "Artist/2", "Artist/3", "Artist/4"]

    match = next(r for r in infer_foreign_keys(shapes) if r.fields == ["owner"])
    assert match.method == "id_shape"
    assert match.foreign_collection == "Artist"
    assert match.foreign_fields == ["_id"]
    assert match.confidence > 0.9


def test_id_shape_ignores_unknown_collections():
    shapes = chinook()
    shapes["Album"].fields["ref"] = "string"
    shapes["Album"].sample_values["ref"] = ["NoSuchCollection/1"]
    assert not any(r.fields == ["ref"] for r in infer_foreign_keys(shapes))


def test_partial_id_shape_scores_lower_than_clean():
    shapes = chinook()
    shapes["Album"].fields["a"] = "string"
    shapes["Album"].fields["b"] = "string"
    shapes["Album"].sample_values["a"] = ["Artist/1", "Artist/2", "Artist/3", "Artist/4"]
    shapes["Album"].sample_values["b"] = ["Artist/1", "junk", "junk", "junk"]

    results = {r.fields[0]: r for r in infer_foreign_keys(shapes) if r.method == "id_shape"}
    assert results["a"].confidence > results["b"].confidence


# ── composite pass ───────────────────────────────────────────────────────────


def test_composite_candidate_from_two_fields_to_one_collection():
    shapes = chinook()
    shapes["Artist"].key_fields = ["_key"]
    shapes["Album"].fields["artist_id"] = "string"
    results = infer_foreign_keys(shapes)
    composite = [r for r in results if r.method == "composite"]
    assert composite, "expected a composite candidate"
    assert len(composite[0].fields) == 2


def test_composite_can_be_disabled():
    shapes = chinook()
    shapes["Album"].fields["artist_id"] = "string"
    results = infer_foreign_keys(shapes, options=InferenceOptions(allow_composite=False))
    assert not [r for r in results if r.method == "composite"]


# ── sampler fold ─────────────────────────────────────────────────────────────


def _sampler(mapping, default=None):
    def sample(lc, lf, fc, ff):
        return mapping.get((lc, lf), default)

    return sample


def _recording(calls: list) -> object:
    def sample(lc, lf, fc, ff):
        calls.append((lc, lf))
        return 1.0

    return sample


def test_a_sampler_on_its_own_probes():
    """Passing a sampler is the opt-in (converged with RSA #60); it used to be ignored."""
    calls: list = []
    infer_foreign_keys(chinook(), sampler=_recording(calls))
    assert ("Album", "ArtistId") in calls


def test_sample_overlap_false_still_turns_a_sampler_off():
    calls: list = []
    infer_foreign_keys(chinook(), options=InferenceOptions(sample_overlap=False), sampler=_recording(calls))
    assert calls == []


def test_sample_overlap_without_a_sampler_changes_nothing():
    assert infer_foreign_keys(chinook(), options=InferenceOptions(sample_overlap=True)) == infer_foreign_keys(chinook())


def test_high_containment_raises_confidence():
    opts = InferenceOptions(sample_overlap=True)
    base = next(r for r in infer_foreign_keys(chinook()) if r.collection == "Album")
    boosted = next(
        r for r in infer_foreign_keys(chinook(), options=opts, sampler=_sampler({}, 1.0)) if r.collection == "Album"
    )
    assert boosted.confidence > base.confidence
    assert any("containment" in e for e in boosted.evidence)


def test_zero_containment_vetoes_the_candidate():
    opts = InferenceOptions(sample_overlap=True)
    results = infer_foreign_keys(chinook(), options=opts, sampler=_sampler({}, 0.0))
    assert results == []


def test_unevaluated_sample_is_not_a_veto():
    """`None` means 'no data', which is not evidence against the candidate."""
    opts = InferenceOptions(sample_overlap=True)
    results = infer_foreign_keys(chinook(), options=opts, sampler=_sampler({}, None))
    assert ("Album", "ArtistId", "Artist") in _pairs(results)


def test_sampler_exception_keeps_the_candidate():
    opts = InferenceOptions(sample_overlap=True)

    def boom(lc, lf, fc, ff):
        raise RuntimeError("connection lost")

    assert ("Album", "ArtistId", "Artist") in _pairs(infer_foreign_keys(chinook(), options=opts, sampler=boom))


# ── ArangoValueSampler ───────────────────────────────────────────────────────


class _FakeDB:
    def __init__(self, result):
        self._result = result
        self.queries = []
        self.aql = self

    def execute(self, query, bind_vars=None):
        self.queries.append((query, bind_vars))
        if isinstance(self._result, Exception):
            raise self._result
        return iter([self._result])


def test_sampler_passes_every_name_as_a_bind_variable():
    db = _FakeDB(0.8)
    assert ArangoValueSampler(db)("Album", "ArtistId", "Artist", "_key") == 0.8
    query, bind_vars = db.queries[0]
    assert bind_vars["@localCollection"] == "Album"
    assert bind_vars["localField"] == "ArtistId"
    for name in ("Album", "ArtistId", "Artist"):
        assert name not in query


def test_sampler_returns_none_on_query_failure():
    assert ArangoValueSampler(_FakeDB(RuntimeError("boom")))("A", "b", "C", "_key") is None


def test_sampler_returns_none_for_empty_collection():
    assert ArangoValueSampler(_FakeDB(None))("A", "b", "C", "_key") is None


# ── Case and whitespace (ported from RSA issue #12) ──────────────────────────


class _ScriptedDB:
    """A fake whose answer depends on whether the query normalizes.

    The real failure this guards is not "the number was low" but "the candidate was
    deleted": AQL ``==`` is case-sensitive, so values that are the same key written two
    ways score zero, and zero is the hard veto.
    """

    def __init__(self, strict, normalized):
        self._strict, self._normalized = strict, normalized
        self.queries = []
        self.aql = self

    def execute(self, query, bind_vars=None):
        self.queries.append(query)
        is_norm = "LOWER(TRIM(" in query
        return iter([self._normalized if is_norm else self._strict])


def test_a_key_differing_only_in_case_is_not_vetoed():
    db = _ScriptedDB(strict=0.0, normalized=1.0)
    score = ArangoValueSampler(db)("Invoice", "CustomerCode", "Customer", "code")
    assert score == 1.0, (
        "every sampled value names a real document and differs only in case, so a zero "
        "here is the hard veto deleting a genuine reference"
    )
    assert getattr(score, "normalized", False) is True, (
        "the result must say it needed normalizing, or the evidence cannot tell a reviewer that the raw values disagree"
    )
    assert len(db.queries) == 2


def test_a_strong_strict_match_does_not_pay_for_a_second_probe():
    """The normalized probe cannot use an index, so it must not run when it cannot help."""
    db = _ScriptedDB(strict=1.0, normalized=1.0)
    sampler = ArangoValueSampler(db)
    score = sampler("Album", "ArtistId", "Artist", "_key")
    assert score == 1.0
    assert getattr(score, "normalized", False) is False
    assert len(db.queries) == 1
    assert sampler.probes == 1


def test_normalizing_never_lowers_a_score():
    db = _ScriptedDB(strict=0.6, normalized=0.2)
    assert ArangoValueSampler(db)("A", "b", "C", "d") == 0.6


def test_a_genuine_absence_is_still_a_veto():
    """Normalizing must not turn "not there" into "there"."""
    db = _ScriptedDB(strict=0.0, normalized=0.0)
    assert ArangoValueSampler(db)("A", "b", "C", "d") == 0.0


def test_the_second_probe_is_skipped_when_the_budget_is_gone():
    db = _ScriptedDB(strict=0.0, normalized=1.0)
    sampler = ArangoValueSampler(db, max_probes=1)
    score = sampler("A", "b", "C", "d")
    assert score == 0.0, "budget gone, so the strict answer stands"
    assert sampler.skipped == 1, "and the caller must be able to see it was not measured"
    assert sampler.status()["status"] == "degraded"


def test_probe_budget_is_enforced_and_reported():
    """Exhaustion must surface, never silently reduce the relationship count."""
    sampler = ArangoValueSampler(_FakeDB(1.0), max_probes=2)
    assert sampler("A", "b", "C", "_key") == 1.0
    assert sampler("A", "c", "C", "_key") == 1.0
    assert sampler("A", "d", "C", "_key") is None

    status = sampler.status()
    assert status["status"] == "degraded"
    assert status["unprobedCandidates"] == 1
    assert "budget" in status["reason"]


def test_status_is_ok_when_budget_holds():
    sampler = ArangoValueSampler(_FakeDB(1.0), max_probes=10)
    sampler("A", "b", "C", "_key")
    assert sampler.status()["status"] == "ok"
    assert sampler.status()["reason"] is None


@pytest.mark.parametrize("raw,expected", [(1.5, 1.0), (-0.2, 0.0), ("nope", None)])
def test_sampler_clamps_or_rejects_out_of_range_results(raw, expected):
    assert ArangoValueSampler(_FakeDB(raw))("A", "b", "C", "_key") == expected


# ── regressions found by scoring against Chinook (plan step B3) ──────────────


def test_numeric_reference_to_string_key_is_not_a_type_mismatch():
    """`_key` is always a string; a reference imported from SQL is numeric.

    Type-gating one against the other is a category error, not a mismatch. It rejected
    every genuine candidate — recall 0.0 across the whole Chinook schema — and no unit
    fixture caught it because they all used matching types.
    """
    shapes = {
        "Artist": CollectionShape(name="Artist", fields={"_key": "string", "Name": "string"}),
        "Album": CollectionShape(name="Album", fields={"_key": "string", "ArtistId": "integer"}),
    }
    found = _pairs(infer_foreign_keys(shapes))
    assert ("Album", "ArtistId", "Artist") in found


def test_type_gate_still_applies_to_non_identity_targets():
    """The exemption is only for `_key` / `_id`, not a blanket disabling."""
    shapes = {
        "Artist": CollectionShape(
            name="Artist",
            fields={"_key": "string", "ArtistCode": "string"},
            key_fields=["ArtistCode"],
        ),
        "Album": CollectionShape(name="Album", fields={"_key": "string", "ArtistCode": "boolean"}),
    }
    assert not _pairs(infer_foreign_keys(shapes))


def test_probe_casts_the_sampled_value_for_identity_targets():
    """Comparing a numeric reference to `_key` raw makes every probe return 0.0.

    With `overlap_veto_on_zero` that silently vetoed every real relationship.
    """
    db = _FakeDB(1.0)
    ArangoValueSampler(db)("Album", "ArtistId", "Artist", "_key")
    query, _ = db.queries[0]
    assert "TO_STRING(d[@localField])" in query


def test_probe_does_not_cast_for_ordinary_targets():
    db = _FakeDB(1.0)
    ArangoValueSampler(db)("Album", "ArtistCode", "Artist", "ArtistCode")
    query, _ = db.queries[0]
    assert "TO_STRING" not in query
    assert "RETURN DISTINCT d[@localField]" in query


# ── mapping emission ─────────────────────────────────────────────────────────


def test_renders_a_foreign_key_mapping():
    album = next(r for r in infer_foreign_keys(chinook()) if r.collection == "Album")
    mapping = album.to_mapping()
    assert mapping["style"] == "FOREIGN_KEY"
    assert mapping["fromCollection"] == "Album"
    assert mapping["toCollection"] == "Artist"
    assert mapping["toKeyFields"] == ["_key"]
    assert mapping["enforced"] is False


def test_enforced_is_always_false():
    """ArangoDB enforces no referential constraint — every result is evidence, not proof."""
    assert all(r.to_mapping()["enforced"] is False for r in infer_foreign_keys(chinook()))


# ── RSA convergence: candidate-key (unique, not just _key) FK targets ─────────


def _natural_key_shapes() -> dict[str, CollectionShape]:
    """`documents` keyed by a hash `_key` but with a natural unique `code`; `chunks`
    references it two ways: `document_id` (→ the key) and `document_code` (→ the natural key)."""
    return {
        "documents": CollectionShape(
            name="documents",
            fields={"_key": "string", "code": "string", "filename": "string"},
            unique_fields={"code"},
            count=50,
        ),
        "chunks": CollectionShape(
            name="chunks",
            fields={"_key": "string", "document_id": "string", "document_code": "string", "text": "string"},
            count=500,
        ),
    }


def test_reference_can_target_a_unique_field_not_just_key():
    results = infer_foreign_keys(_natural_key_shapes())
    assert ("chunks", "document_code", "documents") in _pairs(results)
    match = next(r for r in results if r.fields == ["document_code"])
    assert match.foreign_fields == ["code"]  # the natural key, not `_key`
    assert match.method == "name_suffix"


def test_id_suffix_still_targets_the_key_only_not_a_unique_field():
    # `document_id` names the collection → the collection key; it must NOT also
    # propose `documents.code` (that would be noise).
    results = [r for r in infer_foreign_keys(_natural_key_shapes()) if r.fields == ["document_id"]]
    assert all(r.foreign_fields == ["_key"] for r in results)
    assert not any(r.foreign_fields == ["code"] for r in results)


def test_unique_target_ranks_below_the_key_target():
    # Same reference name that could resolve to either: `documents_code` where `documents`
    # also happens to have `_key`. The unique-field resolution carries the −0.05 penalty,
    # so a key-target reference of the same base outranks it.
    shapes = _natural_key_shapes()
    by_key = next(r for r in infer_foreign_keys(shapes) if r.fields == ["document_id"])
    by_unique = next(r for r in infer_foreign_keys(shapes) if r.fields == ["document_code"])
    assert by_unique.confidence < by_key.confidence


def test_non_unique_field_is_never_a_target():
    # If `code` is NOT unique, `document_code` must not be proposed — uniqueness is what
    # supplies the many-to-one direction (the report's low-cardinality-containment guard).
    shapes = _natural_key_shapes()
    shapes["documents"].unique_fields = set()
    assert not any(r.fields == ["document_code"] for r in infer_foreign_keys(shapes))


def test_upper_case_collections_are_matched_by_name():
    # CUSTOMER_ID must resolve to CUSTOMERS. With a case-sensitive singularize the
    # target's name variants were {customers, customerss, customerses} -- never
    # "customer" -- so the relationship was silently missed.
    shapes = {
        "CUSTOMERS": CollectionShape(name="CUSTOMERS", fields={"_key": "string", "NAME": "string"}, count=50),
        "ORDERS": CollectionShape(name="ORDERS", fields={"_key": "string", "CUSTOMER_ID": "string"}, count=500),
    }
    assert ("ORDERS", "CUSTOMER_ID", "CUSTOMERS") in _pairs(infer_foreign_keys(shapes))
