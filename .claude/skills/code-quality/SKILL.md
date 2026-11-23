---
name: code-quality
description: Audit arango-schema-analyzer (the `schema_analyzer` package) for unnecessary complexity, duplicated logic, and reuse misses — a simplicity/necessity-ladder review, distinct from pr-review's correctness and mergeability focus. Use for a whole-repo audit or a diff/PR-scoped quality pass, or when asked whether something "needs to be this complicated" or could reuse existing logic.
---

# Code quality (arango-schema-analyzer)

## Invocation
`/code-quality audit` — scan the whole `schema_analyzer` package for
unnecessary complexity, duplicated logic, and reuse misses.
`/code-quality review <diff | branch | PR number>` — same lens, scoped to a
change instead of the whole repo.

## Purpose
`pr-review` asks "is this safe to merge": mergeability, the CI gate, and claims
verified by running them. This skill asks a different question: "does this need
to exist, and is it the simplest form that works." It is a simplicity and reuse
audit, modelled on the necessity ladder from the `ponytail` skill
(github.com/DietrichGebert/ponytail) and on arango-query-core's `code-quality`
skill, adapted to this package. Run it alongside `pr-review`, not instead of
it: a change can be safe to merge and still be more complicated than it needs
to be.

## The necessity ladder
For any new or existing piece of logic, check in this order and stop at the
first rung that holds:

1. Does this need to exist at all?
2. Is equivalent logic already somewhere in this codebase? Grep for the
   concept before writing it.
3. Could the standard library do it?
4. Is there an already-installed dependency that does it (`python-arango`,
   `pydantic`, `jsonschema`)?
5. Can it collapse to one line?
6. Is it the minimum that works?

A reuse chain worth following: the AQL for an entity or a relationship is
built in one place, `PhysicalMapping.aql_entity_match` /
`aql_relationship_traversal` (`mapping.py`). `build_cypher_resolution_index`
(`exports.py`) calls them instead of composing its own AQL, and the fields each
export carries come from one tuple, `_ENTITY_RESOLUTION_KEYS`. When a new
mapping style was added (`LABELS`), the AQL changed in one function and every
export followed. That is rung 2 working as intended.

## Never cut
Do not flag or suggest simplifying away any of these, even when they read as
"extra" code for what the rest of the function does:

- **LLM-egress redaction** (`redaction.py`): every masking pass, including the
  ones that look redundant. Two consecutive reviews (#47, #55) found leaks
  where a pass was missing, never where one was surplus.
- **The `run_tool` trust boundary** (`tool.py`, `cache.py`): the host
  allowlist (`SCHEMA_ANALYZER_ALLOWED_HOSTS`), the cache-root confinement
  (`SCHEMA_ANALYZER_CACHE_ROOT`), and request/response schema validation
  (`tool_contract_v1.py`).
- **The MCP bearer-token check** (`mcp_server.py`, constant-time compare).
- **The LLM-output allowlist and reconciliation** (`reconcile.py`): dropping
  collection names the model invented, backfilling ones it omitted.
- **Error shaping**: every path that turns an exception into a contract-shaped
  `{ok: false, error: {code, message}}` instead of letting it escape.

## Deliberate duplicates: do not flag
These look like reuse misses and are not. Merging them would break convergence
with sibling repositories or a published contract:

- **One singularizer, two names.** `utils.singularize` delegates to
  `csi.naming.singularize` (since r2g-arango#30); keep the delegation. The rules
  themselves are shared with relational-schema-analyzer and r2g through the
  vendored known-answers list below.
- **`fk_inference.py`** is a hand port of relational-schema-analyzer's FK
  engine; every divergence is marked `ARANGO:`. Simplify it in both places or
  not at all.
- **`csi/naming_known_answers.py`** is vendored from relational-schema-analyzer
  and must stay identical to it.
- **Schema copies**: `schema_analyzer/tool_contract/v1/*` and
  `schema_analyzer/csi/v1/*` are byte-identical to their `docs/` twins, by test.
- **Mapping-style and field names** shared with arango-query-core,
  relational-schema-analyzer and r2g (`LABELS` / `MULTI_LABEL` field set,
  `FOREIGN_KEY` / `JOIN_TABLE`): a local rename is a fork.

## Whole-repo audit protocol (`/code-quality audit`)
The package is about 17,600 lines in about 65 files, too large to read end to
end in one pass with equal care. Instead:

1. **Map entry points and data flow first.** The paths almost every caller
   depends on: `run_tool` (`tool.py`) and `cli.py` / `mcp_server.py` →
   `AgenticSchemaAnalyzer` (`analyzer.py`) → `snapshot_physical_schema`
   (`snapshot.py`) → baseline inference (`baseline.py`, `type_detection.py`) or
   the LLM loop (`workflow.py`, `reconcile.py`) → enrichment passes
   (`enrichment.py`, `statistics.py`, `fk_inference.py`, `taxonomy.py`, …) →
   exports (`mapping.py`, `exports.py`, `owl_export.py`, `csi/`, `docs.py`).
   Edge plumbing: `eval/`, `providers/`, `metric_history.py`, `defaults.py`.
2. **Read the hot path in full** (the modules in step 1 before "Edge
   plumbing"), skim the rest, skip `tests/` and `docs/`.
3. **Grep for repeated concepts across modules**, which is where reuse misses
   hide in a package this size: type-field detection and discriminator values,
   collection-name → type inference, walking `sample_documents` /
   `sample_edges`, value-count handling, AQL execution and error handling,
   IRI / local-name handling, and the per-detector "read snapshot entry, build
   metadata block" pattern.
4. **For each candidate, apply the ladder** and confirm it with a command
   before reporting it: the two code paths really do the same thing (compare
   behaviour, not just shape), and a "dead" symbol really has zero callers
   across `schema_analyzer/`, `tests/`, and the public exports in
   `__init__.py`.
5. **Rank findings by reach × impact**, not file order: a duplication on the
   `analyzer.py` → `snapshot.py` path outranks one in an eval helper.
6. **Also report genuine positive examples**: reuse chains followed correctly,
   or a deliberate decision not to merge two similar things because it would
   cost something real. This is a comparative audit, not a complaint list.

For speed, split step 3–4 across parallel reviewers by module group (pipeline
core / detectors and enrichment / exports and contract), give each the ladder
and both lists above, and verify every finding they return yourself before it
goes in the report.

## Diff-scoped protocol (`/code-quality review <target>`)
The same ladder, applied only to the lines a diff or PR touches plus the code
they call into, not the whole repo. Use it to assess one PR's quality,
alongside or after a `pr-review` correctness pass.

## Report
For each finding, give:
- the file and line range
- what the code does today
- which rung it should sit on instead, and why
- the command that confirmed it
- its reach/impact ranking

Lead with the highest-reach finding, not the first one found. Then list the
positive examples, then anything checked and deliberately left alone.

## History
(Append one entry per audit: date, what was found, what was fixed and where.)

- **2026-11-23, first whole-repo audit** (main at `c2fd34b` + this skill; three
  parallel reviewers, every finding below re-confirmed by command). The first four
  were fixed the same day (next entry). Duplications that have already changed behaviour:
  - `analyzer.py`'s no-LLM path (`_prepare_analysis`) copies `_build_result`'s
    enrichment list and lacks `_apply_tenant_scope`, so the default path returns
    no `tenantScope` / `tenantScopeReport`.
  - `diff._health_score` and `csi._settle_property_mappings` read camelCase
    metadata only, while `normalize_analysis_dict` dumps an `AnalysisResult`
    with snake_case keys; two `_meta_get` copies (`metric_history.py`,
    `csi/__init__.py`) already solve this.
  - `tool.py` uses `x or DEFAULT` for `reviewThreshold` / `cacheTtlSeconds`,
    turning a valid `0` into the default.
  - `incremental.refresh_statistics` recomputes the counts fingerprint without
    `exclude_collections`, so the next run reports `stats_changed`.
  - `owl_export.py` lists `phys:` annotations three times; JSON-LD drops a
    relationship's `typeField` / `typeValue`, and `observedCardinality` is used
    but never declared.
  - `statistics.py` re-dispatches on mapping style instead of using
    `aql_entity_match`, and the two already disagree on `MULTI_LABEL`.
  - `baseline.py` builds its own edge-collection → definition map (last graph
    wins) while `utils.index_edge_definitions_by_collection` (first wins) has
    only test callers.
  Lower: sync/async analyze bodies copied; snapshot phase 5 samples and discards
  when samples aren't kept; `_observed_field_names` ×3 (vci, graphrag,
  rdf_topology); graph vertex/edge sets built four ways; `quality.py`'s gold
  scorer mirrors `eval/scoring.py`; the dead `entityStrategy` check in `tool.py`.
- **2026-11-23, fixed the four behaviour-changing findings**, each with a
  regression test that fails without the fix:
  - No-LLM tenant scope (#58): the three copies of the enrichment-pass list are one
    `analyzer._apply_snapshot_passes`, called by the no-LLM, LLM and fallback
    paths; the individual `_apply_*` stay re-exported from `analyzer`.
  - Model metadata keys (#59): `normalize_analysis_dict` dumps a model by alias
    (camelCase, the contract shape), and one `utils.meta_get` replaces the two
    `_meta_get` copies, so a hand-built snake_case dict is still read.
  - Zero-valued options (#60): `reviewThreshold` / `cacheTtlSeconds` use
    `.get(key, DEFAULT)`.
  - Incremental fingerprints (#61): the full re-analysis branch had the same bug as
    `refresh_statistics`; both now keep the `exclude_collections` fingerprints the
    change state was compared with. Closed drift gap REQ-058 (#58 closed REQ-089).
  Still open from the first audit: the OWL annotation lists (JSON-LD drops a
  relationship's `typeField` / `typeValue`; `observedCardinality` undeclared),
  `statistics.py` re-dispatching on style instead of `aql_entity_match`
  (`MULTI_LABEL` disagreement), the unused `utils.index_edge_definitions_by_collection`,
  and the lower-reach items listed above.
