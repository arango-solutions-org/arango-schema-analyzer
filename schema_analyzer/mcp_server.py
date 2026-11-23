"""MCP (Model Context Protocol) server wrapping the v1 JSON tool contract.

Requires optional dependency: ``pip install 'arangodb-schema-analyzer[mcp]'``.

Transports (PRD §3.11):

* **stdio** (default) — local IDE / Cursor use::

      arangodb-schema-analyzer-mcp

* **sse** / **streamable-http** — remote agents::

      arangodb-schema-analyzer-mcp --transport sse --host 0.0.0.0 --port 8000

**Security.** Remote transports expose an endpoint that can drive the analyzer
against arbitrary databases and (if configured) ship snapshots to an LLM, so
they are gated:

* **Bearer token** — set ``SCHEMA_ANALYZER_MCP_TOKEN`` and every HTTP request
  must carry ``Authorization: Bearer <token>`` (constant-time compared). When
  unset, the server starts but logs a loud warning; do not expose an
  unauthenticated remote server to untrusted networks.
* **Connection allowlist / cache-root** — the existing ``run_tool`` trust
  boundary (``SCHEMA_ANALYZER_ALLOWED_HOSTS`` / ``SCHEMA_ANALYZER_CACHE_ROOT``)
  is enforced inside every operation, so it applies uniformly to stdio and
  remote callers without extra wiring.

Environment fallbacks for the CLI flags: ``SCHEMA_ANALYZER_MCP_TRANSPORT``,
``SCHEMA_ANALYZER_MCP_HOST``, ``SCHEMA_ANALYZER_MCP_PORT``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8000
REMOTE_TRANSPORTS = ("sse", "streamable-http")

TRANSPORT_ENV_VAR = "SCHEMA_ANALYZER_MCP_TRANSPORT"
HOST_ENV_VAR = "SCHEMA_ANALYZER_MCP_HOST"
PORT_ENV_VAR = "SCHEMA_ANALYZER_MCP_PORT"
TOKEN_ENV_VAR = "SCHEMA_ANALYZER_MCP_TOKEN"


def _require_fastmcp():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        print(
            "The MCP server requires the 'mcp' package. Install with:\n  pip install 'arangodb-schema-analyzer[mcp]'",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    return FastMCP


def _bearer_token_valid(auth_header: str | None, expected: str) -> bool:
    """Validate an ``Authorization`` header against the expected bearer token.

    Pure and constant-time. An empty ``expected`` means no token is configured,
    so the request is allowed (the open-server case, warned about elsewhere).
    """
    if not expected:
        return True
    if not auth_header:
        return False
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return secrets.compare_digest(parts[1].strip(), expected)


def _install_auth(app: Any, expected_token: str) -> None:
    """Attach a bearer-token gate to a Starlette app (used for remote transports)."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    async def dispatch(request: Any, call_next: Any) -> Any:
        if not _bearer_token_valid(request.headers.get("authorization"), expected_token):
            return JSONResponse(
                {"ok": False, "error": {"code": "UNAUTHENTICATED", "message": "missing or invalid bearer token"}},
                status_code=401,
            )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=dispatch)


def _typed_request(operation: str, **fields: Any) -> dict[str, Any]:
    """Build a v1 request dict for one operation, dropping unset (None) fields."""
    from .tool_contract_v1 import CONTRACT_VERSION

    req: dict[str, Any] = {"contractVersion": CONTRACT_VERSION, "operation": operation}
    for key, value in fields.items():
        if value is not None:
            req[key] = value
    return req


def build_app(*, host: str | None = None, port: int | None = None):
    """Construct the FastMCP app with generic + per-operation tools."""
    FastMCP = _require_fastmcp()

    from .tool import _error_response, run_tool

    mcp = FastMCP(
        "arangodb-schema-analyzer",
        host=host or DEFAULT_MCP_HOST,
        port=port or DEFAULT_MCP_PORT,
        instructions=(
            "ArangoDB schema analyzer. Use the per-operation tools "
            "(schema_analyzer_snapshot/analyze/export/docs/owl) or the generic "
            "arangodb_schema_analyzer_run with a v1 tool-contract request dict. "
            "See docs/tool-contract/v1/request.schema.json."
        ),
    )

    # --- generic passthrough tools (back-compat) ---
    @mcp.tool()
    def arangodb_schema_analyzer_run(request: dict[str, Any]) -> dict[str, Any]:
        """Execute one schema-analyzer operation. The ``request`` object must match
        ``docs/tool-contract/v1/request.schema.json`` (e.g. operation analyze | snapshot | export | docs | owl).
        """
        return run_tool(request)

    @mcp.tool()
    def arangodb_schema_analyzer_run_json(request_json: str) -> dict[str, Any]:
        """Same as arangodb_schema_analyzer_run but accepts a JSON string (convenient for some MCP clients)."""
        try:
            req = json.loads(request_json)
        except json.JSONDecodeError as e:
            return _error_response(op=None, code="INVALID_REQUEST", message=f"Invalid JSON: {e}")
        if not isinstance(req, dict):
            return _error_response(op=None, code="INVALID_REQUEST", message="request_json must be a JSON object")
        return run_tool(req)

    # --- typed per-operation tools (PRD §3.11) ---
    @mcp.tool()
    def schema_analyzer_snapshot(
        connection: dict[str, Any], analysisOptions: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Deterministic physical-schema snapshot. ``connection`` per the v1 contract."""
        return run_tool(_typed_request("snapshot", connection=connection, analysisOptions=analysisOptions))

    @mcp.tool()
    def schema_analyzer_analyze(
        connection: dict[str, Any],
        llm: dict[str, Any] | None = None,
        analysisOptions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Analyze a database into a conceptual schema + physical mapping (optional ``llm``)."""
        return run_tool(_typed_request("analyze", connection=connection, llm=llm, analysisOptions=analysisOptions))

    @mcp.tool()
    def schema_analyzer_export(input: dict[str, Any], outputOptions: dict[str, Any] | None = None) -> dict[str, Any]:
        """Export a prior analysis for a transpiler. ``input.analysis`` required; ``outputOptions.exportTarget``."""
        return run_tool(_typed_request("export", input=input, outputOptions=outputOptions))

    @mcp.tool()
    def schema_analyzer_docs(input: dict[str, Any]) -> dict[str, Any]:
        """Markdown documentation for a prior analysis (``input.analysis`` required)."""
        return run_tool(_typed_request("docs", input=input))

    @mcp.tool()
    def schema_analyzer_owl(input: dict[str, Any], outputOptions: dict[str, Any] | None = None) -> dict[str, Any]:
        """OWL export for a prior analysis. ``outputOptions.owlFormat`` = turtle | jsonld."""
        return run_tool(_typed_request("owl", input=input, outputOptions=outputOptions))

    return mcp


def serve(
    transport: str = "stdio",
    *,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
) -> None:
    """Run the MCP server on the chosen transport.

    stdio runs in-process; remote transports build the Starlette app, attach the
    bearer-token gate when a token is configured, and serve via uvicorn.
    """
    app_obj = build_app(host=host, port=port)

    if transport == "stdio":
        app_obj.run(transport="stdio")
        return

    if transport not in REMOTE_TRANSPORTS:
        raise ValueError(f"Unsupported transport: {transport!r}")

    if not token:
        logger.warning(
            "Serving MCP over %s with no auth token (%s unset): anyone who can reach %s:%s can drive the "
            "analyzer against arbitrary databases. Set %s before exposing this server.",
            transport,
            TOKEN_ENV_VAR,
            app_obj.settings.host,
            app_obj.settings.port,
            TOKEN_ENV_VAR,
        )

    starlette_app = app_obj.sse_app() if transport == "sse" else app_obj.streamable_http_app()
    if token:
        _install_auth(starlette_app, token)

    import uvicorn

    uvicorn.run(starlette_app, host=app_obj.settings.host, port=app_obj.settings.port)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="arangodb-schema-analyzer-mcp",
        description="MCP server for the ArangoDB schema analyzer (v1 tool contract).",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", *REMOTE_TRANSPORTS),
        default=os.environ.get(TRANSPORT_ENV_VAR, "stdio"),
        help="Transport to serve (default: stdio, or %(prog)s env).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(HOST_ENV_VAR, DEFAULT_MCP_HOST),
        help=f"Bind host for remote transports (default: {DEFAULT_MCP_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get(PORT_ENV_VAR, DEFAULT_MCP_PORT)),
        help=f"Bind port for remote transports (default: {DEFAULT_MCP_PORT}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    serve(
        args.transport,
        host=args.host,
        port=args.port,
        token=os.environ.get(TOKEN_ENV_VAR),
    )


if __name__ == "__main__":
    main()
