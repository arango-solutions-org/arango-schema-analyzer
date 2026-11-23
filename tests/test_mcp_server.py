import asyncio

import pytest

from schema_analyzer.mcp_server import (
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    TOKEN_ENV_VAR,
    _bearer_token_valid,
    _parse_args,
    _typed_request,
)

# --- bearer token validation (pure) -----------------------------------------


def test_bearer_no_expected_token_is_open():
    # No token configured => server is open (warned about, but allowed).
    assert _bearer_token_valid(None, "") is True
    assert _bearer_token_valid("Bearer anything", "") is True


def test_bearer_missing_header_rejected():
    assert _bearer_token_valid(None, "secret") is False
    assert _bearer_token_valid("", "secret") is False


def test_bearer_wrong_scheme_rejected():
    assert _bearer_token_valid("Token secret", "secret") is False
    assert _bearer_token_valid("secret", "secret") is False


def test_bearer_wrong_token_rejected():
    assert _bearer_token_valid("Bearer nope", "secret") is False


def test_bearer_correct_token_accepted():
    assert _bearer_token_valid("Bearer secret", "secret") is True
    assert _bearer_token_valid("bearer secret", "secret") is True  # scheme case-insensitive


# --- typed request builder ---------------------------------------------------


def test_typed_request_drops_unset_fields():
    req = _typed_request("snapshot", connection={"url": "x"}, analysisOptions=None)
    assert req["operation"] == "snapshot"
    assert "contractVersion" in req
    assert req["connection"] == {"url": "x"}
    assert "analysisOptions" not in req


def test_typed_request_keeps_set_fields():
    req = _typed_request("owl", input={"analysis": {}}, outputOptions={"owlFormat": "jsonld"})
    assert req["input"] == {"analysis": {}}
    assert req["outputOptions"] == {"owlFormat": "jsonld"}


# --- argument parsing + env fallback -----------------------------------------


def test_parse_args_defaults(monkeypatch):
    for var in ("SCHEMA_ANALYZER_MCP_TRANSPORT", "SCHEMA_ANALYZER_MCP_HOST", "SCHEMA_ANALYZER_MCP_PORT"):
        monkeypatch.delenv(var, raising=False)
    args = _parse_args([])
    assert args.transport == "stdio"
    assert args.host == DEFAULT_MCP_HOST
    assert args.port == DEFAULT_MCP_PORT


def test_parse_args_env_fallback(monkeypatch):
    monkeypatch.setenv("SCHEMA_ANALYZER_MCP_TRANSPORT", "sse")
    monkeypatch.setenv("SCHEMA_ANALYZER_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("SCHEMA_ANALYZER_MCP_PORT", "9100")
    args = _parse_args([])
    assert (args.transport, args.host, args.port) == ("sse", "0.0.0.0", 9100)


def test_parse_args_flags_override_env(monkeypatch):
    monkeypatch.setenv("SCHEMA_ANALYZER_MCP_TRANSPORT", "sse")
    args = _parse_args(["--transport", "streamable-http", "--port", "7000"])
    assert args.transport == "streamable-http"
    assert args.port == 7000


def test_parse_args_rejects_unknown_transport():
    with pytest.raises(SystemExit):
        _parse_args(["--transport", "carrier-pigeon"])


# --- app registers generic + typed tools ------------------------------------


def test_build_app_registers_all_tools():
    pytest.importorskip("mcp")
    from schema_analyzer.mcp_server import build_app

    app = build_app()
    assert app.name == "arangodb-schema-analyzer"
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert {
        "arangodb_schema_analyzer_run",
        "arangodb_schema_analyzer_run_json",
        "schema_analyzer_snapshot",
        "schema_analyzer_analyze",
        "schema_analyzer_export",
        "schema_analyzer_docs",
        "schema_analyzer_owl",
    } <= names


def test_build_app_honors_host_port():
    pytest.importorskip("mcp")
    from schema_analyzer.mcp_server import build_app

    app = build_app(host="0.0.0.0", port=9123)
    assert app.settings.host == "0.0.0.0"
    assert app.settings.port == 9123


# --- auth middleware end-to-end (Starlette) ----------------------------------


def _auth_test_client():
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from schema_analyzer.mcp_server import _install_auth

    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/ping", ok)])
    _install_auth(app, "secret")
    return TestClient(app)


def test_auth_middleware_rejects_without_token():
    client = _auth_test_client()
    resp = client.get("/ping")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


def test_auth_middleware_rejects_wrong_token():
    client = _auth_test_client()
    resp = client.get("/ping", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_auth_middleware_allows_correct_token():
    client = _auth_test_client()
    resp = client.get("/ping", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# --- serve() dispatch --------------------------------------------------------


def test_serve_rejects_unknown_transport():
    pytest.importorskip("mcp")
    from schema_analyzer.mcp_server import serve

    with pytest.raises(ValueError, match="Unsupported transport"):
        serve("carrier-pigeon")


def test_serve_stdio_invokes_run(monkeypatch):
    pytest.importorskip("mcp")
    import schema_analyzer.mcp_server as mod

    called = {}

    class FakeApp:
        settings = type("S", (), {"host": "127.0.0.1", "port": 8000})()

        def run(self, transport):
            called["transport"] = transport

    monkeypatch.setattr(mod, "build_app", lambda **kw: FakeApp())
    mod.serve("stdio")
    assert called["transport"] == "stdio"


def test_serve_remote_warns_without_token(monkeypatch, caplog):
    pytest.importorskip("mcp")
    import schema_analyzer.mcp_server as mod

    class FakeApp:
        settings = type("S", (), {"host": "0.0.0.0", "port": 8000})()

        def sse_app(self):
            return object()

    monkeypatch.setattr(mod, "build_app", lambda **kw: FakeApp())
    served = {}
    monkeypatch.setattr(mod, "_install_auth", lambda app, token: served.setdefault("auth", True))

    import sys

    fake_uvicorn = type(sys)("uvicorn")
    fake_uvicorn.run = lambda app, host, port: served.setdefault("ran", (host, port))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    with caplog.at_level("WARNING"):
        mod.serve("sse", host="0.0.0.0", port=8000, token=None)
    assert served["ran"] == ("0.0.0.0", 8000)
    assert "auth" not in served  # no token => no auth middleware installed
    assert any("no auth token" in r.message for r in caplog.records)


def test_run_json_error_envelopes_keep_their_exact_shape():
    pytest.importorskip("mcp")
    import json

    from schema_analyzer.mcp_server import build_app

    run_json = build_app()._tool_manager.get_tool("arangodb_schema_analyzer_run_json").fn
    assert json.dumps(run_json("[1]")) == (
        '{"contractVersion": "1", "operation": null, "ok": false, '
        '"error": {"code": "INVALID_REQUEST", "message": "request_json must be a JSON object"}}'
    )
    resp = run_json("{not json")
    assert list(resp) == ["contractVersion", "operation", "ok", "error"]
    assert resp["operation"] is None
    assert resp["error"]["code"] == "INVALID_REQUEST"
    assert resp["error"]["message"].startswith("Invalid JSON: ")
