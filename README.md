# arangodb-schema-analyzer

Standalone Python library that analyzes an ArangoDB database's physical schema and produces:

- a **conceptual schema** (entities, relationships, properties)
- a **conceptual→physical mapping** suitable for transpilers (Cypher, SPARQL, future)
- **metadata** (confidence, timestamp, analyzed collection counts, detected patterns,
  per-entity tenant scope, deployment-style sharding profile, named-graph membership)

Analysis covers the whole database by default and labels named-graph membership
(`metadata.graphMembership` + per-entry `graphs`); pass `graph_scope` /
`analysisOptions.graphScope` to restrict analysis to a single named graph.

Current release: see [`CHANGELOG.md`](CHANGELOG.md). The version is the single
source of truth in [`pyproject.toml`](pyproject.toml).

## Install

From source (this repo):

```bash
python -m pip install -e .
```

Optional LLM provider extras:

```bash
python -m pip install -e ".[openai]"
python -m pip install -e ".[anthropic]"
python -m pip install -e ".[openrouter]"
```

OpenRouter actually requires no extra SDK (uses stdlib `urllib`); the `[openrouter]` extra exists as a documentation marker only and pulls in nothing.

**MCP (Model Context Protocol)** — optional server wrapping the v1 JSON tool contract:

```bash
python -m pip install -e ".[mcp]"
arangodb-schema-analyzer-mcp                                   # stdio (local IDE)
arangodb-schema-analyzer-mcp --transport sse --host 0.0.0.0 --port 8000        # remote
arangodb-schema-analyzer-mcp --transport streamable-http --port 8000           # remote
```

Exposes both generic tools (`arangodb_schema_analyzer_run` / `_run_json`) and
typed per-operation tools (`schema_analyzer_snapshot|analyze|export|docs|owl`).

**Remote transports are security-gated.** Set `SCHEMA_ANALYZER_MCP_TOKEN` and
every HTTP request must send `Authorization: Bearer <token>` (constant-time
checked; missing/invalid → `401 UNAUTHENTICATED`). If unset, the server still
starts but logs a loud warning — never expose an unauthenticated remote server.
The `run_tool` trust boundary (`SCHEMA_ANALYZER_ALLOWED_HOSTS` /
`SCHEMA_ANALYZER_CACHE_ROOT`) is enforced for every transport. Flags fall back
to `SCHEMA_ANALYZER_MCP_TRANSPORT` / `_HOST` / `_PORT`.

**Development extras** (pytest, ruff, mypy, etc.):

```bash
python -m pip install -e ".[dev]"
```

If you don't install a provider SDK (or you don't provide an API key), analysis degrades gracefully to deterministic baseline inference.

## Usage

```python
from arango import ArangoClient

from schema_analyzer import AgenticSchemaAnalyzer

client = ArangoClient(hosts="http://localhost:8529")
db = client.db("mydb", username="root", password="openSesame")

analyzer = AgenticSchemaAnalyzer(
    llm_provider="openai",  # or "anthropic" or "openrouter"
    api_key=None,  # e.g. os.environ["OPENAI_API_KEY"]
    model="gpt-4o-mini",
    cache={"type": "filesystem", "directory": ".schema-analyzer-cache"},
)

analysis = analyzer.analyze_physical_schema(
    db,
    timeout_ms=60_000,
    sample_limit_per_collection=5,
)

print(analysis.metadata.confidence)
```

## Tool usage (CLI)

This project can be called as a **non-interactive tool** (stdin JSON → stdout JSON) using the v1 contract under `docs/tool-contract/v1/`.

Install (editable):

```bash
python -m pip install -e .
```

Example (analyze) using the provided request example:

```bash
cat docs/tool-contract/v1/examples/request.analyze.json | arangodb-schema-analyzer --pretty
```

### CLI options

```
arangodb-schema-analyzer [--request FILE] [--out FILE] [--pretty] [-v|--verbose]
```

- `--request FILE` — path to request JSON (default: read from stdin)
- `--out FILE` — write response JSON to file (default: stdout)
- `--pretty` — pretty-print JSON output
- `-v` / `--verbose` — enable verbose logging

### Convenience subcommands

Point at a database and emit a single artifact directly (no hand-written
request JSON):

```bash
arangodb-schema-analyzer snapshot --url http://localhost:8529 --database mydb --password ...
arangodb-schema-analyzer analyze  --database mydb --provider openai --api-key-env-var OPENAI_API_KEY
arangodb-schema-analyzer docs     --database mydb            # Markdown
arangodb-schema-analyzer owl      --database mydb --format jsonld
```

Connection args fall back to `ARANGO_URL`/`ARANGO_DB`/`ARANGO_USER`/`ARANGO_PASS`
env vars. Prefer `--password-env-var NAME` over inline `--password`.

## Evaluation CLI

Run analysis quality benchmarks against domain packs:

```bash
arangodb-schema-analyzer eval \
  --provider openai \
  --model gpt-4o-mini \
  --report eval_report.json
```

Pass `--baseline <prior-report.json>` to diff a new run against an earlier
report. The CI regression gate compares against the checked-in
`eval/baselines/ci_no_llm_baseline.json` (regenerated 2026-09-14 after the
3.12 discriminator fix; PRD §3.12.3); any report you saved with `--report`
also works as a baseline.

Each run records its outcome: `ok`, `fell_back_to_baseline` (an LLM was requested but
the deterministic baseline answered — the provider failed or had no API key), or
`error` (the run raised; the eval carries on with the next run). The report's
`health` block counts them, and its headline `summary.scores` and `calibration` are
computed over the `ok` runs only. When more than 5% of runs fell back or errored those
headline numbers are withheld (null, with `summary.withheld_reason`); pass
`--ignore-health` to report them anyway.

Options: `--url`, `--user`, `--password`, `--database`, `--domains`, `--sample-limit`, `--timeout-ms`, `--scale`, `--ignore-health`, `--no-cleanup`.

Domains included: `healthcare`, `financial_fraud_detection`, `insurance`, `intelligence`, `network_asset_management`.

## Public API

Exports (see `schema_analyzer/__init__.py`):

- `AgenticSchemaAnalyzer` — main analyzer class
- `ConceptualSchema` — conceptual schema dataclass
- `PhysicalMapping` — physical mapping dataclass with AQL helpers
- `generate_schema_docs(analysis)` — Markdown documentation generator
- `export_mapping(analysis, target)` — transpiler export (`cypher` or `sparql`)
- `build_cypher_resolution_index(analysis)` — flattened label/rel-type → AQL
  lookup for a Cypher transpiler (built on the `PhysicalMapping` AQL helpers)
- `diff_analyses(previous, current)` — structural diff between two analyses
  (added/removed/changed entities & relationships, mapping-style flips,
  health-score delta)
- `export_conceptual_model_as_owl_turtle(analysis)` — OWL Turtle export
  (with `rdfs:subClassOf`, functional/inverse-functional characteristics,
  observed cardinality, and `owl:inverseOf`)
- `export_conceptual_model_as_jsonld(analysis)` — OWL conceptual model as JSON-LD
- `compute_gold_comparison(conceptual, reference)` — precision/recall/F1 of the
  conceptual schema vs a supplied gold reference (also via
  `AgenticSchemaAnalyzer(gold_reference=...)` / `analysisOptions.goldReference`)
- `metric_snapshot` / `append_to_history` / `summarize_history` /
  `record_metrics` — quality-metric history (trend lines across analysis runs)
- `to_csi(analysis)` / `from_csi(doc)` — Conceptual Schema Interchange (CSI) v1,
  a direction-agnostic document (also via the `csi` tool operation) for
  interop with forward relational→graph tools
- `register_provider(name, ...)` — register custom LLM providers
- `list_providers()` — list registered LLM provider names
- `run_tool(request_dict)` — programmatic entrypoint to the v1 tool contract
- `fingerprint_physical_schema(snapshot)` — full-snapshot SHA-256 (cache key)
- `fingerprint_physical_shape(db, *, exclude_collections=None)` — cheap probe
  that hashes only the collection set + per-collection type + index digests
- `fingerprint_physical_counts(db, *, exclude_collections=None)` — shape
  fingerprint combined with per-collection `count()`
- `assess_change_state(db, *, prior_shape, prior_counts)` — four-valued change
  state (`unchanged` / `stats_changed` / `shape_changed` / `no_cache`) from the
  cheap probes, without a full snapshot
- `refresh_statistics(db, prior)` — stats-only refresh that preserves the cached
  conceptual schema + physical mapping (see `AnalysisResult.metadata.incrementalRefresh`)
- `AgenticSchemaAnalyzer.analyze_incremental(db, *, prior=...)` — full re-analysis
  on shape change, stats-only refresh on count change, reuse when unchanged
- `AgenticSchemaAnalyzer(domain_context=...)` / `domainContext` request field —
  supply explicit business-domain priors that override auto-detection
- `RedactionOptions(strip_samples/mask_field_values/mask_field_names)` /
  `analysisOptions.redaction` — scrub the snapshot before LLM egress;
  `maskFieldNames` tokenizes field names and round-trips them back to real names
  in the output (`build_field_name_map` / `unmask_field_names`);
  `screen_field_values` (Python only) masks just the fields a caller's
  `(collection, field, values) -> bool` function picks, e.g. personal data

## Recent additions

See [`CHANGELOG.md`](CHANGELOG.md) for the full history. Highlights since 0.3.0:

- **0.14.0** — **LPG type detection converged on the analyzer** (CDF unified-
  architecture paper Q-5): tier-1 type-field names (`type`, `_type`, `entityType`,
  `@type`, `entity_type`; edges `type`, `relation`, `relationship`, `relType`,
  `predicate`) are accepted on coverage alone, broadened names need a type-like
  token, and edges carrying `_fromType` / `_toType` resolve endpoints without
  `DOCUMENT()` lookups. This analyzer is the portfolio's owner of "what is the
  type field"; `arango-ontoextract` and `arango-cypher-py` consume its answer.
- **0.13.1** — CSI `provenance.validTimeSource` admits all five producer values
  (`catalog`, `event`, `file`, `fingerprint-continuity`, `observed`), matching
  `relational-schema-analyzer` 0.8.
- **0.13.0** — **bitemporal stamping** (PRD §3.13.5): every result and CSI
  document carries `transactionTime`, `validTime.from`, `validTimeSource` and
  `predecessorFingerprint`; valid time by fingerprint continuity from a prior
  run (`input.previousAnalysis` / `--prior-run`).
- **0.12.x** — `FOREIGN_KEY` / `JOIN_TABLE` relationship styles, FK inference
  to any single-column candidate key, `detectForeignKeys` /
  `sampleForeignKeyOverlap` reachable through the v1 tool contract, and the
  fix for LPG discriminator detection being inert on ArangoDB 3.12.
- **0.11.0** — temporal element provenance (PRD §3.13.2), cache schema
  versioning + invalidate, and the CI eval regression gate.
- **0.10.0** — **caller-supplied `domainContext`** (override auto domain
  detection), **incremental re-analysis** (`analyze_incremental` +
  `assess_change_state` / `refresh_statistics` with stored shape/counts
  fingerprints and stats-only refresh, PRD §3.13.3), and **field-name masking**
  (`analysisOptions.redaction.maskFieldNames`) that tokenizes field names before
  LLM egress and round-trips them back to real names.
- **0.9.0** — **named-graph awareness**: whole-DB analysis now labels
  named-graph membership (`metadata.graphMembership` + per-entry `graphs`), with
  optional single-graph `graphScope`. Plus **GraphRAG template detection**
  (`metadata.graphRag`), **gold-standard comparison + metric history**
  (`compute_gold_comparison`, `metric_snapshot`/`record_metrics`), and
  **Conceptual Schema Interchange (CSI) v1** (`to_csi`/`from_csi` + the `csi`
  op) for interop with a forward relational→graph tool. Includes a fix so named
  graph definitions parse correctly against live ArangoDB.
- **0.8.0** — **confidence calibration from eval feedback**
  (`schema_analyzer/eval/calibration.py`): reliability curve, ECE/MCE/Brier, an
  overconfidence gap, and a `recommended_review_threshold`, surfaced in the
  `eval` CLI and report-comparison drift section (eval reports are now
  `{"runs", "calibration"}`; legacy list baselines still diff). Plus **MCP
  remote transports** (sse / streamable-http with bearer-token auth, §3.11) and
  a hardened **transpiler export contract** — SPARQL `datatypeProperties` for
  literal triple patterns, and `docs/transpiler-integration.md` for transpiler
  authors (§6.4).
- **0.7.0** — `metadata.qualityMetrics` + `metadata.healthScore`
  (structural/grounding signals + 0–100 composite), element-level `source`
  provenance (`llm`/`baseline`/`human`), `diff_analyses()`, a **SPARQL** export
  target, a Cypher resolution adapter, and `analysisOptions.redaction`
  (`stripSamples` / `maskFieldValues`) for LLM-egress scrubbing. mypy is now a
  blocking CI gate and the coverage floor is 80%.
- **0.6.0 — Shard-family detection** (`physicalMapping.shardFamilies`)
  groups conceptual entities that share an identical property set and a
  common name suffix (the per-source / per-repo collection-duplication
  pattern), so downstream consumers can emit UNION-aware guidance
  instead of silently picking one member. Plus **multitenancy classification**
  (`metadata.multitenancy`) layered on the sharding profile.
- **0.5.0 — Sharding-profile classification.** Every analysis stamps
  `metadata.shardingProfile` with one of `OneShard`, `DisjointSmartGraph`,
  `SmartGraph`, `SatelliteGraph`, or `Sharded`, plus per-graph and
  per-collection evidence. Snapshot-only, no extra DB round trip.
- **0.4.0 — Tenant-scope annotations.** Every entity in
  `physicalMapping.entities[*]` now carries a `tenantScope` block
  (`tenant_root` / `tenant_scoped` / `global`), with a per-run
  `metadata.tenantScopeReport` summary. Configurable via
  `SCHEMA_ANALYZER_TENANT_*` env vars.
- **0.3.0 — Cheap change-detection probes** (`fingerprint_physical_shape`,
  `fingerprint_physical_counts`), **statistics block** on
  `metadata.statistics`, and a **reconciliation step** that backfills any
  collections the LLM omitted.

## Configuration

Tunable defaults live in `schema_analyzer/defaults.py` (full list there).
Selected parameters:

| Parameter | Default | Description |
|---|---|---|
| `MAX_REPAIR_ATTEMPTS` | 2 | LLM repair loop iterations |
| `LLM_TEMPERATURE` | 0.0 | Sampling temperature |
| `DEFAULT_TIMEOUT_MS` | 60000 | Analysis timeout (ms) |
| `DEFAULT_REVIEW_THRESHOLD` | 0.6 | Confidence threshold for `review_required` |
| `DEFAULT_CACHE_TTL_SECONDS` | 86400 | Cache TTL (seconds) |
| `TENANT_SCOPE_ROOT_NAMES` | `("Tenant",)` | Entity names treated as tenant roots |
| `TENANT_SCOPE_FIELD_REGEX` | `^tenant[_-]?(id&#124;key)$` | Denormalised tenant-reference field detector (regex pipe escaped as `&#124;` to avoid breaking the markdown table) |
| `MIN_TENANT_FIELD_COVERAGE_FRACTION` | 0.5 | Threshold for `discriminator_field` multitenancy |
| `MIN_SHARD_FAMILY_SIZE` | 2 | Min members for a shard-family group |

## Notes

- **Secrets**: API keys are read from config/env; never persisted by this library.
- **AQL fragments**: helper methods return AQL text + bind variables; collection names are passed via bind parameters.
- **Graceful degradation**: without an LLM provider, the analyzer returns deterministic baseline inference with `review_required=True`.

## Integration evaluation (Docker ArangoDB)

Bring up a local ArangoDB:

```bash
docker compose up -d
```

Run integration tests (opt-in):

```bash
export RUN_INTEGRATION=1
export ARANGO_URL=http://localhost:18529
export ARANGO_DB=schema_analyzer_it
export ARANGO_USER=root
export ARANGO_PASS=openSesame
pytest -q -m integration
```
