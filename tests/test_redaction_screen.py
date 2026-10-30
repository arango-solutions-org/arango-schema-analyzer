"""Tests for per-field value screening before LLM egress (PRD §4.3, ``screen_field_values``)."""

from __future__ import annotations

import json

from schema_analyzer.analyzer import AgenticSchemaAnalyzer
from schema_analyzer.redaction import RedactionOptions, redact_snapshot_for_egress

SNAPSHOT = {
    "collections": [
        {
            "name": "customers",
            "type": "document",
            "observed_fields": {"fields": ["kind", "owner", "status"], "by_type": {"Gold": ["owner"]}},
            "sample_documents": [{"_key": "1", "kind": "Gold", "owner": "Ann Lee", "status": "active"}],
            "sample_field_value_counts": {
                "kind": [{"value": "Gold", "count": 7}, {"value": "Silver", "count": 3}],
                "owner": [{"value": "Ann Lee", "count": 6}, {"value": "Bob Ray", "count": 4}],
            },
            "sample_field_value_overflow": {"owner": [{"value": "Cy Fox", "count": 1}]},
        },
        {
            "name": "manages",
            "type": "edge",
            "edge_endpoints": {
                "entity_types_by_relation": {"Ann Lee": {"from_entity_types": ["Ann Lee"], "to_entity_types": ["Gold"]}}
            },
        },
    ],
    "graphs": [],
}


def _owner_only(collection: str, field: str, values: list[str]) -> bool:
    return field == "owner"


def test_only_the_screened_field_is_masked():
    out = redact_snapshot_for_egress(SNAPSHOT, RedactionOptions(screen_field_values=_owner_only))
    coll = out["collections"][0]
    counts = coll["sample_field_value_counts"]
    assert [c["value"] for c in counts["owner"]] == ["<redacted>:0", "<redacted>:1"]
    assert [c["count"] for c in counts["owner"]] == [6, 4]
    assert [c["value"] for c in counts["kind"]] == ["Gold", "Silver"]
    assert coll["sample_field_value_overflow"]["owner"][0]["value"] == "<redacted>:2"
    assert coll["sample_documents"][0] == {"_key": "1", "kind": "Gold", "owner": "<redacted>", "status": "active"}
    # The caller's snapshot is untouched.
    assert SNAPSHOT["collections"][0]["sample_field_value_counts"]["owner"][0]["value"] == "Ann Lee"


def test_a_screened_value_is_masked_wherever_it_appears():
    out = redact_snapshot_for_egress(SNAPSHOT, RedactionOptions(screen_field_values=_owner_only))
    by_rel = out["collections"][1]["edge_endpoints"]["entity_types_by_relation"]
    assert by_rel == {"<redacted>:0": {"from_entity_types": ["<redacted>:0"], "to_entity_types": ["Gold"]}}
    assert out["collections"][0]["observed_fields"]["by_type"] == {"Gold": ["owner"]}
    assert "Ann Lee" not in json.dumps(out)


def test_the_screen_sees_collection_field_and_every_sampled_value():
    seen: list[tuple[str, str, list[str]]] = []

    def record(collection: str, field: str, values: list[str]) -> bool:
        seen.append((collection, field, sorted(values)))
        return False

    out = redact_snapshot_for_egress(SNAPSHOT, RedactionOptions(screen_field_values=record))
    assert sorted(seen) == [
        ("customers", "kind", ["Gold", "Gold", "Silver"]),
        ("customers", "owner", ["Ann Lee", "Ann Lee", "Bob Ray", "Cy Fox"]),
        ("customers", "status", ["active"]),
    ]
    assert json.dumps(out, sort_keys=True) == json.dumps(SNAPSHOT, sort_keys=True)


def test_screen_makes_options_active_but_never_comes_from_a_request():
    assert RedactionOptions(screen_field_values=_owner_only).active
    assert RedactionOptions.from_dict({"screenFieldValues": True}).screen_field_values is None


def test_mask_field_values_still_masks_everything():
    opts = RedactionOptions(mask_field_values=True, screen_field_values=lambda c, f, v: False)
    out = redact_snapshot_for_egress(SNAPSHOT, opts)
    kinds = out["collections"][0]["sample_field_value_counts"]["kind"]
    assert all(k["value"].startswith("<redacted>") for k in kinds)


def test_composes_with_field_name_masking():
    opts = RedactionOptions(screen_field_values=_owner_only, mask_field_names=True)
    out = redact_snapshot_for_egress(SNAPSHOT, opts)
    text = json.dumps(out)
    assert "Ann Lee" not in text
    assert '"owner"' not in text
    assert "Silver" in text


class _CapturingProvider:
    def __init__(self, text: str) -> None:
        self._text = text
        self.last_prompt = ""

    def generate(self, *, model: str, system: str, prompt: str, timeout_ms: int):
        self.last_prompt = prompt

        class R:
            def __init__(self, t: str) -> None:
                self.text = t

        return R(self._text)


class _FakeDB:
    def collections(self):
        return {}

    def graphs(self):
        return []


_VALID = json.dumps(
    {
        "conceptualSchema": {"entities": [], "relationships": [], "properties": []},
        "physicalMapping": {"entities": {}, "relationships": {}},
        "metadata": {
            "confidence": 0.8,
            "timestamp": "2026-01-01T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 1},
            "detectedPatterns": [],
        },
    }
)


def test_analyzer_prompt_withholds_only_screened_values(monkeypatch):
    provider = _CapturingProvider(_VALID)
    import schema_analyzer.analyzer as analyzer_mod

    monkeypatch.setattr(analyzer_mod, "create_provider", lambda name, *, api_key: provider)
    analyzer = AgenticSchemaAnalyzer(
        llm_provider="openai",
        api_key="k",
        model="m",
        redaction=RedactionOptions(screen_field_values=_owner_only),
    )
    analyzer.analyze_physical_schema(_FakeDB(), use_cache=False, _snapshot=json.loads(json.dumps(SNAPSHOT)))

    assert "Ann Lee" not in provider.last_prompt
    assert "Bob Ray" not in provider.last_prompt
    assert "Silver" in provider.last_prompt  # type values still reach the model
    assert "owner" in provider.last_prompt
