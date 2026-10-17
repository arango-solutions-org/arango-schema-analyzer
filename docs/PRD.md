# Product Requirements Document (PRD)

## **Project Name:** ArangoDB Schema Analyzer

**Package**: `arangodb-schema-analyzer` (PyPI)
**Import**: `schema_analyzer`
**Language**: Python ≥ 3.10
**Version**: tracked in `pyproject.toml` — see `CHANGELOG.md` for the current release

**Companion codebase:** **Arango-OntoExtract (AOE)** is developed in the **`arango-ontoextract`** repository. A typical local layout is `~/code/arango-ontoextract` alongside this repo. Cross-references in this document mean that project.

---

### **1. Executive Summary**

The **ArangoDB Schema Analyzer** is a standalone Python library that uses algorithmic heuristics and Large Language Models (LLMs) to reverse-engineer a **conceptual model** from an existing ArangoDB physical schema. It simultaneously generates a machine-readable **mapping layer** that links conceptual entities to their physical implementations. This enables language transpilers (Cypher, SPARQL, SQL) to generate correct AQL for schemas that mix Property Graph and Labeled Property Graph patterns.

The system operates as:
- A **Python library** (`AgenticSchemaAnalyzer`)
- A **non-interactive CLI tool** (`arangodb-schema-analyzer`) using stdin/stdout JSON per the v1 tool contract
- An **evaluation harness** for benchmarking analysis quality across domain packs
- An **MCP stdio server** (`arangodb-schema-analyzer-mcp`, ships in the
  `[mcp]` extra; see §3.11) that exposes the same operations as MCP tools
  for AI agents and IDEs

**Quality and lineage (directional):** v0.1 ships **metadata confidence**, **review gating**, and **offline eval F1 scores** against domain packs. Broader **ontology-quality metrics** and **temporal provenance** for re-analysis and schema drift are specified in §3.12 and §3.13 to align with the patterns used in **Arango-OntoExtract (AOE)** (`arango-ontoextract` — multi-signal scoring, health score, extraction run records, and temporal diff).

---

### **2. Problem Statement**

ArangoDB schemas "in the wild" follow different modeling conventions:

* **Property Graph (PG):** Typed vertex/edge collections (e.g., `Users`, `Follows`) — one collection per concept.
* **Labeled Property Graph (LPG):** Generic collections with discriminator fields (e.g., a single `entities` collection with a `type` field distinguishing `User`, `Post`, etc.).
* **Hybrid:** Combinations where some concepts use dedicated collections and others share generic collections with type fields.

Without a unified map of *how* a concept is physically implemented, transpilers cannot generate correct AQL. This library produces that map.

---

### **3. Functional Requirements**

#### **3.1. Physical Schema Introspection**

The system must connect to an ArangoDB database and produce a deterministic **physical schema snapshot** containing:
- Collection metadata (name, type, count, properties, indexes)
- Named graph definitions (edge definitions, orphan collections)
- Candidate type fields detected from sample documents
- Sample field value distributions for type-field inference

**Implementation**: `snapshot_physical_schema()` in `snapshot.py`.

#### **3.2. Conceptual Schema Inference**

The system must produce a **conceptual schema** with:
- **Entities**: named types with labels and properties (e.g., `User`, `Post`)
- **Relationships**: typed directed edges between entities (e.g., `FOLLOWS: User → User`)
- **Properties**: global property definitions

**Implementation**: `ConceptualSchema` dataclass in `conceptual.py`.

#### **3.3. Physical Mapping Generation**

The system must produce a **conceptual → physical mapping** describing how each conceptual entity/relationship maps to ArangoDB collections:

| Mapping Style | Entity/Relationship | Description |
|---|---|---|
| `COLLECTION` | Entity | One document collection per entity type |
| `LABEL` | Entity | Shared collection filtered by `typeField == typeValue` |
| `DEDICATED_COLLECTION` | Relationship | One edge collection per relationship type |
| `GENERIC_WITH_TYPE` | Relationship | Shared edge collection filtered by `typeField == typeValue` |
| `FOREIGN_KEY` | Relationship | Relationship carried by a scalar attribute on the source document that references a target document's key (SQL-style, never enforced by ArangoDB); compiled as a keyed document lookup, not an edge traversal. |
| `JOIN_TABLE` | Relationship | Relationship reified as a document collection linking two entities (SQL-style junction table), optionally carrying its own properties; compiled as a two-hop traversal for a conceptually one-hop relationship. |

The `FOREIGN_KEY` and `JOIN_TABLE` relationship styles are emitted by
`PhysicalMapping.aql_relationship_traversal()` in `mapping.py` (`_aql_foreign_key`,
`_aql_join_table`), preserving the injection-safe `{query, bind_vars, edge_variable}`
contract; a `FOREIGN_KEY` mapping is produced from a detected reference by
`InferredForeignKey.to_mapping()` in `fk_inference.py` (see §6.2). These names are
shared deliberately with `relational-schema-analyzer` so the two dialects converge.

`LABEL`-style entity mappings must be **label-faithful**: when the PascalCase
entity name is lossy relative to the raw discriminator value that appears in
the data (`FIN_METRIC` → `FINMETRIC`), the raw value is recorded in the
mapping's `aliases` list and copied into the transpiler-facing blocks of the
`cypher` export and `build_cypher_resolution_index`, so a query author who
writes the real data value resolves the entity without downstream
normalization heuristics (see
`docs/cypher-vocabulary-fidelity-bug-report.md` issue #1).

**Implementation**: `PhysicalMapping` dataclass in `mapping.py`, with `aql_entity_match()` and `aql_relationship_traversal()` helper methods that produce injection-safe AQL fragments; alias derivation in `baseline.py` (LABEL branch of `infer_baseline_from_snapshot`), export propagation via `exports.py::_ENTITY_RESOLUTION_KEYS`.

#### **3.4. Hybrid Pattern Detection**

The system must automatically detect and classify physical schema patterns:

- **PG (Property Graph):** Distinct vertex collections, no discriminator fields → `COLLECTION` / `DEDICATED_COLLECTION` mapping.
- **LPG (Labeled Property Graph):** Generic node/edge collections with discriminator fields (`type`, `_type`, `entityType`, `@type`, `entity_type`, `kind`, `category`, `label`; edges: `relation`, `relType`, `relationship`, `predicate`, `type`) → `LABEL` / `GENERIC_WITH_TYPE` mapping.

**Type-detection ownership (2026-09-14, CDF unified-architecture paper Q-5).** This analyzer is the single owner of LPG type detection for the portfolio; `arango-ontoextract` and `arango-cypher-py` consume the `LABEL` / `GENERIC_WITH_TYPE` entries in CSI rather than re-detecting. Two tiers of candidate names: **tier-1** names (`type`, `_type`, `entityType`, `@type`, `entity_type`; edges `type`, `relation`, `relationship`, `relType`, `predicate`) are unambiguous and are accepted on coverage alone (≥ 2 observed values, broadly present), regardless of the distinct-count acceptance bound; **tier-2** names (`label`, `category`, `kind`, …) may hold a display value and must pass the full distribution gate. Edges carrying endpoint types directly (`_fromType` / `_toType` and variants) resolve per-relation domain/range without `DOCUMENT()` lookups.

Detection uses candidate type field analysis from `sample_field_value_counts` in the snapshot.

The per-field distinct-value sampling behind detection is capped
(`SAMPLE_VALUE_TOP_K`, default 20) and that cap must be **configurable and
transparent** (see `docs/cypher-vocabulary-fidelity-bug-report.md` issue #2):

- The snapshot records each candidate field's true distinct total
  (`sample_field_distinct_counts`) and up to `SAMPLE_VALUE_OVERFLOW_K` (50)
  beyond-top-K values with counts (`sample_field_value_overflow`).
- Discriminator values dropped by the cap are reported in
  `metadata.entityTypeCaps` / `relationshipTypeCaps` — per collection: type
  field, distinct total, exported/dropped counts, and the dropped classes
  with counts — on both the baseline and LLM analysis paths, never silently.
- Callers can raise the cap via `analyze_physical_schema(max_entity_types=…)`
  (sync + async) / `snapshot_physical_schema(sample_value_top_k=…)`; the
  discriminator acceptance bound (`MAX_TYPE_FIELD_DISTINCT_VALUES`) scales
  with the raised cap. The bound governs **tier-2** discriminator names only; tier-1
  names are accepted on coverage alone (see §3.7 type-detection ownership), so a rich
  graph is mapped to its sampled top-K types with the rest reported in the caps rather
  than collapsed to one class. LLM-egress redaction masks the new snapshot keys the
  same way as their top-K counterparts.

For a labeled property graph whose full label vocabulary lives in a single
discriminator field, callers can pass `min_type_value_count > 0` (**full-label-set
mode**) to `snapshot_physical_schema` / `analyze_physical_schema`. The snapshot
then retains **every** distinct discriminator value whose observed count meets the
floor — rather than only the top-K — so label-rooted queries resolve against the
complete vocabulary; the floor guards against a pathological free-text field
returning unbounded classes. **Implementation**: `snapshot.py`
(`min_type_value_count` parameter), threaded via the analyzer; floor default in
`defaults.py`. _Shipped alongside a fix to a reserved-word `COLLECT` bug
(unquoted `distinct` object key) that had silently disabled discriminator
detection against a live database._

The automatic discriminator heuristic must be **overridable**: on a pure-PG schema
(one collection per type) whose collections carry a plausible-looking but
non-discriminating field (e.g. a high-cardinality `region` attribute), auto
detection can shatter each collection into value-classes. Callers must be able to
pass `entity_strategy="collection"` to `analyze_physical_schema` (sync and async)
to skip discriminator inference entirely and map one entity per document
collection (`COLLECTION`) and one relationship per edge collection
(`DEDICATED_COLLECTION`), deterministically and without LLM involvement. The
default `entity_strategy="auto"` preserves the detection behavior above.

**Implementation**: Baseline heuristic inference in `baseline.py` (`infer_baseline_from_snapshot()`); `entity_strategy` override in `analyzer.py` (`analyze_physical_schema`, threaded via `_prepare_analysis` to `infer_baseline_from_snapshot(collection_per_entity=…)`, which skips discriminator selection in its entity and relationship branches); cap sampling/reporting in `snapshot.py` (`_detect_type_fields_via_collect`) and `baseline.py` (`type_value_caps_from_snapshot`).

#### **3.5. Agentic Semantic Enrichment (LLM)**

When an LLM provider is configured, the system uses it to:
- Infer richer semantics than heuristics alone (entity naming, relationship endpoint resolution)
- Generate a conceptual schema + mapping from the physical snapshot
- Self-repair via a generate → validate → repair loop

When no LLM is available (no provider or no API key), the system **degrades gracefully** to deterministic baseline inference.

**Human-in-the-loop:** `metadata.review_required` (with `confidence` vs `reviewThreshold`) signals that outputs are **provisional**. This library does not implement curation UIs; consumers (e.g. **AOE** in `arango-ontoextract`) own approval, edit, and promotion workflows.

**Implementation**: `AgenticSchemaAnalyzer` in `analyzer.py`, with the generate-validate-repair loop in `workflow.py`.

#### **3.6. LLM Provider Support**

The system supports multiple LLM providers via a pluggable registry:

| Provider | SDK | Default Model |
|---|---|---|
| OpenAI | `openai` (optional extra) | `gpt-4o-mini` |
| Anthropic | `anthropic` (optional extra) | `claude-3-5-sonnet-latest` |
| OpenRouter | stdlib `urllib` (no extra) | `openai/gpt-4o-mini` |

Custom providers can be registered via `register_provider()`.

**Implementation**: `providers/` package with `LLMProvider` protocol in `providers/base.py`.

#### **3.7. Output Formats**

The system produces multiple output formats:

| Format | Operation | Description |
|---|---|---|
| Analysis JSON | `analyze` | Conceptual schema + physical mapping + metadata |
| Physical Snapshot | `snapshot` | Deterministic physical schema introspection |
| Export JSON | `export` | Stable JSON for transpiler consumption (currently Cypher) |
| Markdown Docs | `docs` | Human-readable schema documentation |
| OWL (Turtle and JSON-LD) | `owl` | Conceptual schema + physical mapping as OWL 2 ontology with `phys:` annotation properties |

**OWL content.** The OWL export (Turtle and JSON-LD) carries classes, object properties
(relationships, with domains/ranges), and one `owl:DatatypeProperty` per entity property. A
property name shared by several classes takes an `owl:unionOf` domain (repeated `rdfs:domain`
would mean their intersection); `rdfs:range` is an `xsd:` type only when all those classes agree
on an inferred type; `phys:attributeName` records the stored field when it differs from the IRI
local name. Names that would collide with a class or relationship IRI, or with each other after
sanitizing, are omitted rather than punned. Relationship (edge) properties are not yet exported.

**Implementation**: `docs.py`, `exports.py`, `owl_export.py` (`export_conceptual_model_as_owl_turtle`, `export_conceptual_model_as_jsonld`).

#### **3.8. Tool Contract (v1)**

The system exposes a **stable non-interactive JSON API** defined by JSON Schema:

- **Request schema**: `docs/tool-contract/v1/request.schema.json`
- **Response schema**: `docs/tool-contract/v1/response.schema.json`

Operations: `analyze`, `snapshot`, `export` (targets `cypher` | `sparql`), `docs`, `owl` (`outputOptions.owlFormat` `turtle` | `jsonld`), `diff` (`input.analysis` vs `input.previousAnalysis` → `diff_analyses`), `resolve` (`build_cypher_resolution_index`), and `csi` (`to_csi`).

All requests and responses are validated against the JSON Schema. Failures return structured `{ "ok": false, "error": { "code": "...", "message": "..." } }` responses for both expected errors (`SchemaAnalyzerError`) and unexpected exceptions.

Requests carry an `analysisOptions` object (defined in `request.schema.json`). Among
its fields, `analysisOptions.entityStrategy` (`"auto"` | `"collection"`, default
`"auto"`) exposes the §3.4 discriminator override at the tool boundary: `"collection"`
maps one entity per document collection and one relationship per edge collection,
skipping discriminator inference. `tool.py` validates the value and returns
`{ "ok": false, "error": { "code": "INVALID_REQUEST" } }` for any other value before
analysis runs.

`analysisOptions.detectForeignKeys` (boolean, default `false`) turns on inference of
relationships carried by scalar reference attributes (document-modelled foreign keys),
so a database of document collections with no edge collections still yields
relationships (§6.2). `analysisOptions.sampleForeignKeyOverlap` (boolean, default
`false`) additionally confirms each candidate by sampling values against the target
collection's keys. Both must be **declared in `request.schema.json`** as well as read by
`tool.py`: `analysisOptions` is `additionalProperties: false`, so an option the schema
does not declare is rejected at validation and is unreachable through the contract even
if the code reads it (see §4.7).

`input.previousAnalysis` (the pre-existing shared contract field — a prior analysis
`{conceptualSchema, physicalMapping, metadata}`, already required by `diff`) routes `analyze`
through `analyze_incremental` (§3.13.3): a stats-only refresh or the prior annotated when the
shape fingerprint still matches, a full re-analysis otherwise. It is also how a contract
consumer obtains **bitemporal fingerprint-continuity valid time** (§3.13.5) — unreachable
through the contract without a prior run. Reusing the existing field (rather than minting a new
one) keeps this project, `relational-schema-analyzer`, and CDF converged. The CLI exposes it as
`analyze --prior-run FILE` (a saved `analyze` result or its bare analysis).

**Implementation**: `tool.py` (entrypoint + `analysisOptions.entityStrategy` / `detectForeignKeys` / `sampleForeignKeyOverlap` validation/threading, `input.previousAnalysis` → `analyze_incremental`), `cli.py` (`analyze --prior-run FILE` → `input.previousAnalysis`), `tool_contract_v1.py` (schema loading and validation), `tool_contract/v1/request.schema.json` (`input.previousAnalysis` already declared as an `AnalysisOutput`).

#### **3.9. CLI**

Three surfaces:

1. **Tool mode** (default): `arangodb-schema-analyzer [--request FILE] [--pretty] [--out FILE] [-v]` — stdin/stdout JSON using the v1 contract.
2. **Convenience subcommands**: `snapshot | analyze | docs | owl` with `--url --database --user --password|--password-env-var --out --pretty -v`; `analyze`/`docs`/`owl` add `--provider --model --api-key-env-var`; `analyze --prior-run FILE` (→ `input.previousAnalysis`, §3.8) and `analyze --declared-references FILE` (→ `analysisOptions.declaredReferences`, §6.2); `owl --format turtle|jsonld`. These build v1 requests and call `run_tool`.
3. **Eval mode**: `arangodb-schema-analyzer eval [--url --user --password --database] [--provider NAME] [--model NAME] [--domains LIST] [--sample-limit N] [--timeout-ms N] [--scale N] [--report FILE] [--baseline FILE] [--fail-on-regression] [--no-cleanup]` — run evaluation benchmarks.

**Implementation**: `cli.py`.

#### **3.10. Evaluation Harness**

The system includes a domain-based evaluation framework:

- **Domain packs** (in `domains/`): ground-truth conceptual models for healthcare, financial fraud detection, insurance, intelligence, and network asset management.
- **Physical schema generator**: materializes multiple physical schema variants (PG + LPG) from each domain spec with seeded sample data.
- **Scoring**: entity F1, relationship F1, domain/range F1, mapping style accuracy.
- **Report comparison**: diff two eval reports to track quality regressions/improvements.

**Implementation**: `eval/` package (`domain_loader.py`, `generator.py`, `runner.py`, `scoring.py`).

#### **3.11. MCP Server (optional integration surface)**

**Purpose:** Expose schema-analyzer operations through the **Model Context Protocol (MCP)** so MCP-capable clients (e.g. Cursor, custom agents) can call **analyze**, **snapshot**, **export**, **docs**, and **owl** without bespoke subprocess glue.

**Relationship to the v1 tool contract:** MCP tools are a **thin adapter** over the same inputs/outputs as `run_tool()` / CLI JSON (see §3.8). Parameter schemas should stay aligned with `docs/tool-contract/v1/request.schema.json` (subset per tool).

**Suggested MCP tools (illustrative):**

| MCP tool | Maps to operation | Notes |
|---|---|---|
| `schema_analyzer_snapshot` | `snapshot` | Connection + `analysisOptions` |
| `schema_analyzer_analyze` | `analyze` | Connection + optional `llm` + `analysisOptions` |
| `schema_analyzer_export` | `export` | `input.analysis` + `exportTarget` |
| `schema_analyzer_docs` | `docs` | `input.analysis` |
| `schema_analyzer_owl` | `owl` | `input.analysis` |

**Requirements:**

- **Secrets:** Prefer env-indirection (`passwordEnvVar`, `apiKeyEnvVar`) in tool parameters; document that inline secrets in MCP payloads inherit the same risks as inline JSON secrets (logs, transcripts).
- **Errors:** Surface contract-shaped errors (`ok: false`, typed `code`) to the client.
- **Transports:** Support at least **stdio** for local IDE use; **SSE** (or equivalent) optional for remote agents.
- **Implementation status:** **Shipped, incl. remote transports.** The MCP
  server lives in `schema_analyzer/mcp_server.py` (`arangodb-schema-analyzer-mcp`
  console script, `[mcp]` extra). It serves **stdio** (default), **sse**, and
  **streamable-http** (`--transport`, `--host`, `--port`, with
  `SCHEMA_ANALYZER_MCP_TRANSPORT/HOST/PORT` env fallbacks) and exposes both the
  generic `run`/`run_json` tools and the five typed per-operation tools above.
  Remote transports are gated by a bearer token (`SCHEMA_ANALYZER_MCP_TOKEN`;
  missing/invalid → `401 UNAUTHENTICATED`, constant-time compared) and inherit
  the `run_tool` allowlist/cache-root trust boundary uniformly.

**Reference:** AOE documents a full MCP surface for ontology library and extraction (`arango-ontoextract` PRD §6.10); this project scopes MCP to **schema reverse-engineering and mapping** only.

#### **3.12. Quality metrics — extraction and ontology structure**

This library produces a **conceptual schema** and **physical mapping**, optionally serialized as **OWL Turtle** (§3.7). Quality measurement spans **offline benchmarks** (today) and **production-oriented metrics** (target, aligned with AOE §6.13 where applicable).

**3.12.1. Implemented (v0.1) — eval harness**

| Metric | Description |
|---|---|
| Entity F1 | Precision/recall/F1 over normalized entity names vs domain-pack ground truth |
| Relationship F1 | Over relationship types |
| Domain/range F1 | Over relationship signatures (type + endpoints) |
| Mapping-style accuracy | Agreement with expected COLLECTION / LABEL / DEDICATED_COLLECTION / GENERIC_WITH_TYPE patterns |

**3.12.2. Per-run metadata (v0.1)**

| Signal | Use |
|---|---|
| `metadata.confidence` | Single scalar; gating with `review_required` vs `reviewThreshold` |
| `metadata.warnings` / `assumptions` | Human review queues |
| `used_baseline`, `repair_attempts` | Explains whether LLM path ran and how much self-repair occurred |

**3.12.3. Target — richer ontology / extraction quality (roadmap)**

Inspired by **AOE** (`arango-ontoextract`): multi-signal confidence, structural ontology metrics, and composite scores. Mapped to **schema-derived** artifacts (no document chunks unless optional domain context is added later):

| Category | Intended metrics | Notes |
|---|---|---|
| **Structural (schema-derived)** | Relationship connectivity (entities participating in ≥1 relationship), orphan entity ratio, property richness on conceptual entities, cycle / inconsistency flags | Analogous to AOE “connectivity” and OntoQA-style structural checks, applied to conceptual graph + mapping completeness |
| **Grounding vs physical** | Agreement of mapping with snapshot (every mapped collection exists; type fields appear in `sample_field_value_counts` when sampled) | Faithfulness-to-schema analogue of AOE faithfulness-to-chunks |
| **Gold comparison** | Optional recall/precision vs a supplied reference OWL/TTL or domain pack | Analogous to AOE gold-standard recall (`POST /quality/recall` pattern) |
| **Composite** | Normalized **health score** (0–100) combining structural + confidence + optional gold overlap | Analogous to AOE ontology health score |
| **History** | Store metric snapshots per analysis run for trend lines | Analogous to AOE `quality_history` / dashboard history |

**Requirements (acceptance for “metrics complete” milestones):**

- Eval metrics remain the **regression gate** for releases (CI compares reports to baselines).
  _Shipped:_ the Integration workflow runs the deterministic no-LLM eval and fails on any gated
  quality-metric drop vs the committed `eval/baselines/ci_no_llm_baseline.json`
  (`schema-analyzer eval --fail-on-regression`, backed by `eval.report_regressions`;
  confidence deltas are advisory, not gated).
- Any new composite score must document **inputs, formula, and failure modes** (e.g. empty relationship set).
- Production metrics must not require sending full snapshots to third parties unless explicitly configured.

#### **3.13. Temporal provenance, runs, and schema-change lineage**

**Problem:** Consumers need to know **when** an analysis was produced, **what physical schema** it reflected, and **what changed** after a database or mapping evolves — especially for **ontology update cycles** driven by graph schema changes.

**AOE reference:** `arango-ontoextract` uses **extraction run** documents (`started_at`, `completed_at`, `model`, `prompt_version`, stats), **temporal versioning** on ontology entities (`created` / `expired` intervals), **point-in-time snapshot** APIs, and **temporal diff** between timestamps (PRD §5.3, §6.5, data model for `extraction_runs`, `quality_history`).

**3.13.1. Minimum viable (target for schema-analyzer)**

> Status: shape + counts fingerprints and snapshot fingerprint shipped in
> 0.3.0; see CHANGELOG. Run id, ISO timestamps, and provider/model lineage
> are emitted via `metadata`. The 3.13.3 change-state contract, stats-only
> refresh, and diff all shipped (change-state + refresh via
> `schema_analyzer.incremental` / `analyze_incremental`; diff via
> `diff_analyses`). Element-level provenance (3.13.2) shipped.

| Element | Requirement |
|---|---|
| **Analysis run identity** | Each `analyze` produces a stable **run id** (UUID) and records **ISO-8601 timestamps** for start/end (or single `completed_at` if synchronous). |
| **Physical schema linkage** | Persist **snapshot fingerprint** (existing SHA-256, §4.1) and **library + contract version** on the result object. |
| **LLM lineage** | When LLM is used: `provider`, `model`, optional **`promptVersion`** / **system prompt hash** (no raw prompt in logs by default). |
| **Reproducibility** | Cache hits remain keyed by fingerprint; run records distinguish “served from cache at time T” vs “fresh LLM at time T”. |

**3.13.2. Element-level provenance (target)**

| Element | Requirement |
|---|---|
| Conceptual entities / relationships | Optional fields: `firstSeenAt`, `lastValidatedAt`, `source` (`baseline` \| `llm` \| `human`) |
| Mapping entries | Same lineage fields where useful for auditing COLLECTION vs LABEL decisions |

> Status: fully shipped. ``source`` tags via ``provenance.annotate_provenance``;
> ``firstSeenAt`` / ``lastValidatedAt`` via ``provenance.stamp_temporal_provenance``
> (stamped on every fresh analysis, restamped as revalidation by the incremental
> ``unchanged`` / ``stats_only`` branches) and ``provenance.carry_forward_first_seen``
> (``analyze_incremental`` full re-analysis carries ``firstSeenAt`` forward for
> elements that survived the schema change).

**3.13.3. Change detection and diff (target)**

Schema-analyzer distinguishes between **physical shape** changes (which invalidate the conceptual schema and physical mapping) and **data-volume** changes (which invalidate only derived statistics). Consumers MUST be able to determine which, if either, has occurred **without** running a full snapshot.

| Capability | Requirement |
|---|---|
| **Shape fingerprint** | Cheap `fingerprint_physical_shape(db)` probe — hashes only the collection set, per-collection type, and per-collection sorted index digests `(type, fields, unique, sparse, vci, deduplicate)` with auto-generated `name` and `id` excluded. Stable under ordinary writes. |
| **Counts fingerprint** | Cheap `fingerprint_physical_counts(db)` probe — shape fingerprint concatenated with per-collection `count()`. Changes whenever either the shape or any collection's row count changes. |
| **Change-state contract** | **Shipped.** `incremental.assess_change_state(db, prior_shape, prior_counts)` derives the four-valued status: `unchanged` (both match), `stats_changed` (shape matches, counts differ), `shape_changed` (shape differs), `no_cache` (no prior fingerprint recorded). Prior runs stamp `metadata.shapeFingerprint` / `countsFingerprint` for the comparison. |
| **Stats-only refresh** | **Shipped.** `incremental.refresh_statistics(db, prior)` (and the `stats_changed` branch of `analyze_incremental`) preserves the cached `conceptual_schema` and `physical_mapping` and recomputes only derived statistics from a minimal `db.collections()` snapshot. Analyzer invocation, OWL regeneration, type-discriminator detection, and sample extraction are skipped; result carries `metadata.incrementalRefresh = "stats_only"`. |
| **Trigger re-analysis** | **Shipped (library policy).** `analyze_incremental` runs a full `analyze_physical_schema` on `shape_changed` / `no_cache`; the `unchanged` branch returns the prior annotated `incrementalRefresh = "unchanged"`. |
| **Diff** | Compare two `AnalysisResult` payloads (or OWL exports): added/removed/changed entities, relationships, and mapping styles — analogous to AOE `get_ontology_diff` but scoped to **schema-derived conceptual models**. |

**Implementation notes (non-normative):**

- The existing `fingerprint_physical_schema(snapshot)` (§4.1) remains the correct key for a full-snapshot cache. The new shape and counts fingerprints are cheap probes intended for "is refreshing worth it?" decisions, not replacements.
- Auto-generated index identifiers (`name`, `id`) MUST NOT contribute to the shape fingerprint; ArangoDB may assign different values to semantically-equivalent indexes across restarts.
- Transient failures on individual collections (e.g. `indexes()` or `count()` raises) MUST degrade gracefully — the fingerprint function contributes a sentinel rather than propagating the exception.
- Both probes accept an optional `exclude_collections` iterable so callers using a database-resident cache can self-exclude their bookkeeping collection and avoid fingerprint self-perturbation.

**3.13.4. Full temporal graph (optional / long-term)**

Full **edge-interval time travel** for every conceptual entity (AOE-style `created`/`expired` on all versions) is **not** required for v0.1. If schema-analyzer outputs are **imported into AOE**, AOE’s temporal layer can own fine-grained history; this PRD still requires **run-level** and **fingerprint-level** provenance here so handoffs are auditable.

**3.13.5. Bitemporal stamping (shipped 0.13.0, 2026-09-12; commissioned 2026-09-11)**

> Commissioned by the CDF unified-architecture paper
> (`contextual-data-fabric/docs/research/unified-ontology-mapping-architecture.md`,
> Q-3 answered by AK: the downstream schema and mapping planes are **bitemporal**).
> Twin requirement for the relational side: `relational-schema-analyzer/docs/DESIGN-ADDENDUM-bitemporal.md`.
> Status: **shipped** (2026-09-12). This is a *recording* requirement; §3.13.4 stands — the
> analyzer still keeps no history and offers no time-travel queries.
> **Implementation**: `provenance.compute_valid_time` / `prior_valid_from`; stamped by
> `analyzer._stamp_metadata` (`observed`, non-incremental path) and the
> `analyze_incremental` / `incremental.refresh_statistics` change-state branches
> (`fingerprint-continuity` on `unchanged` / `stats_changed`, reset to `observed` on
> `shape_changed`). Emitted additively on result `metadata`
> (`transactionTime` / `validTime` / `validTimeSource` / `predecessorFingerprint`) and in
> CSI `provenance` via `csi.build_provenance`; the CSI schema declares the keys as optional
> (`csi/v1/csi.schema.json`). `analysisCompletedAt` / `transactionTime` are now stamped on
> every result.

**Problem:** A downstream temporal store (AOE) will keep every physical-schema version on two intervals — **valid time** (when the definition was true of the database) and **transaction time** (when the store learned it). Transaction time is already stamped here (`analysisStartedAt` / `analysisCompletedAt`, §3.13.1; CSI `provenance.generatedAt`). Valid time is not, and it can only be captured at introspection: nothing downstream can recover when a collection or index actually changed. ArangoDB exposes **no DDL timestamps** — collection and index properties carry none — so the analyzer cannot read a catalog date the way a relational introspector can. What it *can* do is bound valid time from the fingerprints it already computes.

| Element | Requirement |
|---|---|
| **Transaction time is mandatory** | `analysisCompletedAt` MUST be present on every result and on every CSI document (`provenance.generatedAt`); today it is optional. `transactionTime` is emitted as an explicit alias so consumers need not know the analyzer's field names. |
| **Valid time** | Every result and CSI document carries `validTime: {from}` and `validTimeSource`. With no prior run, `validTime.from = analysisCompletedAt` and `validTimeSource = observed`. `validTime.to` is never set by the analyzer; closing a version is the temporal store's job. |
| **Fingerprint-continuity lower bound** | When the caller supplies a prior run's metadata (as `analyze_incremental` already accepts, §3.13.3) and the prior **shape fingerprint** equals the current one, `validTime.from` MUST be carried back to the earliest run in the unbroken chain, with `validTimeSource = fingerprint-continuity`. This is the collection-level analogue of `carry_forward_first_seen` (§3.13.2) and is the analyzer's only source of valid time stronger than observation. A `stats_changed` state (counts moved, shape did not) preserves the chain; a `shape_changed` state resets it to `observed`. |
| **Predecessor linkage** | `predecessorFingerprint` = the prior run's shape fingerprint when a prior run was supplied, so a consumer can link versions without diffing. |
| **Element-level stamps unchanged** | `firstSeenAt` / `lastValidatedAt` (§3.13.2) are transaction-time stamps and remain so. No element-level valid time is required: ArangoDB offers no per-field DDL signal, so collection-level granularity is the honest ceiling. |
| **Emission** | Additive keys in the tool-contract v1 `metadata` and in CSI (`provenance.transactionTime`, `provenance.validTime`, `provenance.validTimeSource`, `provenance.predecessorFingerprint`). v1 consumers are unaffected; the CSI JSON Schema gains the keys as optional in a minor revision (CSI v1.1 in CDF's terms). `generatedAt` stays. |
| **Redaction** | The new keys are timestamps and hashes; §4.3 redaction is unaffected. |

**Acceptance:**

- Fresh analysis, no prior → `validTimeSource == observed`, `validTime.from == analysisCompletedAt`.
- `analyze_incremental` with an equal shape fingerprint dated T0 → `validTime.from == T0`, source `fingerprint-continuity`, `predecessorFingerprint` set; chained across three unchanged runs → still T0.
- `shape_changed` → source resets to `observed`, `predecessorFingerprint` = prior shape fingerprint.
- `validate_csi` passes for documents with and without the new provenance keys.

---

### **4. Non-Functional Requirements**

#### **4.1. Caching**

- Default cache is filesystem-based, keyed by physical schema fingerprint (SHA-256 of normalized snapshot).
- The cache interface (`get` / `set` / `invalidate`) is storage-agnostic; deployments MAY substitute alternate backends (collection-backed, Redis, object store, etc.). Any substitute MUST be tolerant to missing entries, corrupt documents, and stale cache-document schema versions.
- When using a database-resident cache (e.g. an ArangoDB collection in the same database being analyzed), the cache collection MUST be excluded from shape-fingerprint computation (`fingerprint_physical_shape(db, exclude_collections={...})`) to prevent self-invalidation on its own writes.
- Cache documents SHOULD carry a `cache_schema_version` field so loading code can refuse-and-discard documents whose shape it no longer understands.
- Configurable TTL (default 86400s / 24h).
- `generated_at` timestamp excluded from fingerprint for stability.

**Implementation**: `cache.py` (`AnalysisCache` / `FilesystemCache` with `get` / `set` / `invalidate`; `_cache.cache_schema_version` stamped from `defaults.CACHE_SCHEMA_VERSION` on write and refuse-and-discarded on version mismatch — including legacy unversioned entries — at load). Alternate backends, if any, live alongside.

#### **4.2. Determinism**

- All outputs (snapshots, fingerprints, JSON) are deterministically ordered
- `stable_dumps()` with `sort_keys=True` ensures reproducibility
- Collection iteration is sorted alphabetically

#### **4.3. Security**

- API keys accepted via env vars (`passwordEnvVar`, `apiKeyEnvVar`) — preferred over inline secrets
- Secrets are never logged or persisted by the library
- AQL fragments use bind parameters exclusively — no string interpolation of collection names
- **Trust boundary — LLM:** When a provider is configured, **physical schema snapshots** (and optional samples) may be transmitted to the vendor API. The PRD treats this as **customer-configured data egress**; documentation and the tool contract must make that explicit. Redaction modes are **shipped** via `analysisOptions.redaction`: `stripSamples` (drop sampled docs/edges), `maskFieldValues` (opaque data-value tokens), and `maskFieldNames` (opaque `redacted_field_N` field-name tokens **round-tripped** back to real names in the LLM output via `redaction.unmask_field_names`; system fields preserved).
- **Untrusted structured output:** Conceptual and mapping payloads may originate from an LLM. Downstream use (e.g. AQL helpers) assumes **validated** mapping shapes; stricter validation (allowlists for collection names and attribute keys from the snapshot) is encouraged where security goals exceed “best effort.”
- **Filesystem cache:** Cached analysis may contain sensitive schema metadata; deployments should restrict cache directory permissions and disk encryption as appropriate.
- **Operator-side hardening for hosted MCP/RPC surfaces:** Two opt-in environment variables let operators tighten the trust boundary without modifying the contract:
  - `SCHEMA_ANALYZER_ALLOWED_HOSTS` — comma-separated `host[:port]` allowlist for `connection.url` (rejects anything else with `INVALID_ARGUMENT`).
  - `SCHEMA_ANALYZER_CACHE_ROOT` — absolute path; resolved `analysisOptions.cache.directory` values must lie under it, blocking path-traversal writes via the v1 contract.
  Both default to **off** to preserve local CLI ergonomics; turn them on whenever `run_tool` is exposed to non-local callers. See `docs/tool-contract/v1/README.md` for the full trust-model write-up.

#### **4.4. Error Handling**

- All domain errors use `SchemaAnalyzerError` with typed `code` field
- Tool entrypoint catches both `SchemaAnalyzerError` and unexpected `Exception`, returning contract-shaped error responses
- LLM workflow retries transient `PROVIDER_ERROR` failures with exponential backoff

#### **4.5. Configuration**

Tunable defaults are centralized in `defaults.py`:

| Parameter | Default | Description |
|---|---|---|
| `MAX_REPAIR_ATTEMPTS` | 2 | LLM output validation repair attempts |
| `MAX_RETRIES` | 2 | Transient provider error retries |
| `RETRY_BASE_DELAY` | 1.0s | Exponential backoff base |
| `LLM_TEMPERATURE` | 0.0 | Sampling temperature for all providers |
| `ANTHROPIC_MAX_TOKENS` | 4096 | Max tokens for Anthropic/OpenRouter |
| `DEFAULT_TIMEOUT_MS` | 60000 | Analysis timeout |
| `DEFAULT_REVIEW_THRESHOLD` | 0.6 | Confidence below this triggers `review_required` |
| `DEFAULT_CACHE_TTL_SECONDS` | 86400 | Cache time-to-live |
| `SAMPLE_VALUE_TOP_K` | 20 | Max distinct values per candidate type field |

#### **4.6. Testing**

- Unit tests with an 80% coverage gate (`pytest --cov-fail-under=80` in CI)
- Integration tests (opt-in via `RUN_INTEGRATION=1`) against Docker ArangoDB
- Golden snapshot tests for determinism validation
- CI: lint (ruff + mypy), test matrix (Python 3.10–3.13), integration on PRs

#### **4.7. Tool contract fidelity**

Fields in `docs/tool-contract/v1/request.schema.json` **must either be implemented** in `tool.py` / `AgenticSchemaAnalyzer` **or be explicitly marked deferred** in this PRD and in schema descriptions. Implemented: **`connection.verifyTls`** (maps to python-arango `verify_override`), **`analysisOptions.maxRepairAttempts`**, **`llm.systemPrompt`**, **`llm.promptVersion`** (participates in LLM cache key with the effective system prompt), **`domainContext`** (caller-supplied domain priors → `AgenticSchemaAnalyzer.domain_context`), **`analysisOptions.entityStrategy`** (`auto` | `collection`, validated in `tool.py` → `analyze_physical_schema(entity_strategy=…)`; see §3.4/§3.8), **`analysisOptions.detectForeignKeys`** / **`analysisOptions.sampleForeignKeyOverlap`** (booleans → `AgenticSchemaAnalyzer.detect_foreign_keys` / `sample_fk_overlap`; see §3.8/§6.2), **`input.previousAnalysis`** (the pre-existing shared field, also required by `diff`, → `analyze_incremental`; the contract path to §3.13.5 fingerprint-continuity valid time; `--prior-run FILE` on the CLI), and redaction modes (`analysisOptions.redaction`). Drift between schema and code undermines agent workflows that rely on the contract — e.g. these two FK options were read by `tool.py` but absent from the schema, so with `additionalProperties: false` a request setting them was rejected at validation and the feature was unreachable through the contract until the schema declared them.

---

### **5. Architecture**

```
┌──────────────────────────────────────────────────────────┐
│   CLI       │  Tool API (run_tool)  │  MCP stdio server  │
│  (cli.py)   │      (tool.py)        │   (mcp_server.py)  │
├──────────────────────────────────────────────────────────┤
│                  AgenticSchemaAnalyzer                   │
│                      (analyzer.py)                       │
├──────────┬──────────────┬───────────┬────────────────────┤
│ Snapshot │   Workflow   │  Baseline │  Reconciliation    │
│(snapshot)│  (workflow)  │ inference │  + LLM-output      │
│          │              │ (baseline)│  allowlist         │
│          │              │           │  (reconcile.py)    │
├──────────┤              ├───────────┴────────────────────┤
│          │  Providers   │  Detectors:                    │
│          │  (providers/)│  • shardingProfile  (0.5.0)    │
│          │              │  • multitenancy     (0.6.0)    │
│          │              │  • shardFamilies    (0.6.0)    │
│          │              │  • tenantScope      (0.4.0)    │
│          │              │  • statistics, reconcile…      │
├──────────┴──────────────┴────────────────────────────────┤
│   ConceptualSchema │ PhysicalMapping │ Cache + Validation │
│   (conceptual.py)  │  (mapping.py)   │ (cache, validation)│
├──────────────────────────────────────────────────────────┤
│       Exports: docs / cypher / owl turtle                │
│     (docs.py / exports.py / owl_export.py)               │
└──────────────────────────────────────────────────────────┘
```

The CLI, the in-process `run_tool` API, and the MCP stdio server are all
peers that funnel into the same `AgenticSchemaAnalyzer` and the same v1
contract. Operator-side hardening (`SCHEMA_ANALYZER_ALLOWED_HOSTS`,
`SCHEMA_ANALYZER_CACHE_ROOT`) applies uniformly to every entrypoint
because it is enforced inside `tool.py` / `cache.py`, not at the
adapter.

---

### **6. Roadmap**

> Items below were originally scoped post-0.1. Several have shipped since;
> each shipped bullet is annotated with the release that introduced it.
> Unannotated bullets remain future work.
>
> **Shipped in 0.7.0** (annotated inline below): VCI detection +
> `vci`/`vciCandidate` mapping annotation (§6.1/§6.2); RDF-topology (RPT)
> detection + `TRIPLE` mapping-style annotation (§6.1/§6.2); richer OWL —
> `rdfs:subClassOf`, functional/inverse-functional characteristics, observed
> cardinality, `owl:inverseOf`, and JSON-LD export (§6.3); SPARQL export target
> and a Cypher resolution adapter (§6.4); `diff` and `resolve` v1 tool-contract
> operations; element-level `source` provenance and `diff_analyses` (§3.13);
> structural/grounding quality metrics + `healthScore` (§3.12.3); LLM-egress
> redaction (§4.3). **Shipped in 0.8.0**: MCP SSE / streamable-http remote
> transport with bearer-token auth (§3.11); confidence calibration from eval
> feedback (§6.5); SPARQL `datatypeProperties` export (§6.4). **Shipped in
> 0.9.0**: GraphRAG template detection (§6.2, `metadata.graphRag`); gold-standard
> comparison + metric history (§3.12.3); Conceptual Schema Interchange (CSI) v1
> (`to_csi`/`from_csi` + `csi` op) for interop with a forward relational→graph
> tool; named-graph membership annotation (`metadata.graphMembership` +
> per-entry `graphs`) and optional `graphScope` analysis scoping. Still future:
> SPARQL/SQL *query generation* (§6.4) and OWL reasoner integration (§6.3).

**CSI conceptual naming (CDF CC-12) — _shipped_.** The CSI export applies OWL
naming to the **conceptual** layer by default (`to_csi(owl_naming=True)`): entity
names become singular PascalCase, and property and relationship names lowerCamel.
Physical collection/field names are never renamed; each renamed conceptual
property records its stored realization at
`arangoPhysicalMapping.entities.<E>.properties.<name>.field`, so a transpiler can
resolve conceptual → physical names. `validate_csi` enforces the naming contract
unless `naming=False`, and `to_csi` accepts override maps for irregular singulars.
**Implementation**: `csi/naming.py` (`apply_owl_naming`), `csi/__init__.py`
(`to_csi` / `validate_csi`).

#### **6.1. Additional Mapping Styles**
- `TRIPLE` — _Shipped in 0.7.0_ as an additive annotation
  (`physicalMapping[*].triple = {"style":"TRIPLE"}` + `tripleCandidate`) driven
  by RPT detection (`schema_analyzer/rdf_topology.py`); the native style is
  preserved alongside. For RDF-style schemas with `_triples` collections and
  `rdf:type` edges.
- `VCI` — _Shipped in 0.7.0_ as an additive annotation
  (`physicalMapping.relationships[*].vci` + `vciCandidate`,
  `schema_analyzer/vci.py`). For Vertex-Centric Index optimization patterns
  (denormalized properties on edges).

#### **6.2. Enhanced Pattern Detection**
- **Foreign-key inference (relationships carried by scalar attributes).**
  _Shipped._ ArangoDB enforces no referential constraint, so a relationship
  persisted as `Album.ArtistId → Artist._key` is invisible to edge-collection
  introspection. `schema_analyzer/fk_inference.py` (`infer_foreign_keys`) detects
  these: name-convention candidate generation (snake_case / camelCase / `_id`-shaped
  values that name their target collection), type-compatibility gating, composite-key
  folding, dedup, and a per-candidate confidence with an `evidence[]` list, governed
  by `InferenceOptions` (defaults in `defaults.py`). A reference may target any
  **single-column candidate key** of the foreign collection — its `_key` **or** a
  single-column unique-indexed field (a natural key landed from a relational source),
  the latter ranked one notch lower; uniqueness supplies the many-to-one direction, so
  a non-unique field is never a target, and composite unique constraints stay out of the
  single-column path. (Converged with `relational-schema-analyzer`, which targets PK or
  UNIQUE the same way.) Value-containment sampling is
  **off by default**; when enabled it delegates to `ArangoValueSampler`
  (`schema_analyzer/fk_sampler.py`) — one bounded AQL containment probe per candidate
  under a hard `FK_MAX_PROBES` budget, surfacing `metadata.foreignKeyStatus =
  "degraded"` with the count of unprobed candidates rather than truncating silently
  (the §3.4 transparency rule). Detection is deterministic, read-only, and
  LLM-independent; each confirmed reference is emitted as a `FOREIGN_KEY` mapping (§3.3).
- RPT (RDF Topology) detection: `_triples` collections, `rdf:type` edges
- GraphRAG template matching — _Shipped in 0.9.0_ as `metadata.graphRag`
  (`schema_analyzer/graphrag.py`): brand-agnostic detection of text chunks
  (embedding/vector fields) + extracted entities + similarity / mention edges +
  vector indexes, with a low/medium/high confidence and per-entry
  `graphRagRole`. Complements the Autograph product detector.
- **Vertex-Centric Index (VCI) detection.** Identify VCI usage on edge
  collections and surface it as a first-class signal on the physical
  mapping (beyond today's per-index `vci=true` flag). Two complementary
  signals:
  - **Index-level:** edge collections carrying `persistent` indexes
    rooted at `_from` / `_to` plus one or more discriminator fields
    (e.g. `[_from, type]`, `[_to, type, validFrom]`). Report which
    fields participate, whether the index is `unique` / `sparse`, and
    classify the access pattern (`out-edge`, `in-edge`, `both`).

    An index whose trailing fields include an array expansion (`[*]`) is
    not a traversal-safe VCI: a persistent index stores one entry per
    element and a traversal scans all of them, so it returns each edge
    once per element with no error. Such an index is reported with
    `traversalSafe: false`, risk `ARRAY_EXPANSION_DUPLICATES_TRAVERSAL_ROWS`
    and its `[*]` fields under `arrayExpandedFields` (not
    `discriminatorFields`), is named in `metadata.vci.warnings`, does not
    count toward the access pattern (`accessPattern` names only directions
    covered by traversal-safe indexes, and is omitted when there are none),
    and never makes a relationship a `vciCandidate` on its own. It remains
    correct for pattern-match lookups on the edge collection. Integer
    trailing fields (e.g. bitmask label columns) are not flagged, because
    the schema cannot show they are bitmasks. **Implementation:**
    `schema_analyzer/vci.py` (`_index_level_vci`, `detect_vci`);
    `tests/test_vci.py`.
  - **Schema-level:** edge attributes that duplicate properties from
    their endpoint vertices (denormalisation for VCI lookups). Report
    the duplicated field, source vertex collection, and duplication
    fraction. Emit the candidate `VCI` mapping style alongside the
    underlying `DEDICATED_COLLECTION` / `GENERIC_WITH_TYPE` mapping
    so consumers (transpilers, planners) can choose the optimised
    traversal.
- **Sharding-pattern detection.** _Shipped in 0.5.0_ — see
  `schema_analyzer/sharding_profile.py` and CHANGELOG 0.5.0. Inspects
  `db.properties()`,
  per-collection `properties()` (`numberOfShards`, `shardKeys`,
  `shardingStrategy`, `replicationFactor`,
  `distributeShardsLike`, `smartGraphAttribute`, `isSmart`,
  `isDisjoint`, `isSatellite`), and named-graph metadata to classify the
  deployment style and emit a top-level `metadata.shardingProfile`:
  - **`OneShard`** — database-level `sharding == "single"` (or every
    user collection has `numberOfShards == 1` and shares the same
    `distributeShardsLike` leader). Report the leader collection.
  - **`SmartGraph`** — named graph with `isSmart == true` and a
    non-empty `smartGraphAttribute`; vertex collections share the
    smart attribute as their shard key. Report the smart attribute,
    member vertex/edge collections, and the named graph(s) that
    define the smart family.
  - **`DisjointSmartGraph`** — SmartGraph with `isDisjoint == true`
    (tenant-sharding pattern: cross-tenant traversal is forbidden by
    construction). Report the disjoint shard key in addition to the
    smart attribute, and flag the graph as a tenancy boundary so
    multitenancy detection (below) can consume it.
  - **`SatelliteGraph`** — vertex/edge collections with
    `replicationFactor == "satellite"` (or `isSatellite == true`),
    typically used for meta-graph / ontology / reference data that
    must be co-located with every shard. Report which collections are
    satellites and which named graphs reference them.
  - **`Sharded` (default)** — none of the above; standard
    hash-sharded collections. Report `shardKeys` and
    `numberOfShards` per collection.
  Detection must be read-only and must not require admin privileges
  beyond what `snapshot_physical_schema` already needs; missing fields
  (older ArangoDB versions, restricted users) degrade to `Sharded`
  with a `metadata.shardingProfileStatus` of `"degraded"` and a
  human-readable reason.
- **Multitenancy detection.** _Shipped in 0.6.0_ as
  `metadata.multitenancy` — see `schema_analyzer/multitenancy.py`.
  Companion `physicalMapping.entities[*].tenantScope` annotator +
  `metadata.tenantScopeReport` shipped in 0.4.0
  (`schema_analyzer/tenant_scope.py`). Determines whether the physical
  schema
  encodes multitenancy and, if so, how. Emit
  `metadata.multitenancy` with:
  - `style` ∈ {`none`, `disjoint_smartgraph`, `shard_key`,
    `discriminator_field`, `collection_per_tenant`,
    `database_per_tenant`}
  - `tenantKey` — the property name(s) that identify a tenant
    (e.g. `tenantId`, `org_id`, `accountId`)
  - `tenantKeyCollections` — collections in which the tenant key
    appears, with coverage (`fraction` of documents that carry the
    key) and cardinality (distinct tenant value count, capped)
  - `physicalEnforcement` — whether tenancy is enforced by the
    physical layout (`disjoint_smartgraph`, `shard_key`,
    `collection_per_tenant`, `database_per_tenant`) or only by
    convention (`discriminator_field`)
  - `evidence` — the signals that drove the classification
    (e.g. shard-key match, smart-attribute match, naming pattern,
    high-coverage discriminator)
  Detection layers on the sharding profile above:
  - **Disjoint SmartGraph multitenancy.** When
    `shardingProfile.style == "DisjointSmartGraph"`, the smart /
    disjoint attribute is the tenant key and `physicalEnforcement` is
    `true`. This is the canonical ArangoDB tenant-sharding pattern.
  - **Shard-key multitenancy.** When non-disjoint collections share a
    `shardKeys` value such as `["tenantId"]` (or
    `distributeShardsLike` follows a leader that does), report the
    shard key as the tenant key with `physicalEnforcement = true`.
  - **Discriminator-field multitenancy.** When a property such as
    `tenantId` / `org_id` / `accountId` appears with high coverage
    (`≥ MIN_TENANT_FIELD_COVERAGE_FRACTION`) across many collections
    but is not a shard key, classify as `discriminator_field` with
    `physicalEnforcement = false` and warn that tenancy is convention
    only.
  - **Collection-per-tenant.** When collection naming follows a
    repeated `<base>__<tenant>` / `<tenant>_<base>` pattern across a
    consistent set of bases, report the inferred base set, the
    extracted tenant identifiers, and `physicalEnforcement = true`.
  - **Database-per-tenant.** Out of scope for a single-database
    snapshot, but the analyzer should mark the result as
    `style: "unknown_single_db"` when the snapshot scope is a single
    database that itself looks tenant-named (e.g. matches a known
    tenant naming pattern), so a higher-level orchestrator can
    aggregate across databases.
  Tunables (e.g. `MIN_TENANT_FIELD_COVERAGE_FRACTION`,
  `MAX_TENANT_DISTINCT_VALUES`) live in `defaults.py`. Detection must
  be deterministic and must not depend on LLM output, though the LLM
  layer may enrich the human-readable description.
- **Shard-family detection.** _Shipped in 0.6.0_ as
  `physicalMapping.shardFamilies` — see
  `schema_analyzer/shard_families.py`. Some schemas encode a per-source /
  per-repository / per-stream dimension by duplicating structurally
  identical collections keyed on that dimension (e.g.
  `IBEX_Documents`, `MAROCCHINO_Documents`, `MOR1KX_Documents`,
  `OR1200_Documents` — four collections, one logical entity from a
  consumer's perspective). Emit
  `physicalMapping.shardFamilies[]` grouping these together:
  - `name` — family label derived from the common suffix (e.g.
    `"Document"`).
  - `suffix` — the common name suffix (`"Document"`,
    `"_Golden_Entities"`, …), ≥ `MIN_SHARD_FAMILY_SUFFIX_LEN`
    characters, ending on a capital-letter boundary.
  - `members[]` — one entry per member with
    `{entity, collectionName, discriminatorValue}`.
  - `sharedProperties[]` — the property set common to all members.
  - `discriminator` — `{source: "field", field: "<name>"}` when a
    matching field (default `repo`, configurable via
    `SHARD_FAMILY_DISCRIMINATOR_FIELDS`) exists on the member
    collections; `{source: "collection_prefix"}` otherwise.

  Detection is deterministic: bucket conceptual entities by the hash
  of their sorted property names; within each bucket, confirm a
  common suffix of at least `MIN_SHARD_FAMILY_SUFFIX_LEN`. Buckets of
  size < `MIN_SHARD_FAMILY_SIZE` are skipped. This is explicitly
  **not** multi-tenancy (structural duplication, not per-customer
  isolation) and layers independently of `metadata.multitenancy`.
  Downstream consumers (e.g. NL→Cypher prompt builders) use the
  family grouping to emit UNION-aware guidance instead of silently
  picking one member. Tunables
  (`MIN_SHARD_FAMILY_SIZE`,
  `MIN_SHARD_FAMILY_SUFFIX_LEN`,
  `SHARD_FAMILY_DISCRIMINATOR_FIELDS`) live in `defaults.py`.

#### **6.3. Richer OWL Support**
- Class hierarchies (`rdfs:subClassOf`):
  - **export** (Turtle + JSON-LD) — _Shipped in 0.7.0_ (`owl_export.py::_subclass_edges`, sourced from shard families).
  - **discovery** — _Shipped_ via `schema_analyzer/taxonomy.py`, delegated to the
    paradigm-neutral `conceptual-taxonomy` library (optional dependency; absent → no
    abstraction discovery, not failure). The adapter supplies the two ArangoDB-specific
    inputs — discriminator enumerations from `LABEL` mappings (`build_discriminators`)
    and `_key`-containment ratios measured against the live database
    (`measure_key_containment`) — and folds returned proposals in additively
    (`merge_into_analysis`), synthesizing abstract classes that carry no
    `physicalMapping` entry by design. Shard-family members are excluded so structural
    duplication is not mistaken for subsumption. Containment measurement is opt-in for
    the same cross-collection cost reason FK probing is (`metadata.taxonomyStatus`).
- Property characteristics (`owl:functional`, `owl:inverseOf`)
- Cardinality constraints
- JSON-LD export format alongside Turtle
- Integration with OWL reasoners for consistency checking

#### **6.4. Transpiler Integration**

**Direction (decided): adapter, not query engine.** This library produces a
stable **mapping contract** that external transpilers consume to generate AQL;
it does not host a query parser/translator. See
`docs/transpiler-integration.md` for the author-facing contract.

- Direct integration with `arango-cypher` to consume mapping output — supported
  via `build_cypher_resolution_index` (the `resolve` op) and the raw `cypher`
  export.
- **SPARQL mapping contract** — _Shipped / hardened in 0.8.0._ The `sparql`
  export emits classes, object properties (with domains/ranges), and
  **datatype properties** for entity literal attributes, each annotated with the
  physical resolution, so a SPARQL→AQL transpiler can resolve type, relationship,
  **and literal** triple patterns. SPARQL→AQL *query translation itself* remains
  out of scope for this library (lives in the consuming transpiler).
- SQL mapping for relational query translation — future; would add a `sql`
  export target (relational view) on the same adapter principle. See §6.4.1 for
  the planned design.

**6.4.1. SQL / relational-view export (planned)**

**Purpose.** Emit a relational projection of the conceptual + physical model so a
SQL-oriented consumer (BI tooling, or a **SQL→AQL** transpiler) can resolve
relational query patterns against the graph. Adapter only — like the `cypher` /
`sparql` targets it produces a *mapping contract*, not executed queries.

**Mapping rules.**

- **Entity (COLLECTION style)** → one base table/view named after the collection;
  primary key `_key`; columns are the entity's literal/datatype properties.
- **Entity (LABEL style — multiple labels sharing a collection)** → one view per
  label, filtered by the type discriminator (`typeField == value`); columns are
  that label's observed fields. The discriminator filter is carried in the
  physical resolution so the projection is reproducible.
- **Literal properties** → columns with an inferred SQL type (JSON/AQL scalar →
  SQL type; heterogeneous / unknown → `TEXT` or `JSON`).
- **Relationship (edge collection)** →
  - default **many-to-many** → a junction table with `from_key` / `to_key`
    foreign keys plus columns for any edge properties;
  - a **1:N** shape is emitted as an optional FK column on the child table *only*
    when backed by a uniqueness signal (unique index on an endpoint); otherwise
    the junction table is used. FKs are never assumed without evidence.
- **Endpoints** are resolved with the existing edge-endpoint entity-type
  resolution; FK targets reference the resolved entity tables.
- Every emitted object carries an `arangoResolution` block (collection, style,
  discriminator/filter, AQL snippet) mirroring the other exports, so downstream
  translation stays deterministic.

**Output shape (illustrative).**

```json
{
  "tables": [
    {
      "name": "users",
      "entity": "User",
      "primaryKey": "_key",
      "columns": [{ "name": "email", "sqlType": "TEXT", "conceptualProperty": "email", "nullable": true }],
      "arangoResolution": { "collectionName": "users", "style": "COLLECTION" }
    }
  ],
  "foreignKeys": [
    { "table": "orders", "columns": ["user_key"], "referencesTable": "users", "referencesColumns": ["_key"], "viaEdgeCollection": "placed_by" }
  ],
  "junctionTables": [
    {
      "name": "follows",
      "relationship": "FOLLOWS",
      "fromColumn": "from_key",
      "toColumn": "to_key",
      "properties": [{ "name": "since", "sqlType": "TIMESTAMP" }],
      "arangoResolution": { "collectionName": "follows", "style": "COLLECTION" }
    }
  ]
}
```

**Contract surface.**

- `export_mapping(analysis, target="sql")` returns the structure above.
- Tool contract: add `"sql"` to the export `format` enum and a `SqlExportResult`
  definition in `response.schema.json`.
- CLI: `export --target sql`.

**Open decisions (resolve before implementing).**

- Table/view naming when several labels share a collection (prefix with the
  collection name to avoid collisions?).
- Type-inference source (observed fields vs. sampled values) and the default for
  heterogeneous fields.
- Whether to also emit DDL (`CREATE TABLE …`) text alongside the structured JSON.
- FK-inference threshold (which uniqueness signals justify a FK vs. a junction).

**Effort / gating.** Moderate, self-contained, no new runtime dependencies.
Deliberately **gated on a concrete SQL consumer** — not built speculatively.

#### **6.5. Advanced Features**
- **Schema evolution and lineage** — See §3.13 (run records, fingerprint linkage, diff between analyses, stale detection). Optional alignment with AOE temporal imports when analyses are promoted into `arango-ontoextract`.
- **Quality metrics expansion** — See §3.12.3 (structural ontology metrics, optional gold recall, health score, metric history).
- **Confidence calibration from eval feedback loops.** _Shipped in 0.8.0._
  `schema_analyzer/eval/calibration.py` pairs
  `metadata.confidence` with realized eval quality to emit a reliability curve,
  ECE / MCE / Brier, an overconfidence gap, and a
  `recommended_review_threshold` (Youden's J on the review gate). Surfaced via
  the `eval` CLI and `compare_reports` drift section; deterministic and
  DB-free so it runs on any saved eval report.
- ~~Streaming/incremental analysis for large databases~~ _Shipped_ as the
  §3.13.3 change-state contract + stats-only refresh
  (`schema_analyzer.incremental`, `AgenticSchemaAnalyzer.analyze_incremental`).
- Custom provider SDK support beyond OpenAI/Anthropic/OpenRouter
- ~~**MCP packaging** — Standalone MCP server or module as in §3.11~~ _Shipped in 0.4.0_ as the `arangodb-schema-analyzer-mcp` stdio server (`[mcp]` extra; see §3.11).

#### **6.6. Non-goals (decided out of scope)**

Deliberate boundary decisions, recorded here so they are **not mistaken for
pending roadmap work**. Revisit only via an explicit scope change.

- **Query generation / translation (SPARQL→AQL, Cypher→AQL, SQL→AQL).** Decided
  in §6.4: this library is an **adapter, not a query engine**. It emits stable
  mapping contracts (the `cypher` / `sparql` exports and the resolution index)
  that the consuming transpilers (`arango-cypher-py`, `arango-sparql-py`) use to
  generate AQL. A query parser/translator will not live here. (Note: this is
  distinct from the planned **`sql` mapping export** in §6.4.1, which is a
  contract, not a translator.)
- **Full temporal graph / edge-interval time-travel (§3.13.4).** The library
  provides **run-level and fingerprint-level** provenance (run IDs, ISO
  timestamps, snapshot/shape/counts fingerprints, `diff_analyses`, element
  `source` tags) and stops there. Fine-grained bitemporal history
  (`created`/`expired` on every element, "as-of" reconstruction) is owned by
  AOE's temporal layer when analyses are promoted into `arango-ontoextract`;
  duplicating it here is out of scope.

---

### **7. Dependencies**

#### Core
- `python-arango ≥ 8.1.1` — ArangoDB client
- `pydantic ≥ 2.6.0` — data validation and metadata models
- `jsonschema ≥ 4.21.0` — tool contract validation

#### Optional
- `openai ≥ 1.0.0` — OpenAI provider (`[openai]` extra)
- `anthropic ≥ 0.25.0` — Anthropic provider (`[anthropic]` extra)
- OpenRouter — uses stdlib `urllib`; no extra SDK required (the `[openrouter]`
  extra exists only for feature discoverability)
- `mcp ≥ 1.2.0` — MCP stdio server (`[mcp]` extra; enables
  `arangodb-schema-analyzer-mcp`)
- `conceptual-taxonomy` — class-abstraction discovery (§6.3); when absent the
  taxonomy discovery pass is skipped (no failure), leaving OWL export unaffected

#### Dev
- `pytest`, `pytest-cov`, `ruff`, `mypy`
