"""End-to-end wiring: does an ordinary analysis actually produce FKs and taxonomy?

The unit suites prove the algorithms; this proves they are *reachable*. Both capabilities
existed and were fully tested for a while before anything called them, so an analysis
produced exactly what it had before. These tests exist so that cannot recur silently.

No database: `analyze_physical_schema` accepts a prepared `_snapshot`, so the whole
enrichment path runs against a hand-built one.
"""

import copy
from typing import Any

import pytest

from schema_analyzer import AgenticSchemaAnalyzer


def _doc(name: str, fields: list[str], count: int = 10, samples: list[dict] | None = None) -> dict:
    entry: dict[str, Any] = {
        "name": name,
        "type": "document",
        "count": count,
        "properties": {},
        "indexes": [],
        "candidate_type_fields": [],
        "observed_fields": {"fields": sorted(fields)},
        "inferred_entity_type": name,
    }
    if samples is not None:
        entry["sample_documents"] = samples
    return entry


def chinook_snapshot(*, samples: bool = False) -> dict:
    """FK columns retained on documents, no edge collections — the motivating shape."""
    return {
        "collections": [
            _doc(
                "Artist",
                ["_key", "Name"],
                275,
                samples=[{"_key": "1", "Name": "AC/DC"}] if samples else None,
            ),
            _doc(
                "Album",
                ["_key", "Title", "ArtistId"],
                347,
                samples=[{"_key": "1", "Title": "For Those About To Rock", "ArtistId": 1}] if samples else None,
            ),
            _doc("Genre", ["_key", "Name"], 25),
            _doc("Track", ["_key", "Name", "AlbumId", "GenreId"], 3503),
        ]
    }


def analyze(snapshot: dict, **flags) -> Any:
    return AgenticSchemaAnalyzer(**flags).analyze_physical_schema(db=None, use_cache=False, _snapshot=snapshot)


# ── foreign keys ─────────────────────────────────────────────────────────────


def test_analysis_finds_no_foreign_keys_by_default():
    """Off unless asked for — the flag is the contract, not an accident of the data."""
    result = analyze(chinook_snapshot())
    styles = {r.get("style") for r in result.physical_mapping.get("relationships", {}).values()}
    assert "FOREIGN_KEY" not in styles
    assert result.metadata.foreign_key_status is None


def test_enabling_detection_puts_foreign_keys_in_the_mapping():
    result = analyze(chinook_snapshot(), detect_foreign_keys=True)
    fks = {
        name: r
        for name, r in result.physical_mapping.get("relationships", {}).items()
        if r.get("style") == "FOREIGN_KEY"
    }
    assert fks, "detection ran but nothing reached physicalMapping.relationships"

    pairs = {(r["fromCollection"], r["toCollection"]) for r in fks.values()}
    assert ("Album", "Artist") in pairs
    assert ("Track", "Album") in pairs
    assert ("Track", "Genre") in pairs


def test_foreign_keys_also_reach_the_conceptual_schema():
    """A physical mapping with no conceptual counterpart is invisible to consumers."""
    result = analyze(chinook_snapshot(), detect_foreign_keys=True)
    rels = result.conceptual_schema.get("relationships", [])
    assert {(r["fromEntity"], r["toEntity"]) for r in rels} >= {("Album", "Artist")}
    assert all(r.get("type") for r in rels)


def test_status_is_reported():
    result = analyze(chinook_snapshot(), detect_foreign_keys=True)
    status = result.metadata.foreign_key_status
    assert status["status"] == "ok"
    assert status["added"] >= 3
    assert status["sampled"] is False  # no db handle, so no containment probe


@pytest.mark.parametrize("sample_fk_overlap", [False, True])
def test_containment_probing_is_opt_in_even_with_a_database(sample_fk_overlap, monkeypatch):
    """Probing is a cross-collection DB cost (PRD §6.2), so an analysis does it only when
    ``sample_fk_overlap`` is set. Passing a sampler turns probing on in ``fk_inference``,
    so the analyzer must not build one otherwise, even when it holds a database handle."""
    built: list = []

    class _Sampler:
        def __init__(self, db):
            built.append(db)

        def __call__(self, *args):
            return None

        def status(self):
            return {"status": "ok"}

    monkeypatch.setattr("schema_analyzer.fk_sampler.ArangoValueSampler", _Sampler)
    analyzer = AgenticSchemaAnalyzer(detect_foreign_keys=True, sample_fk_overlap=sample_fk_overlap)
    analyzer._db = object()
    plain = analyze(chinook_snapshot())
    data = {
        "conceptualSchema": copy.deepcopy(plain.conceptual_schema),
        "physicalMapping": copy.deepcopy(plain.physical_mapping),
    }
    status = analyzer._detect_attribute_relationships(data, chinook_snapshot())

    assert built == ([analyzer._db] if sample_fk_overlap else [])
    assert status is not None and status["sampled"] is sample_fk_overlap


def test_detection_is_recorded_as_a_pattern():
    result = analyze(chinook_snapshot(), detect_foreign_keys=True)
    assert "attribute_foreign_key" in result.metadata.detected_patterns


def test_enforced_is_always_false():
    result = analyze(chinook_snapshot(), detect_foreign_keys=True)
    for rel in result.physical_mapping.get("relationships", {}).values():
        if rel.get("style") == "FOREIGN_KEY":
            assert rel["enforced"] is False


def test_id_shaped_values_are_used_when_samples_are_present():
    """`_id`-shape detection needs sampled documents; without them it simply cannot run."""
    snapshot = chinook_snapshot(samples=True)
    snapshot["collections"][1]["sample_documents"] = [
        {"_key": "1", "Title": "T", "ArtistId": 1, "owner": "Artist/1"},
        {"_key": "2", "Title": "U", "ArtistId": 2, "owner": "Artist/2"},
    ]
    snapshot["collections"][1]["observed_fields"]["fields"].append("owner")

    result = analyze(snapshot, detect_foreign_keys=True)
    methods = {
        r.get("method")
        for r in result.physical_mapping.get("relationships", {}).values()
        if r.get("style") == "FOREIGN_KEY"
    }
    assert "id_shape" in methods


def test_unknown_field_types_do_not_block_detection():
    """The default snapshot has no samples, so every field type is unknown."""
    snapshot = chinook_snapshot(samples=False)
    assert "sample_documents" not in snapshot["collections"][1]
    assert analyze(snapshot, detect_foreign_keys=True).metadata.foreign_key_status["added"] >= 3


# ── taxonomy ─────────────────────────────────────────────────────────────────


def account_snapshot() -> dict:
    core = ["_key", "accountId", "name", "description", "balance"]
    return {
        "collections": [
            _doc("MortgageAccount", [*core, "routingNumber", "principal", "monthlyPayment"]),
            _doc("CheckingAccount", [*core, "routingNumber", "overdraftLimit"]),
            _doc("SavingsAccount", [*core, "routingNumber", "apy"]),
            _doc("InsuranceAccount", [*core, "premium", "policyNumber"]),
            _doc("Customer", ["_key", "customerId", "email"]),
        ]
    }


def test_no_taxonomy_by_default():
    result = analyze(account_snapshot())
    assert result.metadata.taxonomy_status is None
    assert not result.conceptual_schema.get("abstractClasses")


def test_enabling_discovery_produces_abstract_classes():
    result = analyze(account_snapshot(), discover_taxonomy=True)
    abstract = result.conceptual_schema.get("abstractClasses") or []
    assert abstract, "discovery ran but nothing reached the conceptual schema"

    extents = {frozenset(c["members"]) for c in abstract}
    assert frozenset({"MortgageAccount", "CheckingAccount", "SavingsAccount", "InsuranceAccount"}) in extents
    assert frozenset({"MortgageAccount", "CheckingAccount", "SavingsAccount"}) in extents


def test_abstract_classes_are_merged_as_entities():
    result = analyze(account_snapshot(), discover_taxonomy=True)
    added = [e for e in result.conceptual_schema["entities"] if e.get("abstract")]
    assert added
    # No physical mapping by design — the absent mapping *is* the signal.
    assert all(e["name"] not in result.physical_mapping.get("entities", {}) for e in added)


def test_subclass_proposals_are_emitted():
    result = analyze(account_snapshot(), discover_taxonomy=True)
    proposals = result.conceptual_schema.get("subClassOfProposals") or []
    assert proposals
    assert all(p.get("mechanism") and p.get("confidence") is not None for p in proposals)


def test_taxonomy_status_is_reported():
    result = analyze(account_snapshot(), discover_taxonomy=True)
    assert result.metadata.taxonomy_status["status"] == "ok"
    assert result.metadata.taxonomy_status["abstractClasses"] >= 2


def test_discovering_a_taxonomy_does_not_lower_the_health_score():
    """An abstract class has no physical mapping; counting it ungrounded would penalise it."""
    plain = analyze(account_snapshot())
    enriched = analyze(account_snapshot(), discover_taxonomy=True)
    assert enriched.metadata.health_score >= plain.metadata.health_score


# ── failure containment ──────────────────────────────────────────────────────


@pytest.mark.parametrize("flag", ["detect_foreign_keys", "discover_taxonomy"])
def test_enrichment_failure_never_fails_the_analysis(flag, monkeypatch):
    """Enrichment is additive; a broken enricher must not lose the whole analysis."""
    target = (
        "schema_analyzer.fk_inference.apply_to_analysis"
        if flag == "detect_foreign_keys"
        else "schema_analyzer.taxonomy.discover"
    )

    def boom(*args, **kwargs):
        raise RuntimeError("enricher exploded")

    monkeypatch.setattr(target, boom)
    result = analyze(chinook_snapshot(), **{flag: True})

    assert result.conceptual_schema["entities"], "analysis was lost"
    status = result.metadata.foreign_key_status if flag == "detect_foreign_keys" else result.metadata.taxonomy_status
    assert status["status"] == "degraded"
    assert "exploded" in status["reason"]
