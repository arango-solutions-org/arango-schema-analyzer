"""Library functions give the same answer for an ``AnalysisResult`` as for its JSON form.

``analyze_physical_schema`` returns an ``AnalysisResult``; ``run_tool`` hands out the
camelCase dict. ``normalize_analysis_dict`` used to dump a model with snake_case
metadata keys, so readers that looked up ``healthScore`` or ``nestedFields`` found
nothing when given the model.
"""

from __future__ import annotations

from typing import Any

from schema_analyzer import diff_analyses, export_mapping
from schema_analyzer.csi import to_csi
from schema_analyzer.types import AnalysisResult
from schema_analyzer.utils import normalize_analysis_dict

CONFLICTS = [{"collection": "people", "name": "address.city"}]


def _analysis(health: int, nested: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "conceptualSchema": {
            "entities": [{"name": "Person", "labels": ["Person"], "properties": [{"name": "name"}]}],
            "relationships": [],
            "properties": [],
        },
        "physicalMapping": {
            "entities": {"Person": {"style": "COLLECTION", "collectionName": "people", "properties": {}}},
            "relationships": {},
        },
        "metadata": {
            "confidence": 0.5,
            "timestamp": "2026-01-01T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 0},
            "detectedPatterns": [],
            "healthScore": health,
            "nestedFields": nested,
        },
    }


def _model(data: dict[str, Any]) -> AnalysisResult:
    return AnalysisResult(
        conceptual_schema=data["conceptualSchema"],
        physical_mapping=data["physicalMapping"],
        metadata=data["metadata"],
    )


def test_diff_reports_the_health_score_change_for_models():
    before, after = _analysis(50), _analysis(80)
    expected = {"previous": 50, "current": 80, "delta": 30}
    assert diff_analyses(before, after)["healthScore"] == expected
    assert diff_analyses(_model(before), _model(after))["healthScore"] == expected


def test_to_csi_keeps_unresolved_field_locations_for_a_model():
    data = _analysis(50, {"maxDepth": 1, "conflicts": CONFLICTS, "arraysSkipped": []})
    assert to_csi(data)["provenance"]["unresolvedFieldLocations"] == CONFLICTS
    assert to_csi(_model(data))["provenance"]["unresolvedFieldLocations"] == CONFLICTS


def test_a_model_normalizes_to_the_contract_metadata_keys():
    data = _analysis(50)
    meta = normalize_analysis_dict(_model(data))["metadata"]
    assert meta["healthScore"] == 50
    assert "health_score" not in meta
    # The cypher export bundle passes metadata through: same keys either way.
    from_model = export_mapping(_model(data), target="cypher")["metadata"]
    assert set(from_model) == set(export_mapping(_model(data).model_dump(by_alias=True), target="cypher")["metadata"])


def test_snake_case_metadata_in_a_hand_built_dict_is_still_read():
    before, after = _analysis(50), _analysis(80)
    for d in (before, after):
        d["metadata"]["health_score"] = d["metadata"].pop("healthScore")
    assert diff_analyses(before, after)["healthScore"]["delta"] == 30
