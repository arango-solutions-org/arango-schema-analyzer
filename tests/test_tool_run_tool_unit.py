def test_run_tool_rejects_invalid_request():
    from schema_analyzer.tool import run_tool

    resp = run_tool({"contractVersion": "1", "operation": "analyze"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_REQUEST"


def test_run_tool_export_docs_owl_require_input_analysis():
    from schema_analyzer.tool import run_tool

    for op in ("export", "docs", "owl"):
        resp = run_tool({"contractVersion": "1", "operation": op, "input": {}})
        assert resp["ok"] is False


def test_connect_db_respects_verify_tls(monkeypatch):
    from unittest.mock import MagicMock

    recorded: dict[str, object] = {}

    class FakeArangoClient:
        def __init__(self, hosts, verify_override=True, **kwargs):
            recorded["hosts"] = hosts
            recorded["verify_override"] = verify_override

        def db(self, name, username="", password=""):
            return MagicMock()

    monkeypatch.setattr("schema_analyzer.tool.ArangoClient", FakeArangoClient)
    from schema_analyzer.tool import _connect_db

    _connect_db(
        {
            "url": "https://db.example:8529",
            "database": "mydb",
            "password": "secret",
            "verifyTls": False,
        }
    )
    assert recorded["verify_override"] is False


def test_run_tool_analyze_missing_password_env_var_returns_error(monkeypatch):
    from schema_analyzer.tool import run_tool

    monkeypatch.delenv("ARANGO_PASS", raising=False)
    req = {
        "contractVersion": "1",
        "operation": "analyze",
        "connection": {
            "url": "http://localhost:8529",
            "database": "db",
            "username": "root",
            "passwordEnvVar": "ARANGO_PASS",
        },
    }
    resp = run_tool(req)
    assert resp["ok"] is False
    assert resp["error"]["code"] in ("INVALID_ARGUMENT", "ERROR")


# ── prior-run exposure through the v1 contract (PRD §3.13.5) ──────────────────
# Reuses the pre-existing shared field `input.previousAnalysis` (also required by `diff`),
# NOT a minted `analysisOptions.priorRun` — converged with relational-schema-analyzer.

_BASE_ANALYZE = {
    "contractVersion": "1",
    "operation": "analyze",
    "connection": {"url": "http://localhost:8529", "database": "db", "username": "root", "password": "x"},
}

# input.previousAnalysis is a strict $ref to AnalysisOutput, so it must be a full analysis.
_VALID_PRIOR = {
    "conceptualSchema": {"entities": [], "relationships": [], "properties": []},
    "physicalMapping": {"entities": {}, "relationships": {}},
    "metadata": {
        "confidence": 0.1,
        "timestamp": "2020-01-01T00:00:00Z",
        "analyzedCollectionCounts": {"documentCollections": 0, "edgeCollections": 0},
        "detectedPatterns": [],
    },
}


def test_previous_analysis_is_accepted_on_analyze_by_the_request_schema():
    # The prior-run field must be reachable through the contract; it's the pre-existing
    # input.previousAnalysis, so no new key and additionalProperties:false doesn't reject it.
    from schema_analyzer.tool_contract_v1 import validate_request_v1

    req = {**_BASE_ANALYZE, "input": {"previousAnalysis": _VALID_PRIOR}}
    assert validate_request_v1(req) == []


def test_run_tool_routes_previous_analysis_through_analyze_incremental(monkeypatch):
    import schema_analyzer.tool as tool_mod
    from schema_analyzer.analyzer import AgenticSchemaAnalyzer
    from schema_analyzer.types import AnalysisMetadata, AnalysisResult

    monkeypatch.setattr(tool_mod, "_connect_db", lambda conn: object())
    monkeypatch.setattr(
        tool_mod, "snapshot_physical_schema", lambda *a, **k: {"version": 1, "collections": [], "graphs": []}
    )

    calls: list[str] = []

    def _fake_result():
        return AnalysisResult(
            conceptual_schema={"entities": [], "relationships": [], "properties": []},
            physical_mapping={"entities": {}, "relationships": {}},
            metadata=AnalysisMetadata(
                confidence=0.1,
                timestamp="2020-01-01T00:00:00Z",
                analyzed_collection_counts={"documentCollections": 0, "edgeCollections": 0},
                detected_patterns=[],
            ),
        )

    def _fake_incremental(self, db, **kwargs):
        calls.append("incremental")
        assert isinstance(kwargs.get("prior"), dict)  # previousAnalysis threaded through
        return _fake_result()

    def _fake_full(self, db, **kwargs):
        calls.append("full")
        return _fake_result()

    monkeypatch.setattr(AgenticSchemaAnalyzer, "analyze_incremental", _fake_incremental)
    monkeypatch.setattr(AgenticSchemaAnalyzer, "analyze_physical_schema", _fake_full)

    tool_mod.run_tool({**_BASE_ANALYZE, "input": {"previousAnalysis": _VALID_PRIOR}})
    tool_mod.run_tool(dict(_BASE_ANALYZE))
    assert calls == ["incremental", "full"]


def test_run_tool_rejects_malformed_previous_analysis():
    from schema_analyzer.tool import run_tool

    resp = run_tool({**_BASE_ANALYZE, "input": {"previousAnalysis": "not-an-object"}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_REQUEST"


def test_run_tool_keeps_a_zero_review_threshold_and_cache_ttl(monkeypatch):
    """0 is a valid ``reviewThreshold`` and ``cacheTtlSeconds``; it used to become the default."""
    import schema_analyzer.tool as tool_mod
    from schema_analyzer.analyzer import AgenticSchemaAnalyzer

    monkeypatch.setattr(tool_mod, "_connect_db", lambda conn: object())
    monkeypatch.setattr(
        tool_mod, "snapshot_physical_schema", lambda *a, **k: {"version": 1, "collections": [], "graphs": []}
    )
    seen: list[tuple[float, int]] = []
    real_init = AgenticSchemaAnalyzer.__init__

    def _spy_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        seen.append((self.review_threshold, self.cache_ttl_seconds))

    monkeypatch.setattr(AgenticSchemaAnalyzer, "__init__", _spy_init)

    zero = {**_BASE_ANALYZE, "analysisOptions": {"reviewThreshold": 0, "cacheTtlSeconds": 0}}
    assert tool_mod.run_tool(zero)["ok"] is True
    assert tool_mod.run_tool(dict(_BASE_ANALYZE))["ok"] is True
    assert seen == [(0.0, 0), (0.6, 86400)]


_MINIMAL_ANALYSIS = {
    "conceptualSchema": {"entities": [], "relationships": [], "properties": []},
    "physicalMapping": {"entities": {}, "relationships": {}},
    "metadata": {
        "confidence": 1.0,
        "timestamp": "t",
        "analyzedCollectionCounts": {"documentCollections": 0, "edgeCollections": 0},
        "detectedPatterns": [],
    },
}


def _docs_request(req_id=None):
    req = {"contractVersion": "1", "operation": "docs", "input": {"analysis": _MINIMAL_ANALYSIS}}
    if req_id:
        req["requestId"] = req_id
    return req


def test_error_envelopes_keep_their_exact_shape(monkeypatch):
    """Byte-identical to the envelopes run_tool hand-built before ``_error_response``:
    same keys, same order, ``requestId`` last and only when the request carried one."""
    import json

    import schema_analyzer.tool as tool
    from schema_analyzer.errors import SchemaAnalyzerError

    def raise_(exc):
        def _raise(*_a, **_k):
            raise exc

        return _raise

    monkeypatch.setattr(tool, "generate_schema_docs", raise_(SchemaAnalyzerError("bad", code="INVALID_ARGUMENT")))
    assert json.dumps(tool.run_tool(_docs_request("r2"))) == (
        '{"contractVersion": "1", "operation": "docs", "ok": false, '
        '"error": {"code": "INVALID_ARGUMENT", "message": "bad"}, "requestId": "r2"}'
    )
    monkeypatch.setattr(tool, "generate_schema_docs", raise_(SchemaAnalyzerError("bad", code=None)))
    assert json.dumps(tool.run_tool(_docs_request())) == (
        '{"contractVersion": "1", "operation": "docs", "ok": false, "error": {"code": "ERROR", "message": "bad"}}'
    )
    monkeypatch.setattr(tool, "generate_schema_docs", raise_(RuntimeError("secret detail")))
    assert json.dumps(tool.run_tool(_docs_request("r3"))) == (
        '{"contractVersion": "1", "operation": "docs", "ok": false, "error": {"code": "INTERNAL_ERROR", '
        '"message": "An internal error occurred. Check server logs for details."}, "requestId": "r3"}'
    )
    # A request that fails validation echoes its raw requestId, None included, before ``ok``.
    assert json.dumps(tool.run_tool({"contractVersion": "1", "operation": "docs"})) == (
        '{"contractVersion": "1", "operation": "docs", "requestId": null, "ok": false, '
        '"error": {"code": "INVALID_REQUEST", "message": "\'input\' is a required property"}}'
    )


def test_unknown_entity_strategy_is_rejected_by_the_request_schema():
    from schema_analyzer.tool import run_tool

    resp = run_tool(
        {
            "contractVersion": "1",
            "operation": "analyze",
            "connection": {"url": "http://localhost:8529", "database": "db"},
            "analysisOptions": {"entityStrategy": "bogus"},
        }
    )
    assert resp == {
        "contractVersion": "1",
        "operation": "analyze",
        "requestId": None,
        "ok": False,
        "error": {"code": "INVALID_REQUEST", "message": "'bogus' is not one of ['auto', 'collection', 'labels']"},
    }
