# Tool Contract v1 (Schema Analyzer)

This document defines the **v1 JSON contract** for calling this project as a non-interactive tool from other agentic workflows.

## Overview

- **Request schema**: `request.schema.json`
- **Response schema**: `response.schema.json`
- **Examples**: `examples/`

The tool is designed to be callable via:

- **Library**: `schema_analyzer.tool.run_tool(request_dict) -> response_dict`
  (also re-exported as `schema_analyzer.run_tool`)
- **CLI**: `arangodb-schema-analyzer` (stdin JSON → stdout JSON)
- **MCP**: `arangodb-schema-analyzer-mcp` — stdio MCP server that exposes the
  same operations to MCP-capable clients (requires the `[mcp]` install extra)

## Key principles

- **Stable**: `contractVersion` is required and must be `"1"`.
- **Non-interactive**: no prompts; all inputs are in the request JSON.
- **Structured errors**: failures return `ok=false` and an `error` object.
- **Secrets**: prefer `*EnvVar` fields instead of embedding secrets in JSON.

## Operations

- `snapshot`: connect to ArangoDB and return a deterministic physical schema snapshot
- `analyze`: snapshot + run the agentic analyzer, returning analysis JSON
- `export`: export analysis to a stable JSON contract for transpilers
  (`cypher` or `sparql` via `outputOptions.exportTarget`; additional targets
  may be added under the same operation without bumping `contractVersion`)
- `docs`: produce Markdown documentation from an analysis result
- `owl`: export conceptual schema + physical mapping as OWL
  (`turtle` by default, or `jsonld` via `outputOptions.owlFormat`)
- `diff`: structural diff of `input.previousAnalysis` vs `input.analysis`
  (added/removed/changed entities & relationships, mapping-style flips,
  health-score delta)
- `resolve`: flattened label/relationship-type → AQL resolution index for a
  Cypher transpiler, built from `input.analysis`
- `csi`: Conceptual Schema Interchange v1 document (`to_csi`) for
  `input.analysis`. `outputOptions.csiPublisherUri` (an absolute http(s) URI
  without query or fragment, one per published document) records
  `provenance.publisher.uri`, gives every mapping entry an id under it and also
  writes `physicalMapping` beside `arangoPhysicalMapping`; without it the
  document is unchanged. The URI may instead come from
  `input.analysis.metadata.publisherUri` (kept by `from_csi`). Ids already in the
  analysis are kept, never re-minted. The request fails with `INVALID_REQUEST`,
  naming the field or the offending ids, when the publisher URI is malformed, when
  `csiPublisherUri` and `metadata.publisherUri` differ, when carried ids belong to
  a different publisher, when a `propertyIds` entry names
  a property that no longer exists, or when the analysis uses a reserved key
  otherwise: `id` and `propertyIds` on a mapping entry, and `id` on a foreign-key
  `reference`, are reserved for mapping ids. An id is stable across a `from_csi`
  round trip, and not across a fresh analysis that renames an entity or property
  (carrying ids across a rename is issue #52). To move a document to a new
  publisher on purpose, remove its mapping ids and `metadata.publisherUri`, then
  supply the new `csiPublisherUri`

## Request / Response

See the JSON Schema files:

- `request.schema.json`
- `response.schema.json`

## Examples

- `examples/request.analyze.json`
- `examples/response.analyze.json`

## Trust model & operator-side hardening

`run_tool` was originally written for a **trusted local caller** (CLI, in-process
library, your own MCP server). Every request field that drives I/O is
honoured as-is. When you expose this contract over a network boundary
(MCP server, HTTP gateway, agent-to-agent RPC), the *operator* — not the
caller — is responsible for the trust boundary. The library ships two
opt-in environment variables to let you tighten that boundary without
forking the contract:

| Env var | Purpose | Behaviour when unset |
|---|---|---|
| `SCHEMA_ANALYZER_ALLOWED_HOSTS` | Comma-separated `host[:port]` list. `connection.url` is rejected unless its host (or `host:port`) appears in the list. | All hosts allowed (preserves CLI ergonomics). |
| `SCHEMA_ANALYZER_CACHE_ROOT` | Absolute path. `analysisOptions.cache.directory` (after resolution of `~` and `..`) must lie under this root. | Any caller-supplied directory is accepted. |

Additional caps are encoded directly in `request.schema.json`:

- `connection.url` ≤ 2048 chars; passwords / API keys ≤ 1024 chars; system
  prompts ≤ 64 KiB; arrays inside `input.analysis.*` capped at 50–100 K
  items.
- `analysisOptions.timeoutMs` ≤ 600 000 (10 min);
  `sampleLimitPerCollection` ≤ 1000; `maxRepairAttempts` ≤ 10;
  `cacheTtlSeconds` ≤ 31 536 000 (1 year).

`input.snapshot` (the free-form snapshot blob used by `export` / `docs` /
`owl`) cannot be size-bounded by JSON Schema alone. Hosts that expose
this contract to untrusted callers SHOULD enforce a request-body cap at
the transport layer (e.g. a few MiB).

Use `passwordEnvVar` / `apiKeyEnvVar` rather than inlining secrets, so
that request payloads can be safely logged. The analyzer never echoes
secret material into the response.

