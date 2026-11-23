import json

from schema_analyzer.analyzer import AgenticSchemaAnalyzer


class FakeProvider:
    def __init__(self, text: str):
        self._text = text

    def generate(self, *, model: str, system: str, prompt: str, timeout_ms: int):
        class R:
            def __init__(self, t):
                self.text = t

        return R(self._text)


class FakeDB:
    def collections(self):
        class FakeCol:
            def __init__(self, col_type):
                self._type = col_type

            def properties(self):
                return {"type": self._type}

            def count(self):
                return 0

            def indexes(self):
                return []

        return {"users": FakeCol(2), "follows": FakeCol(3)}

    def graphs(self):
        return []


def test_analyze_with_mock_provider_parses_json(monkeypatch):
    payload = {
        "conceptualSchema": {"entities": [], "relationships": [], "properties": []},
        "physicalMapping": {"entities": {}, "relationships": {}},
        "metadata": {
            "confidence": 0.8,
            "timestamp": "2026-01-01T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 1},
            "detectedPatterns": [],
        },
    }
    text = "here you go\n" + json.dumps(payload) + "\nthanks"

    import schema_analyzer.analyzer as analyzer_mod

    def _fake_create_provider(name, *, api_key):
        return FakeProvider(text)

    monkeypatch.setattr(analyzer_mod, "create_provider", _fake_create_provider)

    analyzer = AgenticSchemaAnalyzer(llm_provider="openai", api_key="k", model="m")
    res = analyzer.analyze_physical_schema(FakeDB(), sample_limit_per_collection=0)
    assert res.metadata.confidence == 0.8
    assert res.metadata.review_required is False
    # Quality metrics + composite health score are stamped on every analysis.
    assert res.metadata.health_score is not None
    assert 0 <= res.metadata.health_score <= 100
    assert res.metadata.quality_metrics is not None
    assert "structural" in res.metadata.quality_metrics
    assert "grounding" in res.metadata.quality_metrics


def test_analyze_with_gold_reference_emits_gold_block(monkeypatch):
    payload = {
        "conceptualSchema": {
            "entities": [{"name": "User", "labels": ["User"], "properties": []}],
            "relationships": [],
            "properties": [],
        },
        "physicalMapping": {
            "entities": {"User": {"style": "COLLECTION", "collectionName": "users"}},
            "relationships": {},
        },
        "metadata": {
            "confidence": 0.8,
            "timestamp": "2026-01-01T00:00:00Z",
            "analyzedCollectionCounts": {"documentCollections": 1, "edgeCollections": 0},
            "detectedPatterns": [],
        },
    }
    text = json.dumps(payload)

    import schema_analyzer.analyzer as analyzer_mod

    monkeypatch.setattr(analyzer_mod, "create_provider", lambda name, *, api_key: FakeProvider(text))

    analyzer = AgenticSchemaAnalyzer(
        llm_provider="openai",
        api_key="k",
        model="m",
        gold_reference={"entities": [{"name": "user"}], "relationships": []},
    )
    res = analyzer.analyze_physical_schema(FakeDB(), use_cache=False)
    gold = res.metadata.quality_metrics["gold"]
    assert gold["entities"]["f1"] == 1.0
    assert "gold" in res.metadata.quality_metrics["healthScoreComponents"]


# ── tenant-scope wire-through (issue #13) ────────────────────────────────


class _MultiTenantFakeDB:
    """Fake DB whose collections look like a tiny multi-tenant graph:
    ``Tenant`` (root), ``Device`` (denorm-scoped via ``TENANT_ID``),
    ``Cve`` (global metadata)."""

    def collections(self):
        class FakeCol:
            def __init__(self, col_type):
                self._type = col_type

            def properties(self):
                return {"type": self._type}

            def count(self):
                return 0

            def indexes(self):
                return []

        return {"Tenant": FakeCol(2), "Device": FakeCol(2), "Cve": FakeCol(2)}

    def graphs(self):
        return []


def test_analyze_emits_tenant_scope_annotations_and_report(monkeypatch):
    """End-to-end check that ``_apply_tenant_scope`` is wired into
    both the analyzer pipeline and the export contract.

    We feed a multi-tenant LLM payload (Tenant root, denorm-scoped
    Device with TENANT_ID, global Cve) and assert that the annotator
    stamped the expected ``tenantScope`` blocks under
    ``physicalMapping.entities[*]`` and folded a
    ``tenantScopeReport`` summary into ``metadata``. This protects
    against regressions where ``_apply_tenant_scope`` accidentally
    gets removed from one of the two pipeline call sites or ordered
    after ``_apply_statistics`` (which would clobber the metadata
    block on some payload shapes).
    """
    payload = {
        "conceptualSchema": {
            "entities": [
                {"name": "Tenant", "labels": ["Tenant"], "properties": []},
                {
                    "name": "Device",
                    "labels": ["Device"],
                    "properties": [{"name": "TENANT_ID"}, {"name": "name"}],
                },
                {
                    "name": "Cve",
                    "labels": ["Cve"],
                    "properties": [{"name": "CVE_ID"}],
                },
            ],
            "relationships": [],
            "properties": [],
        },
        "physicalMapping": {
            "entities": {
                "Tenant": {"style": "COLLECTION", "collectionName": "Tenant"},
                "Device": {"style": "COLLECTION", "collectionName": "Device"},
                "Cve": {"style": "COLLECTION", "collectionName": "Cve"},
            },
            "relationships": {},
        },
        "metadata": {
            "confidence": 0.9,
            "timestamp": "2026-01-01T00:00:00Z",
            "analyzedCollectionCounts": {
                "documentCollections": 3,
                "edgeCollections": 0,
            },
            "detectedPatterns": [],
        },
    }
    text = json.dumps(payload)

    import schema_analyzer.analyzer as analyzer_mod

    def _fake_create_provider(name, *, api_key):
        return FakeProvider(text)

    monkeypatch.setattr(analyzer_mod, "create_provider", _fake_create_provider)

    analyzer = AgenticSchemaAnalyzer(llm_provider="openai", api_key="k", model="m")
    res = analyzer.analyze_physical_schema(_MultiTenantFakeDB(), sample_limit_per_collection=0, use_cache=False)

    pm = res.physical_mapping["entities"]
    assert pm["Tenant"]["tenantScope"] == {"role": "tenant_root"}
    assert pm["Device"]["tenantScope"] == {
        "role": "tenant_scoped",
        "tenantEntity": "Tenant",
        "tenantField": "TENANT_ID",
    }
    assert pm["Cve"]["tenantScope"] == {"role": "global"}

    report = res.metadata.tenant_scope_report
    assert report is not None
    assert report["tenantEntity"] == "Tenant"
    assert report["denormScopedCount"] == 1
    assert report["globalCount"] == 1


def test_no_llm_path_runs_the_same_enrichment_as_the_llm_fallback(monkeypatch):
    """The default path (no LLM configured) runs every enrichment pass.

    It used to keep its own copy of the pass list, which lacked tenant scope, so a
    plain ``analyze_physical_schema(db)`` returned no ``tenantScope`` blocks and no
    ``tenantScopeReport`` while the LLM-failed fallback, on the same baseline, did.
    """
    import schema_analyzer.analyzer as analyzer_mod

    class _FailingProvider:
        def generate(self, *, model: str, system: str, prompt: str, timeout_ms: int):
            raise analyzer_mod.SchemaAnalyzerError("provider down", code="LLM_ERROR")

    monkeypatch.setattr(analyzer_mod, "create_provider", lambda name, *, api_key: _FailingProvider())
    fallback = AgenticSchemaAnalyzer(llm_provider="openai", api_key="k", model="m").analyze_physical_schema(
        _MultiTenantFakeDB(), sample_limit_per_collection=0, use_cache=False
    )
    no_llm = AgenticSchemaAnalyzer().analyze_physical_schema(
        _MultiTenantFakeDB(), sample_limit_per_collection=0, use_cache=False
    )

    assert fallback.metadata.used_baseline and no_llm.metadata.used_baseline
    assert no_llm.physical_mapping["entities"]["Tenant"]["tenantScope"] == {"role": "tenant_root"}
    assert no_llm.metadata.tenant_scope_report == fallback.metadata.tenant_scope_report
    assert no_llm.metadata.tenant_scope_report is not None
    for name, spec in fallback.physical_mapping["entities"].items():
        assert no_llm.physical_mapping["entities"][name].get("tenantScope") == spec.get("tenantScope")
