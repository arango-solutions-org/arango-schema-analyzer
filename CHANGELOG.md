# Changelog

## 0.19.0 — 2026-10-30

A minor version: LLM-egress redaction gains a per-field mode. With default options the
prompt and the analysis output are unchanged.

### Added
- **`RedactionOptions.screen_field_values` masks only the fields a caller picks.** The
  caller passes a function `(collection, field, sampled values) -> bool`. It is asked once
  per field that has sampled values; a field it picks has its values masked exactly as
  `mask_field_values` masks every field (the same `<redacted>:N` tokens, also in
  `observed_fields.by_type` keys and edge endpoints), and its values in sample documents
  replaced by `<redacted>`. A nested object or a list is offered under its top-level
  field, with every string and number inside it, and picking it withholds the whole
  value. Every other value still reaches the model, so a caller can
  withhold personal data without hiding the type values the model needs to name entities.
  Python only: a function cannot come from a request, so `from_dict` never sets it.
  `mask_field_values` still masks everything when both are set. Written for
  arango-ontoextract, which passes its personal-data screen.

### Fixed
- **Redaction now covers label co-occurrence.** A label-array collection's
  `label_cooccurrence` block reached the model unmasked under every mode. `mask_field_values`
  and `screen_field_values` now mask its labels with the same tokens as everywhere else, and
  `mask_field_names` masks its field name.

## 0.18.0 — 2026-10-27

A minor version: CSI gains version `"1.1"` (`path` for a value inside a nested object)
and CSI property mappings become strict, and the opt-in `LABELS` entity style is new.
With default options (`nestedFieldDepth` 0, `entityStrategy` `auto`) analysis output is
unchanged, and `to_csi` still writes `"1"`. The one default-path change: `to_csi`
renames a property-mapping key outside the strict list to `x-<key>` instead of passing
it through. arango-sparql-py's `[analyzer]` extra still admits ASA only below 0.16.0.

### Added
- **`entityStrategy: "labels"` maps label arrays to a `LABELS` entity style.** Opt-in
  (`analysisOptions.entityStrategy` / Python `entity_strategy="labels"`); behaves as `auto`
  except that a collection whose type field holds label arrays, with sampled nodes carrying
  more than one label, maps each primary label to
  `{"style": "LABELS", "collectionName", "labelsField", "labelValue"}`, matched as
  `labelValue IN doc[labelsField]`, so a node with several labels matches each of them.
  Conceptual output is unchanged, and `auto` keeps emitting `LABEL`. `PhysicalMapping.aql_entity_match`
  compiles the style; the response, request and CSI schemas (both copies) admit it. The
  shape is shared with arango-query-core and arango-gql-gateway. `metadata.statistics`
  counts each `LABELS` entity by its own label, not the whole shared collection. The
  SPARQL vocabulary export, the Cypher resolution index and the OWL export (Turtle and
  JSON-LD, as `phys:labelsField` and `phys:labelValue`) carry `labelsField` and
  `labelValue`. The label sits in `labelValue`, not `typeValue`, so `LABELS` and
  arango-query-core's `MULTI_LABEL` share one field set (#36); `labelValue` defaults
  to the entity key.
- **CSI 1.1: `path` for values inside nested objects (#33).** A property mapping's
  `field` stays one literal attribute name (`"address.city"` is a single top-level
  attribute). The new `path` (one name per level, `["address", "city"]`) names a value
  inside a nested object. A mapping carries at most one of the two. This matches the
  reader side settled in arango-sparql-py#84.
  - **Version.** The canonical CSI schema (`csi/v1/csi.schema.json` and its `docs/`
    twin) admits `csiVersion` `"1"` and `"1.1"`, and refuses `path` in a `"1"`
    document. `to_csi` writes `"1.1"` only when some mapping uses `path`, so readers
    that accept only `"1"` keep working after an upgrade. `from_csi` accepts both and
    round-trips `path`.
  - **Strict entity property mappings.** Besides `field` / `path`, an entity property
    mapping may hold only `type`, `indexed`, `unique`, `required`, `role`,
    `sentinelValues`, `numericLike`, `sampleValues`, or an `x-` key. `to_csi` keeps any
    other key (an LLM-written `description`, say) as `x-<key>` rather than drop it.
    The rest of a CSI document keeps the additive rule.
- **The analyzer can look inside nested objects (#33).** `analysisOptions.nestedFieldDepth`
  (1-5, default 0 = off) walks the sampled documents into object-valued fields and
  writes `path` for nested values. A dotted name the sample holds only as a top-level
  attribute stays `field`, and takes no index information, because an ArangoDB index
  on `a.b` means the nested attribute. The same rule also settles LLM-written
  mappings.
  - **Flagged, not guessed.** A collection that holds both shapes under one dotted
    name leaves that name unmapped. The conceptual property is removed too, since a
    reader would otherwise read it by its own name. The name is listed under
    `metadata.nestedFields.conflicts`, and in CSI under
    `provenance.unresolvedFieldLocations`.
  - **Lists of objects** partway along a path are not walked yet; each one is listed
    under `arraysSkipped`.
  - **The depth is recorded on the snapshot**, so it is part of the fingerprint and
    no cached analysis crosses the setting.
  - **Masked for LLM egress.** Nested names are covered by `redaction.maskFieldNames`,
    and per-type keys by `maskFieldValues`.
  - **With depth 0, output is unchanged.**

### Changed
- **The tool contract accepts `path` in property mappings.** In both response-schema
  copies and the LLM-output validator, `field` becomes "`field` or `path`, not both",
  and `metadata.nestedFields` is declared.
- **Exports carry `path`.**
  - **OWL** (Turtle and JSON-LD) writes `phys:attributePath`, a JSON array such as
    `["address", "city"]`, for a nested value, in place of `phys:attributeName`.
    This is the annotation arango-sparql-py#84 reads; without either one, a reader
    would fall back to the property's local name and find nothing. A one-level
    `path` is written as `phys:attributeName`.
  - **The SPARQL vocabulary export** (`export_mapping(target="sparql")`) adds `path`
    to a datatype property that has one.
- **`to_csi` never overwrites an `x-` key.** When a mapping already holds
  `x-description`, an unknown `description` is kept as `x-x-description`.

## 0.17.0 — 2026-10-26

A minor version: the singularizer change below alters output, so consumers adopt it
deliberately. arango-sparql-py's `[analyzer]` extra still admits ASA only below 0.16.0.

### Changed
- **The singularizer consults a dictionary.** The 0.16.0 rules failed wherever nobody had
  listed the word: `cactus` -> `cactu`, `knives` -> `knive`, `analytics` -> `analytic`, and
  `cacti`, `criteria`, `mice` unchanged. `csi.naming.singularize` (and `utils.singularize`,
  which delegates to it) now checks the named lists, then a noun dictionary, then the
  rules for words the dictionary does not know. The dictionary is
  `schema_analyzer/csi/data/noun_lexicon.json.gz`, vendored byte for byte from
  relational-schema-analyzer (generated from LemmInflect 0.2.3's noun lexicon; MIT notice in
  `data/NOTICE-noun-lexicon.md`), so ASA gains no runtime dependency.
  `naming.noun_lexicon()` exposes its SHA-256; r2g's tests check it equals RSA's. A word the
  dictionary lists as its own noun stays as it is (`logistics`, `cactus`), and
  `demographics` -> `demographic` is a named data-modelling decision. **Output change**:
  CSI class names and FK/type-detection matching for collections named with `-ves`,
  classical (`cacti`, `criteria`) or irregular plurals and such nouns. The vendored known
  answers (now 142 pairs plus a `SINGULARS` group) are identical to RSA's.
  (arango-solutions-org/r2g-arango#30)

## 0.16.0 — 2026-10-21

A minor version: the singularizer change below alters output, and arango-sparql-py's
`[analyzer]` extra admits ASA only below 0.16.0, so consumers adopt it deliberately.

### Changed
- **One singularizer, which keeps a `-se` plural's `e` and leaves `-s` singulars alone.**
  ASA had two that disagreed with each other and with relational-schema-analyzer's and
  r2g's: `csi.naming.singularize` (CSI class names) turned `warehouses` into `warehous` and
  `movies` into `movy`; `utils.singularize` (FK inference, type detection, quality checks,
  eval scoring) turned `buses` into `buse` and `statuses` into `statuse`; both turned
  `status` into `statu`, `analysis` into `analysi`, `news` into `new` and `series` into
  `sery`. `csi.naming.singularize` now carries the rules shared with RSA and r2g
  (arango-solutions-org/r2g-arango#30): `-es` is dropped only after a sibilant
  (`addresses`, `boxes`, `batches`) or a singular that already ends in `s` (`statuses` ->
  `status`), otherwise only the `s` goes (`warehouses` -> `warehouse`); `-ses` -> `-sis`
  for `-is` words; `-ies` -> `-y` except `-ie` words; named sets cover invariant words,
  singulars ending in `s` and a short irregular table (`people` -> `person`); only the last
  word of a compound inflects. `utils.singularize` delegates to it. **Output change**: CSI
  class names change for collections named with a `-se` plural (`warehouses` ->
  `Warehouse`, was `Warehous`), an `-s` singular (`status` -> `Status`, was `Statu`) or an
  `-ie` plural (`movies` -> `Movie`); FK inference, type detection, quality checks and eval
  scoring change only for `-s` singulars and their plurals (`statuses`, `buses`, `status`,
  `news`), which `utils.singularize` mangled and `<singular>_id` references to them were
  missed. `apply_owl_naming(overrides=...)` remains the escape hatch.
  `schema_analyzer/csi/naming_known_answers.py` is vendored from RSA (identical data);
  `tests/test_naming_known_answers.py` checks both entry points against it.

### Fixed
- **The eval no longer scores a failed model run as the model's.** When the LLM call failed
  (or the provider had no API key), the analyzer answered with the deterministic baseline and
  `run_eval` scored that output as if the model had produced it; the report dropped
  `metadata.used_baseline` and the error, so nothing showed it. An exception in one run also
  aborted the whole eval. Each run entry now carries `outcome` (`ok`,
  `fell_back_to_baseline` or `error`), `llm_requested`, `used_baseline` and `error` (the
  exception, or the analyzer's fallback warnings); an exception is recorded as an `error`
  run, with null scores, and the eval continues. The report gains a `health` block
  (`attempted`, `ok`, `fell_back`, `errors`, …) and a `summary` block of headline mean
  scores over the `ok` runs only. When more than 5% of runs fell back or errored
  (`EVAL_MAX_UNHEALTHY_RUN_FRACTION`), `summary.scores` is null with a `withheld_reason`
  and `calibration.status` is `"withheld"`, unless `eval --ignore-health` (or
  `build_eval_report(..., ignore_health=True)`) is given. No-LLM baseline runs are `ok`,
  not fallbacks. The printed table gains an Outcome column and a reason line for every run
  that is not `ok`. The regression gate (`report_regressions`) also fails on a run that
  errored, or fell back where the baseline report's run did not; the committed no-LLM
  baseline, which predates outcomes, still loads and gates (a missing `outcome` reads as
  `ok`). Existing report fields keep their names.
- **The LLM result cache is keyed by provider and model.** The key held the schema
  fingerprint, prompt version and system prompt only, so switching `--model` or provider on
  an unchanged schema returned the previous model's cached answer. The model is now
  resolved before the cache lookup and both are part of the key (new helper
  `utils.llm_cache_segment`). Existing LLM cache entries miss once and are rewritten under
  the new key; the no-LLM key is unchanged (the bare schema fingerprint).
- **A failed LLM run is no longer cached as the model's answer.** When the LLM failed and
  the analyzer answered with the baseline, that answer was cached under the LLM key, so
  every later run replayed it (as a cache hit) until the TTL expired and the model was never
  tried again. Fallbacks are now not cached (sync and async paths); a successful LLM answer
  and the no-LLM baseline are cached as before. Fallbacks cached by earlier releases can be
  told apart (their `metadata.used_baseline` is true under an LLM key) but cannot be reached:
  the key change above means no current lookup matches them, and they age out with their
  TTL. Should a lookup under an LLM key ever find one, it is treated as a miss and the
  model is asked again.

## 0.15.2 — 2026-10-21

### Fixed
- **VCI detection no longer advertises an array-expanded edge index as a vertex-centric
  index.** `detect_vci` treated every persistent index rooted at `_from` / `_to` as a good
  VCI, so `[_from, type, toLabels[*]]` was reported with `toLabels[*]` as a discriminator
  and could alone make a relationship a `vciCandidate`. A persistent index stores one entry
  per array element, and a traversal scans all of them, so a traversal using that index
  returns each edge once per label -- inflated counts, no error (measured on ArangoDB
  3.12.9 at 20 labels per edge: 20x the rows at one hop, 400x at two). Such an index is now
  still reported but marked `traversalSafe: false` with `risk:
  "ARRAY_EXPANSION_DUPLICATES_TRAVERSAL_ROWS"`, its `[*]` fields move from
  `discriminatorFields` to `arrayExpandedFields`, `metadata.vci.warnings` names it, and it
  never makes a relationship a `vciCandidate` on its own. `indexLevel.accessPattern` now
  counts only traversal-safe indexes (and is omitted when there are none), so a risky
  `[_to, …, fromLabels[*]]` index no longer turns `out-edge` into `both`. Output for indexes without `[*]`
  fields is unchanged. r2g provisions this index shape when `LpgLayout.index_edge_labels` is
  on; it remains correct for pattern-match lookups on the edge collection, which the
  warning says.

## 0.15.1 — 2026-10-16

### Fixed
- **The OWL export now declares the entities' fields** (`owl:DatatypeProperty`, issue #13).
  `export_conceptual_model_as_owl_turtle` and `export_conceptual_model_as_jsonld` emitted classes
  and object properties but no field at all — on prod.demo `IAM`, 1,576 analyzed properties
  became 0 datatype properties, so OWL consumers (arango-sparql-py's NL prompt and ontology
  editor, Protégé, CDF) saw a schema without attributes. Each entity property is now one
  datatype property: `rdfs:domain` is its class, or an `owl:unionOf` of the classes that share
  the name (repeated `rdfs:domain` triples would mean their *intersection*); `rdfs:range xsd:*`
  only from an inferred type all those classes agree on — the baseline's default `"string"` is
  not evidence of a range and is not asserted; `phys:attributeName` (newly declared) records the
  stored field when it differs from the IRI local name (a renamed or sanitized field). A name
  already used by a class or relationship, or two names that sanitize to the same IRI, are left
  out rather than punned or merged. Relationship (edge) properties are not exported yet.

## 0.15.0 — 2026-10-13

### Added
- **Declared attribute references for document stores** (`declared_references`, issue
  ArthurKeen/arango-schema-analyzer#27). A caller can declare a reference the analyzer cannot
  see or should not have to guess — `chunks.document_id -> documents._key` — through
  `apply_reference_overlay(...)`, `AgenticSchemaAnalyzer(declared_references=...)`,
  `analysisOptions.declaredReferences` on the tool contract, or `analyze --declared-references
  FILE` on the CLI. The overlay is the shape contextual-data-fabric already carries
  (`{"references": [{fromCollection, attribute, toCollection, toAttribute}]}`). Each declaration
  is verified against the live database by full containment — every non-null value must
  resolve, compared strictly, with numeric ids cast to match string `_key`s — and merged as a
  `FOREIGN_KEY` relationship with `method: "declared"`, `confidence: 1.0` and conceptual
  `source: "declared"`. A declaration outranks an inferred relationship for the same reference
  and keeps its name. One that does not hold, names an unmapped collection, or has no non-null
  values **fails the analysis** with `INVALID_ARGUMENT` and the reason (e.g. `20/21 non-null
  values resolve`), and nothing is merged. Status lands in `metadata.declaredReferenceStatus`.
- **Secondary labels are classified, not just collected.** For each primary label, the
  snapshot records which other labels ride on its nodes (`label_cooccurrence`). A label on
  *every* node of a primary is an alias and joins the entity's conceptual `labels` — matching
  r2g's forward CSI, so a forward→reverse round trip is lossless. A label on *some* nodes of
  exactly one primary is a subtype: recorded as `partialLabels` and proposed as
  `rdfs:subClassOf` (`method: "secondary_label"`, with its share as evidence). A label that
  rides on **two or more different primaries** cuts across the types — a provenance tag
  (`SourceCRM`), tenant or status — and is recorded as `facetLabels`, never as a subtype. A
  label that is itself some node's primary label is not proposed as a subtype of another.
  Taxonomy discovery now merges its proposals with these instead of replacing them.

### Fixed
- **A real `analyze` result can be fed back into the contract.** The request schema's copy of
  `AnalysisOutput` closed `physicalMapping` without `shardFamilies` — which every analysis
  carries, even as `[]` — and closed `conceptualSchema` without `abstractClasses` /
  `subClassOfProposals`. So no real analysis was accepted as `input.analysis` (`docs`, `owl`,
  `export`, `csi`, `resolve`, `diff`) or as `input.previousAnalysis`: the request failed with
  `INVALID_REQUEST "('shardFamilies' was unexpected)"`. That broke the §3.13.5 prior-run path
  (incremental analysis and fingerprint-continuity valid time), `analyze --prior-run`, and the
  CLI `docs` / `owl` subcommands against any real database; their tests used hand-built
  analyses that omitted the key. All three are now declared (both schema copies), keeping the
  request side's input-size caps, and a live integration test feeds a real result back through
  every operation that takes one.
- **Attribute-carried relationships now export valid CSI.** CSI's physical relationship `style`
  admits only edge-backed styles, so an analysis containing a `FOREIGN_KEY` relationship (from
  `detect_foreign_keys`) produced a document that failed `validate_csi`. `to_csi` now carries
  such a relationship on the conceptual layer only, with its join under `reference`
  (`fromCollection`, `fromFields`, `toCollection`, `toKeyFields`, `confidence`, `method`,
  `evidence`, and `origin` = `declared` | `inferred`); `from_csi` restores the physical mapping,
  so the round trip is lossless. A reference with no conceptual relationship is listed in
  `provenance.unattributedReferences` rather than dropped. No CSI schema change.
- **`detectForeignKeys` works through the tool contract.** The response schema did not admit the
  `FOREIGN_KEY` relationship style the option emits, so whenever it found a reference the request
  failed response validation and returned `INTERNAL_ERROR`. Found only by running against a live
  ArangoDB; the response schema (both copies) now declares the style and its fields, and a live
  integration test pins it.
- **Label arrays now discriminate entities in LPG graphs** (issue
  ArthurKeen/arango-schema-analyzer#28). r2g's LPG layout puts every node in one collection
  with `labels: ["Account", "Customer"]`. The type-field query grouped by the whole array, so
  every value was a list, no discriminator check accepted it, and the collection collapsed to
  one entity (`Node`), with every relationship `Node -> Node`. An array-valued type field now
  groups by its **primary** (first) label — the node's identity, which r2g builds the `_key`
  namespace and CSI `typeValue` on — so the graph reverses into `LABEL` entities
  (`typeField: "labels"`, `typeValue: "Account"`). The same applies to per-type property
  sampling, endpoint resolution through the node's labels, and r2g's edge-carried
  `fromLabels` / `toLabels`. Scalar type fields are unchanged.
- **`discoverTaxonomy` works through the tool contract.** The response schema's
  `conceptualSchema` was closed and did not declare `abstractClasses` / `subClassOfProposals`,
  so any taxonomy result failed response validation and returned `INTERNAL_ERROR`. Both are
  now declared (both schema copies).

## 0.14.3 — 2026-10-12

### Fixed
- **A reference whose keys differ only in case or padding is no longer deleted by FK
  inference.** The value-overlap probe compared with AQL `==`, which is case- and
  whitespace-sensitive, so a key written `CUST1` in one collection and `cust1` (or
  `" CUST1"`) in another scored zero overlap — and zero is the hard veto, so the genuine
  relationship was removed rather than left unconfirmed. The probe now runs strict first
  and, only when the strict result leaves the outcome open (below the 0.9 mark where the
  confidence bump saturates), a second probe on `LOWER(TRIM(TO_STRING(...)))`. A rescued
  candidate's evidence says `(matched after normalizing case/whitespace)`, and the score is
  an `Overlap` float carrying a `normalized` flag, so no consumer changes. The score can only
  rise, so this can newly accept a candidate but never newly veto one. The normalized probe
  cannot use an index and spends a unit of probe budget; when the budget is gone the strict
  answer stands and the run reports `foreignKeyStatus = "degraded"`. Ported from
  relational-schema-analyzer 0.9.2, whose inference core this module mirrors.

## 0.14.2 — 2026-10-05

### Fixed
- **Upper-case collection names are singularized, so their relationships are found.**
  `utils.singularize` matched plural suffixes in lower case only, so `CUSTOMERS`, `ORDERS`
  and `USAGE_METRICS` came back unchanged. FK inference builds a target's name variants
  from it, so a `CUSTOMER_ID` field was never matched to a `CUSTOMERS` collection and the
  relationship was silently missed. Type detection named such a collection's entity in
  the plural (`CUSTOMERS`). Suffixes now match case-insensitively and a replacement letter
  follows the case it replaces (`CATEGORIES` → `CATEGORY`); lower- and mixed-case names
  are unchanged. Affects graphs whose collections keep upper-case names, typically ones
  loaded from Snowflake or Oracle sources. CSI naming already used its own
  case-insensitive `singularize` and is unaffected. Same fix as relational-schema-analyzer
  0.9.0 and r2g 0.4.2.

## 0.14.1 — 2026-10-05

### Fixed
- **An edge field that copies its endpoint collection names is no longer taken for the
  relationship type.** On the IAM demo database every dedicated edge collection
  (`CAN_ACCESS`, `CAN_ASSUME`, `ASSOCIATES`, …) also stores `destination_sub_type`, a copy
  of the target collection's name. 0.14.0 accepted it as the discriminator, mapping
  `CAN_ACCESS` as `GENERIC_WITH_TYPE` into relationships named `aws_ec2_instance`,
  `aws_s3_bucket`, …, so consumers could not express `[:CAN_ACCESS]`. For edge
  collections, a candidate field whose observed values are (≥ 90 % by mass,
  `ENDPOINT_MIRROR_FRACTION`) the names of the collection's own from/to collections is now
  dropped before every acceptance path. Verified on the live IAM database: the `IAM_DEMO`
  mapping now has its 20 real relationship types, each `DEDICATED_COLLECTION`. A genuine
  relation field next to such a mirror is still picked, and without endpoint data the
  rule does nothing.

## 0.14.0 — 2026-09-14

### Changed
- **LPG type detection converged on the analyzer (CDF unified-architecture paper Q-5,
  step 4).** Three detectors existed — this analyzer's, `arango-ontoextract`'s and
  `arango-cypher-py`'s — and could give three answers to "what is the type field" for one
  database. Ownership was decided as the analyzer's (AK, 2026-09-11); this release ports
  what the other two had and this one lacked, so they can consume the CSI `LABEL` /
  `GENERIC_WITH_TYPE` entries instead of re-detecting:
  - **Tier-1 acceptance on coverage alone.** `type`, `_type`, `entityType`, `@type`,
    `entity_type` (documents) and `type`, `relation`, `relationship`, `relType`,
    `predicate` (edges) are unambiguous type names and are now accepted when broadly
    present, regardless of the distinct-count acceptance bound — so a 40-type graph is
    recognised as LPG at the default bound instead of collapsing to one collection-named
    class. The bound (`MAX_TYPE_FIELD_DISTINCT_VALUES`, scaled by `max_entity_types`)
    still governs tier-2 names (`label`, `category`, `kind`, …), which may equally hold a
    display value. A single observed value is never tier-1 evidence (the dedicated-edge
    `relation = "<collection>"` fallback is unchanged). **Behaviour change:** where the
    0.13 contract rejected a `type` field with more than 32 distinct values at the default
    bound, 0.14 accepts it and maps the sampled top-K values, reporting the rest in
    `entityTypeCaps` / `relationshipTypeCaps` (or all of them under a raised cap /
    full-label-set mode).
  - **Candidate names** `@type`, `entity_type`, `category`, `relationship`, `predicate`
    added to the allow-list and preference order.
  - **Endpoint type fields on edges.** When sampled edges carry `_fromType` / `_toType`
    (or `fromType` / `toType`, `_from_type` / `_to_type`), per-relation entity types are
    resolved from those fields with one `COLLECT` and no `DOCUMENT()` lookups — and even
    when the vertex collections have no detectable discriminator. The snapshot records
    `edge_endpoints.endpoint_type_fields`.

### Fixed
- **Plain attributes no longer split a collection into per-value `LABEL` entities.** A
  broadened (non-allow-listed) field name is probed as a discriminator only when it carries
  a type-like token (`type`, `kind`, `class`, `category`, `label`) — `rel_kind`, `etype`,
  `node_class` still qualify; `priority`, `name`, `mcc`, `amount`, `since`, `ip` and `role`
  (an attribute of an edge, not its type) do not. Found by the Integration eval gate:
  `financial_fraud_detection` predicted 23 entities against 7 gold (`cases.priority` →
  `Priority0..4`, `customers.name` → `Name0..4`, …) and `seen_by.role` became ten relation
  types. The regression dates from 0.3.x, when the reserved-word `COLLECT` fix made
  discriminator queries run on ArangoDB 3.12 for the first time — the July baseline had
  scored a detector that never executed.
- **Eval runner analyses each fixture alone.** `run_eval` now drops every user graph and
  collection before materialising the next fixture. Previously each fixture after the first
  was scored together with its predecessors' collections (13, 18, 24, 28 predicted entities
  against 6 gold, growing in run order), and the committed baseline had been recorded that way.
- **`eval/baselines/ci_no_llm_baseline.json` regenerated** from the corrected runner and
  detector (2026-09-14). Against the 2026-07-17 baseline no metric regresses; entity and
  relationship F1 reach 1.00 on nine of ten fixtures and mapping-style accuracy on the
  generic variants rises from 0.00 to 1.00. Future regressions are measured from here.

## 0.13.1 — 2026-09-14

### Changed
- **CSI `provenance.validTimeSource` admits all five producer values.** The 0.13.0 schema
  enumerated only the two values this analyzer emits (`observed`, `fingerprint-continuity`).
  The relational twin (`relational-schema-analyzer` 0.8.0, `DESIGN-ADDENDUM-bitemporal.md`
  §2.2) also emits `catalog`, `event` and `file`, and `r2g export-csi` validates against
  this schema on write — so a catalog-dated Snowflake schema was rejected by the shared
  contract. The enum is now `catalog | event | file | fingerprint-continuity | observed`,
  strongest to weakest; this analyzer's own emission is unchanged. Converged with r2g 0.4.1,
  which de-vendors this file (prefers the installed analyzer's copy) and tests for drift.
- Dev extras: `conceptual-taxonomy` added (the taxonomy wiring tests require it); `ruff`
  banded to 0.16; `.cursor` excluded from ruff. CI had been red on these since 2026-08-22.

## 0.13.0 — 2026-09-12

### Added
- **Bitemporal stamping (PRD §3.13.5).** Every result and CSI document now carries both
  clocks the downstream temporal store (AOE) needs: **transaction time**
  (`metadata.transactionTime` / CSI `provenance.transactionTime`, an explicit alias of the
  now-always-present `analysisCompletedAt`) and **valid time** (`validTime.from` +
  `validTimeSource`). ArangoDB exposes no DDL timestamps, so `validTime.from` is bounded from
  fingerprint continuity: `observed` (this run's completion) with no prior; carried back to the
  earliest run whose shape fingerprint matches (`fingerprint-continuity`) when a prior run is
  threaded through `analyze_incremental` (`unchanged` / `stats_changed` preserve the chain,
  `shape_changed` resets to `observed`). `predecessorFingerprint` links versions. `validTime.to`
  is never set here (the temporal store closes intervals). Additive and backward-compatible:
  v1 tool-contract consumers are unaffected; the CSI schema gains the keys as optional. New
  `provenance.compute_valid_time` / `prior_valid_from`. Converged with the
  `relational-schema-analyzer` twin. Commissioned by the CDF unified-architecture paper.
- **Prior-run input on the tool contract + CLI.** The pre-existing shared field
  `input.previousAnalysis` (already required by `diff`) now also routes `analyze` through
  `analyze_incremental`, and the CLI exposes `analyze --prior-run FILE` (a saved result or its
  bare analysis). This is how a contract consumer (e.g. CDF) obtains fingerprint-continuity
  valid time — previously reachable only via the Python API. Reuses the existing field rather
  than minting a new one, keeping this project / `relational-schema-analyzer` / CDF converged.
  Per-run bitemporal stamps are now also stripped from the filesystem cache payload (they are
  re-stamped on every cache hit), matching the existing provenance-strip.

## 0.12.1 — 2026-08-21

### Added
- **FK inference can target any single-column candidate key, not only `_key`.** A reference
  whose name resolves a foreign collection *and* names one of its single-column unique-indexed
  fields (e.g. `chunks.document_code → documents.code`, where `code` is unique but not the
  document key) is now proposed — the natural-key case common when data is landed from a
  relational source. Unique-field targets rank one notch below the collection key (−0.05);
  id/key/camel references still target the key only; uniqueness supplies the many-to-one
  direction (a non-unique field is never a target); composite unique constraints stay out of
  the single-column path; the type check is unchanged. Mirrors the equivalent
  `relational-schema-analyzer` fix, keeping the two engines converged.

### Fixed
- **`detectForeignKeys` / `sampleForeignKeyOverlap` were unreachable through the v1 tool
  contract.** `tool.py` read both `analysisOptions` keys, but they were not declared in
  `request.schema.json`, and `analysisOptions` is `additionalProperties: false` — so a request
  that set them was rejected at validation, leaving document-modelled foreign-key inference
  (§6.2) reachable only via the Python API. Declared both options in the request schema (both
  copies), documented them in PRD §3.8/§4.7, and added a parity guard test. The Python default
  (`detect_foreign_keys=False`) is unchanged.

## 0.12.0 — 2026-08-18

### Fixed
- **LPG discriminator detection was inert on ArangoDB 3.12** (fixes downstream REQ-116).
  `snapshot._detect_type_fields_via_collect` built its `COLLECT` query with an **unquoted
  `distinct` projection key** — an AQL reserved word — so ArangoDB 3.12 rejected the query with
  `[ERR 1501] syntax error`. The failure was swallowed by a broad `except`, leaving
  `sample_field_value_counts` empty, so every discriminated collection fell through to
  `PG_ENTITY_COLLECTION` instead of `LPG_LABEL` — the library reported success while producing
  naive one-class-per-collection output. The key is now quoted. Committed in `d485d03` but never
  released: PyPI `0.11.0` predated the fix while repo `0.11.0` contained it; this release ends
  that version collision.

### Changed
- **The swallow that hid the bug is no longer silent.** `_detect_type_fields_via_collect` now
  catches `ArangoServerError` specifically and logs each swallowed query failure — plus a
  per-collection summary — at `WARNING`; a broad fallback still degrades gracefully but is
  logged. A one-character AQL error can no longer masquerade as "this field has no values."

### Added
- **`FOREIGN_KEY` and `JOIN_TABLE` relationship mapping styles** (PRD §3.3) — SQL-style attribute
  references and reified junction collections.
- **Foreign-key inference engine** (`fk_inference.py`) + `ArangoValueSampler` (`fk_sampler.py`),
  ported from `relational-schema-analyzer`; opt-in value-containment sampling under a bounded
  probe budget with `metadata.foreignKeyStatus` (PRD §6.2). FK detection and taxonomy discovery
  are now wired into the analyzer.
- **Conceptual-taxonomy (class-abstraction) discovery** (`taxonomy.py`, PRD §6.3), delegated to
  the optional `conceptual-taxonomy` library.
- **LPG full-label-set mode** (`min_type_value_count`) — retain the complete discriminator
  vocabulary rather than only the top-K (PRD §3.4).
- **CSI CC-12 OWL naming** applied by default in `to_csi` and enforced by `validate_csi`.
- **`analysisOptions.entityStrategy`** (`auto` | `collection`) exposed in the v1 tool contract
  (PRD §3.8), backing the existing Python `entity_strategy` override.
- **Real-ArangoDB regression test** for LPG discriminator detection
  (`tests/integration/test_lpg_discriminator_regression.py`) — asserts `sample_field_value_counts`
  is non-empty and `detectedPatterns` contains `LPG_LABEL`; verified to fail when the reserved-word
  quoting is reverted (a fixture DB never parses the AQL, so it cannot catch this class of bug).

### Infrastructure / docs
- Repository renamed `arango-schema-mapper` → `arango-schema-analyzer`; publish workflow guarded
  to the canonical repo; local `scripts/publish.sh` token-based publish path added.
- PRD documentation for the above (§3.3/§3.4/§6.2/§6.3/§3.8); denormalization-detection proposal.

## 0.11.0 — 2026-07-17

### PRD drift closure (5 gaps from 2026-07-16 prd-sync)

- **Temporal element provenance (PRD §3.13.2).** Every conceptual
  entity/relationship and physical-mapping entry now carries `firstSeenAt` /
  `lastValidatedAt` alongside the existing `source` tag. Fresh analyses stamp
  both; the incremental `unchanged` / `stats_only` branches restamp
  `lastValidatedAt` (fingerprint match = revalidation); a full re-analysis via
  `analyze_incremental` carries `firstSeenAt` forward for elements that
  survived the schema change. New `provenance.stamp_temporal_provenance` /
  `carry_forward_first_seen` helpers.
- **Cache schema versioning + invalidate (PRD §4.1).** Cache payloads stamp
  `_cache.cache_schema_version` (`defaults.CACHE_SCHEMA_VERSION`); loading
  refuses and discards entries with a mismatched or missing version (existing
  caches recompute once). `AnalysisCache` gains the PRD-specified
  `invalidate(fingerprint)` (missing entries are a no-op).
- **CI eval regression gate (PRD §3.12.3).** New
  `eval.report_regressions(current, baseline)` and `schema-analyzer eval
  --fail-on-regression` (exit 1 when a gated quality metric — entity /
  relationship / domain-range F1, mapping-style accuracy — drops more than
  `EVAL_DELTA_THRESHOLD` vs the baseline, or a baseline domain/variant
  disappears; confidence is advisory). The Integration workflow now runs the
  deterministic no-LLM eval against the committed
  `eval/baselines/ci_no_llm_baseline.json`.
- **CI Python matrix (PRD §4.6).** Test matrix extended to Python 3.13.

### Collection-per-entity strategy for pure-PG schemas

- **`entity_strategy="collection"`.** Opt-in override on
  `analyze_physical_schema` (sync + async) that skips type-discriminator
  inference: one entity per document collection (`COLLECTION`), one
  relationship per edge collection (`DEDICATED_COLLECTION`). Deterministic, no
  LLM. Fixes the auto heuristic shattering a pure property-graph schema into
  value-classes when a high-cardinality attribute (e.g. `region`) looks like a
  type tag. Default `"auto"` unchanged.

### Cypher vocabulary fidelity (docs/cypher-vocabulary-fidelity-bug-report.md)

- **Raw discriminator value kept as `aliases`.** `LABEL`-style entity mappings
  whose PascalCase name is lossy (`FIN_METRIC` → `FINMETRIC`) now carry the raw
  type value in `physicalMapping.entities[*].aliases`, flowing through the
  `cypher` export and `build_cypher_resolution_index`, so a Cypher author's
  `:FIN_METRIC` resolves without downstream normalization heuristics.
- **Transparent, configurable entity/relationship type cap.** The snapshot now
  records each candidate discriminator field's true distinct-value total
  (`sample_field_distinct_counts`) plus up to `SAMPLE_VALUE_OVERFLOW_K` (50)
  beyond-top-K values (`sample_field_value_overflow`). Discriminator values
  dropped by top-K sampling are reported in `metadata.entityTypeCaps` /
  `relationshipTypeCaps` (collection, type field, distinct total, dropped
  classes with counts) instead of vanishing silently. New
  `max_entity_types` parameter on `analyze_physical_schema` (sync + async) /
  `sample_value_top_k` on `snapshot_physical_schema` raises the cap; the
  discriminator acceptance bound scales with it. Redaction masks the new
  snapshot keys like their top-K counterparts. `SNAPSHOT_FORMAT_VERSION`
  bumped to 2 (fingerprint-keyed caches recompute once).

## 0.10.0 — 2026-07-04

### Advanced redaction: field-name masking with round-tripping (PRD §4.3)

- **`maskFieldNames` redaction mode.** Third composable LLM-egress redaction
  mode (alongside `stripSamples` / `maskFieldValues`). Replaces document *field
  names* with opaque, name-like `redacted_field_N` tokens before the snapshot is
  sent to the provider — across `candidate_type_fields`,
  `sample_field_value_counts` keys, `observed_fields.fields` / `by_type` value
  lists, `indexes[*].fields`, and sample-document keys — while preserving
  ArangoDB system fields (`_key` / `_from` / `_to` / …) and collection names.
- **Output round-tripping.** `AgenticSchemaAnalyzer` builds a snapshot-wide
  name→token map, applies it to the prompt snapshot, then un-masks the model's
  response (`redaction.unmask_field_names`) so the conceptual schema and physical
  mapping carry the **real** field names. Un-masking is an exact regex match on
  `redacted_field_<int>`, robust to tokens appearing standalone (property names)
  or embedded in prose (descriptions); unknown tokens are left intact. New
  `build_field_name_map` / `unmask_field_names` helpers; `RedactionOptions`
  gains `mask_field_names`. Exposed via `analysisOptions.redaction.maskFieldNames`.

### Caller-supplied domain context (PRD §4.7)

- **`domainContext` request field / `AgenticSchemaAnalyzer(domain_context=...)`.**
  Callers can now supply explicit business-domain priors (a domain-name string,
  or an object `{domain, description, entities, relationships}`) that override
  automatic `detect_domain` detection for the LLM prompt. Optional
  `entities`/`relationships` populate the "typical vocabulary" prompt block.
  Provided context is authoritative (`metadata.detectedDomainConfidence = 1.0`);
  new helper `domain_detect.domain_hint_from_context`. Falls back to auto-detect
  when unset.

### Incremental re-analysis + change-state detection (PRD §3.13.3)

- **Stored change-detection fingerprints.** Every analysis now stamps
  `metadata.shapeFingerprint` (collections + types + index digests) and
  `metadata.countsFingerprint` (shape + per-collection row counts) via the cheap
  `fingerprint_physical_shape` / `fingerprint_physical_counts` probes, so a later
  re-probe can decide whether a refresh is warranted without a full snapshot.
- **`assess_change_state(db, prior_shape, prior_counts)`.** Derives the
  four-valued state: `unchanged`, `stats_changed`, `shape_changed`, `no_cache`.
- **`refresh_statistics(db, prior)`.** Stats-only refresh: preserves the cached
  conceptual schema + physical mapping and recomputes only the statistics block
  (from a minimal `db.collections()` snapshot — no discriminator detection,
  sampling, OWL, or LLM), updating `countsFingerprint` and stamping
  `metadata.incrementalRefresh = "stats_only"`.
- **`AgenticSchemaAnalyzer.analyze_incremental(db, prior=...)`.** Orchestrates
  the above: `shape_changed`/`no_cache` → full analysis; `stats_changed` →
  stats-only refresh; `unchanged` → returns the prior annotated
  `incrementalRefresh = "unchanged"`. New module `schema_analyzer/incremental.py`;
  `assess_change_state` / `refresh_statistics` exported from the package root.

## 0.9.0

### Named-graph membership + scoping

- **Graph membership (default).** New `schema_analyzer/graph_membership.py` labels
  which named graph(s) each entity/relationship belongs to — derived from the
  snapshot's graph definitions — and stamps `metadata.graphMembership`
  (per-graph entity/relationship/collection lists + an `ungraphed` bucket) plus
  a per-entry `physicalMapping[*].graphs` list. Membership is many-to-many.
  Snapshot-only; absent (no-op) when the database has no named graphs.
- **Graph scoping (opt-in).** `snapshot_physical_schema(..., graph_scope=NAME)`
  / `AgenticSchemaAnalyzer.analyze_physical_schema(..., graph_scope=NAME)` /
  `analysisOptions.graphScope` restrict analysis to one named graph's
  collections (edge collections + their from/to vertices + orphans), bounding
  cost / LLM egress / focus. `INVALID_ARGUMENT` if the named graph is missing.
- **Bugfix (graph properties parsing).** `_summarize_graph_props` now accepts
  python-arango's normalized snake_case shape (`edge_definitions` with
  `edge_collection` / `from_vertex_collections` / `to_vertex_collections`,
  `orphan_collections`) in addition to the raw HTTP camelCase shape. Previously
  `graphs_detailed` edge definitions were empty for real databases (masked
  because endpoint resolution falls back to edge sampling) — this also feeds
  sharding-profile and the new membership/scoping features correctly.

### Conceptual Schema Interchange (CSI) v1

- New `schema_analyzer/csi` package defining a **direction-agnostic interchange
  document** so this library and a forward relational→graph tool (e.g. R2G) can
  exchange a conceptual model + ArangoDB physical mapping cleanly. A CSI doc is
  the three blocks `conceptualModel` (≡ `conceptualSchema`),
  `arangoPhysicalMapping` (≡ `physicalMapping`), and a small `provenance`
  envelope (`producer`, `direction` reverse/forward, `source`, `confidence`).
  Detection enrichments ride along as optional additive fields.
  - `to_csi(analysis, *, direction, source, ...)` produces a CSI doc;
    `from_csi(csi)` adapts one back into the `{conceptualSchema, physicalMapping,
    metadata}` shape this library's `diff_analyses` / `compute_gold_comparison` /
    quality / export consumers accept — so an R2G-produced CSI can be audited
    here directly.
  - Standalone JSON Schema shipped in both `docs/csi/v1/csi.schema.json` and the
    bundled `schema_analyzer/csi/v1/csi.schema.json` (kept byte-identical, with a
    parity test); `load_csi_schema_v1()` / `validate_csi()` helpers.
  - New v1 tool operation **`csi`** (`input.analysis` → `result.csi`) exposes it
    over the JSON contract / MCP, alongside `export` / `owl` / `diff` / `resolve`.

### Quality metrics expansion — gold comparison + metric history (PRD §3.12.3)

- **Gold-standard comparison.** New `compute_gold_comparison(conceptual,
  reference)` in `schema_analyzer/quality.py` scores the conceptual schema's
  entities and relationship types (precision/recall/F1, name-normalized) against
  a supplied domain-pack-style reference. When a reference is provided,
  `build_quality_block` adds a `gold` block to `metadata.qualityMetrics` and
  folds its overlap into `healthScore` as a weighted component. Wired through a
  new `AgenticSchemaAnalyzer(gold_reference=...)` constructor arg and the
  `analysisOptions.goldReference` v1 request option.
- **Metric history.** New `schema_analyzer/metric_history.py` distills each run
  into a compact `metric_snapshot` (healthScore, confidence, key structural /
  grounding ratios, gold overlap, run id / fingerprint / provenance) and
  maintains an append-only history with `append_to_history`,
  `summarize_history` (min/max/mean/latest/delta trend lines), and a
  filesystem-backed store (`load_history` / `save_history` / `record_metrics`).
  Pure and DB-free; accepts an `AnalysisResult` or a serialized dict
  (snake_case or camelCase metadata keys).

### GraphRAG template detection (PRD §6.2)

- New `schema_analyzer/graphrag.py` — deterministic, snapshot-only recognition
  of a retrieval-augmented-generation graph: text **chunks** (embedding/vector
  fields or chunk-like names + text), extracted **entities**, **similarity**
  edges, chunk→entity **mention** edges, and vector indexes. Brand-agnostic and
  complementary to the Autograph product detector (`arango_products.py`).
  Emits `metadata.graphRag` (classification + evidence + low/medium/high
  confidence) and tags participating physical-mapping entries with
  `graphRagRole` (`chunk`/`entity`/`similarity`/`mention`). `isGraphRag` is only
  asserted when ≥2 signal categories fire, to avoid false positives. Documented
  in both v1 response-schema copies.

## 0.8.0

### Transpiler export contract (PRD §6.4, adapter integration)

- **SPARQL export now emits `datatypeProperties`.** `export_mapping(target="sparql")`
  previously emitted only `classes` + `objectProperties`; it now also lists each
  entity's literal attributes as datatype properties (`iri`, `localName`,
  `label`, `domain`, `attribute`, `physical`), so a SPARQL→AQL transpiler can
  resolve literal-valued triple patterns (`?u :email ?e`) to a document-field
  access on the owning collection — not just entity/relationship patterns. The
  `SparqlExport` response-schema `$def` documents the new array (both the bundled
  and published copies, kept byte-identical). Additive and backward compatible.
- **New `docs/transpiler-integration.md`** — the contract for transpiler authors:
  how to consume the Cypher resolution index (`resolve`) and the SPARQL
  vocabulary view (`export`) to generate injection-safe AQL, including variable
  renaming, the `error`-instead-of-`aql` incomplete-mapping path, and datatype vs
  object property resolution. Documents that this library produces the *map*;
  query translation stays in the transpiler (e.g. `arango-cypher`).

### MCP remote transports + auth (PRD §3.11)

- The MCP server (`schema_analyzer/mcp_server.py`) now serves **sse** and
  **streamable-http** in addition to **stdio**, selectable via
  `--transport / --host / --port` (env fallbacks
  `SCHEMA_ANALYZER_MCP_TRANSPORT / _HOST / _PORT`). stdio remains the default,
  so existing invocations are unchanged.
- **Bearer-token auth for remote transports.** Set `SCHEMA_ANALYZER_MCP_TOKEN`
  and every HTTP request must carry `Authorization: Bearer <token>`
  (constant-time compared; missing/invalid → `401 UNAUTHENTICATED`). When
  unset, the server starts but logs a loud warning. The existing `run_tool`
  trust boundary (`SCHEMA_ANALYZER_ALLOWED_HOSTS` / `SCHEMA_ANALYZER_CACHE_ROOT`)
  is enforced for every transport.
- **Typed per-operation tools.** Alongside the generic `arangodb_schema_analyzer_run`
  / `_run_json`, the server now registers
  `schema_analyzer_snapshot | analyze | export | docs | owl` matching the §3.11
  surface, so MCP clients get typed parameters instead of one opaque request dict.

### Confidence calibration from eval feedback (PRD §3.12.3 / §6.5)

- New `schema_analyzer/eval/calibration.py` pairs each eval run's
  self-reported `metadata.confidence` with its realized quality (mean of
  entity / relationship / domain-range F1 and mapping-style accuracy) to
  measure whether confidence is calibrated and where the review gate should
  sit. Pure and DB-free — operates on report-entry dicts, so it runs on live
  results or a saved report and is unit-tested without ArangoDB.
  - `compute_calibration(entries)` returns a reliability curve (per-bin
    predicted-confidence vs observed-quality), **ECE** / **MCE** / **Brier**
    summaries, an overconfidence `gap` (mean confidence − mean quality), and a
    `recommended_review_threshold` derived by maximizing Youden's J of the
    `review_required = confidence < threshold` gate against a binary
    "good run" label (`observed quality ≥ quality_target`). Inputs, formula,
    and failure modes (empty input, all-good / all-bad non-discriminative
    cases) are documented per the §3.12.3 requirement.
  - `format_calibration_report(cal)` renders a human-readable table; the
    `eval` CLI now prints calibration after the score table.
  - **Eval report shape:** `save_eval_report` now writes
    `{"runs": [...], "calibration": {...}}` instead of a bare list.
    `compare_reports` reads both shapes (legacy list baselines still diff) and
    appends a calibration-drift section (gap / ECE / Brier / recommended
    threshold, baseline vs current) so drift is visible release-over-release.
  - New exports from `schema_analyzer.eval`: `compute_calibration`,
    `format_calibration_report`, `observed_quality`, `calibration_from_results`.
  - New tunables in `defaults.py`: `DEFAULT_CALIBRATION_BINS` (10),
    `DEFAULT_CALIBRATION_QUALITY_TARGET` (0.7).

## 0.7.0

### Internal refactor (no behavior change)

- Split the two largest modules for maintainability and isolated testing:
  `analyzer.py` (894→648 lines) extracted its post-inference enrichment pipeline
  into `enrichment.py`; `snapshot.py` (928→696 lines) extracted its DB-free
  type-discriminator heuristics into `type_detection.py`. Public import paths
  are preserved.

### CLI convenience commands

- New subcommands that connect to a database and emit a single artifact without
  hand-authoring v1 request JSON: `arangodb-schema-analyzer snapshot|analyze|docs|owl`
  with `--url/--database/--user/--password|--password-env-var`, plus
  `--provider/--model/--api-key-env-var` (analyze/docs/owl) and `--format`
  (owl). The default stdin/stdout tool mode and `eval` are unchanged.
- **Contract fix:** `metadata.vci` / `metadata.rdfTopology` are now declared
  nullable in the v1 response schema (they serialize as `null` when no signal
  is found); a baseline analyze response previously failed internal validation.

### New v1 tool-contract operations

- **`diff`** — structural diff of `input.previousAnalysis` vs `input.analysis`
  (wraps `diff_analyses`); result under `result.diff`.
- **`resolve`** — Cypher AQL resolution index from `input.analysis` (wraps
  `build_cypher_resolution_index`); result under `result.resolution`.
  Both promote previously library-only helpers to first-class operations.

### Richer OWL export (PRD §6.3)

- Turtle export now emits **`rdfs:subClassOf`** hierarchy from shard families,
  **`owl:FunctionalProperty` / `owl:InverseFunctionalProperty`** +
  `phys:observedCardinality` derived from the statistics cardinality pattern, and
  **`owl:inverseOf`** from an explicit relationship `inverseOf` field.
- New **JSON-LD export**: `export_conceptual_model_as_jsonld(analysis)` and the
  `owl` operation's `outputOptions.owlFormat: "jsonld"` (returns `result.jsonld`).

### Roadmap detections

- **RDF-topology (RPT) detection + TRIPLE mapping style** (PRD §6.1/§6.2).
  Deterministic, snapshot-only. Recognizes RDF triple/quad stores via collection
  naming (`_triples`, `quads`, …) or a subject/predicate/object field signature,
  and `rdf:type` assertion edges via sampled predicate values. Emits
  `metadata.rdfTopology` and annotates affected physical-mapping entries with
  `tripleCandidate: true` + a `triple` (`{"style": "TRIPLE"}`) block alongside the
  native style.
- **Vertex-Centric Index (VCI) detection** (PRD §6.1/§6.2). Deterministic,
  snapshot-only. Relationship mappings whose edge collection carries a
  persistent index rooted at `_from`/`_to` plus discriminator fields, and/or
  edge attributes that duplicate endpoint-vertex properties, gain a `vci` block
  (`indexLevel` with access pattern out-edge/in-edge/both + participating
  fields; `denormalization` with duplicated fields and source collections) and
  `vciCandidate: true` *alongside* the existing style. A `metadata.vci` summary
  lists the relationships involved. Also fixes a latent gap where the LLM path
  dropped `metadata.multitenancy` from the typed result.

### Transpiler, lineage & egress features (Milestone B)

- **SPARQL export target.** `export_mapping(analysis, target="sparql")` (and the
  v1 `export` operation with `outputOptions.exportTarget="sparql"`) emits an RDF
  vocabulary view — classes, object properties with IRIs/domains/ranges —
  annotated with the physical mapping so a SPARQL→AQL transpiler can resolve
  triple patterns to collections and edge traversals. The response contract
  gains a `SparqlExport` shape.
- **Cypher resolution adapter.** New `build_cypher_resolution_index(analysis)`
  precomputes, per entity label and relationship type, the injection-safe AQL
  match/traversal fragment a Cypher transpiler would otherwise re-derive;
  incomplete mappings are reported with an `error` block instead of crashing.
- **Analysis diff.** New `diff_analyses(previous, current)` (PRD §3.13.3)
  returns added/removed/changed entities and relationships, mapping-style flips,
  and the health-score delta — for stale detection and re-analysis workflows.
- **Element-level provenance.** Every conceptual entity/relationship and
  physical-mapping entry is tagged with `source` (`llm` / `baseline` / `human`,
  PRD §3.13.2); reconciliation-backfilled collections are correctly attributed
  to `baseline` even on an LLM run, and pre-existing `human` tags are preserved.
- **LLM redaction modes.** New `analysisOptions.redaction` (`stripSamples`,
  `maskFieldValues`, PRD §4.3) scrubs sampled documents and concrete field
  values from the snapshot before LLM egress while preserving structure; the
  local snapshot used for baseline/reconciliation/statistics is unaffected.

### Quality & developer experience (Milestone A)

- **Quality metrics + health score.** Every analysis now stamps
  `metadata.qualityMetrics` (deterministic structural signals — connectivity,
  orphan ratio, property richness, dangling-relationship consistency — plus
  grounding signals that check the physical mapping against the snapshot) and
  a normalized 0–100 `metadata.healthScore` composite (PRD §3.12.3). See
  `schema_analyzer/quality.py`. The v1 response contract documents both fields
  (`metadata` already allowed additional properties, so this is backward
  compatible).
- **mypy is now a blocking CI gate.** The `pydantic.mypy` plugin is enabled and
  the previously-tolerated type backlog (python-arango `Result[...]` unions,
  alias mismatches, `Any` leaks) was cleared. A small typed adapter,
  `schema_analyzer/_arango.py`, centralizes the synchronous-path casts.
- **Coverage floor raised 65% → 80%**, with new tests for the LLM retry/repair
  loop, the async analysis path, and the stdlib OpenRouter provider.
- **Contract-parity test.** `tests/test_tool_contract_schema_parity.py` asserts
  the documented (`docs/tool-contract/v1`) and bundled
  (`schema_analyzer/tool_contract/v1`) JSON schemas stay byte-identical.
- **Async path verified.** All shipped providers implement `agenerate`; the
  async `analyze_physical_schema_async` entrypoint now has end-to-end coverage.

## 0.6.1

Bugfix and hardening release. No new user-facing features and no
intentional breaking changes; existing v1 contract callers and
programmatic consumers continue to work unchanged.

### Tool contract (v1)

- **Drop `outputOptions.pretty`** from the request schema. The field was
  declared in 0.6.0 but never read by `run_tool`; pretty-printing has
  always been a CLI / caller concern. Strict callers passing `pretty`
  must remove it to keep validating.
- **Honor `outputOptions.includeSnapshotFingerprint`.** Default `true`
  (previous behavior). Setting `false` now actually omits
  `tooling.snapshotFingerprint` from the response.
- **Bound request size.** `maxLength` on string fields (URL ≤ 2048,
  password ≤ 1024, system prompt ≤ 65536, etc.) and `maxItems` on
  request-side analysis arrays (`entities` / `relationships` ≤ 50000,
  `properties` ≤ 100000). Closes a DoS surface on any process exposing
  the v1 contract over MCP / RPC. Requests within historical sizes are
  unaffected.

### Security hardening

- **Cache directory containment.** New optional `SCHEMA_ANALYZER_CACHE_ROOT`
  env var. When set, every resolved cache directory must live under the
  configured root; `..` traversal is rejected.
- **Connection host allowlist.** New optional
  `SCHEMA_ANALYZER_ALLOWED_HOSTS` env var (comma-separated host[:port]).
  When set, `connection.url` requests targeting other hosts are
  rejected before any database call. Unset preserves the historical
  trust-the-caller behavior for local CLI use.
- **Cache file permissions.** Filesystem cache writes are now `chmod 0o600`
  on POSIX hosts so cached samples are owner-only on shared systems.
- **OpenRouter provider hardening.** HTTP error bodies are scrubbed for
  bearer-token / `api_key` / `sk-…` patterns before being attached to
  `SchemaAnalyzerError`; HTTP redirects are blocked via a custom opener
  so a typo'd `base_url` cannot silently relay credentials to a
  different host.
- **LLM output allowlist.** Reconciliation now strips any
  `collectionName` / `edgeCollectionName` produced by the LLM that does
  not appear in the live snapshot, before backfill runs. Bind-parameter
  AQL already prevented injection; this closes the integrity gap so
  downstream consumers cannot trust hallucinated names.

### Internal refactor (no behavior change)

- Consolidated three near-duplicate `entity_property_names` helpers into
  one in `utils.py`; fixes a latent bug where dict-shaped properties
  were silently skipped in `tenant_scope`.
- Added shared helpers: `normalize_analysis_dict`,
  `iter_edge_definitions`, `index_edge_definitions_by_collection`,
  `split_domain_tokens` in `utils.py`; `import_optional_sdk` and
  `wrap_provider_call` in `providers/base.py`.
- Unified sync / async retry policy in `workflow.py` via a shared
  `_retry_decision` helper.
- Routed all hardwired tunables through `defaults.py`
  (`BASELINE_NO_LLM_CONFIDENCE`, `OPENROUTER_*`, `DEFAULT_*_MODEL`,
  `STATISTICS_*`, `SNAPSHOT_FORMAT_VERSION`, `DEFAULT_OWL_*_IRI`,
  `DEFAULT_EVAL_*`).
- Pre-compiled the default tenant-collection naming regex in
  `multitenancy.py`.
- Added `fresh_database` pytest fixture in `tests/conftest.py`;
  integration tests now reuse it.

### Documentation

- README: documented `--verbose` long form and the `[dev]` /
  `[openrouter]` extras; fixed regex backtick escaping in the config
  table.
- PRD: marked the MCP server as shipped (no longer "future"); widened
  the testing matrix to Python 3.10–3.13 to match the classifiers;
  documented the new env-driven security knobs in §4.3.
- CONTRIBUTING: corrected the project tree to list every `__init__.py`;
  documented `scripts/run_reverse_engineering_eval.py` as an ad-hoc
  developer runner.
- `.env.example`: clarified that `ARANGO_HOST` / `ARANGO_PASSWORD`
  fallbacks apply only to the eval CLI, plus examples for the new
  `SCHEMA_ANALYZER_*` env vars.
- `docs/tool-contract/v1/`: synchronized the schema and example copies
  with the bundled package versions.

### Tests

- 353 unit tests passing (32 integration tests skip without
  `RUN_INTEGRATION=1`). New coverage in `tests/test_tool_security.py`,
  `tests/test_phase2_helpers.py`, and the expanded `tests/test_utils.py`.

## 0.6.0

Additive, non-breaking. Existing exports continue to validate against
the v1 contract unchanged.

### New features

- **Shard-family detection (`physicalMapping.shardFamilies`).**
  Implements PRD §6.2 bullet 5. A *shard family* groups conceptual
  entities that share an identical property set and a common
  CamelCase / snake_case suffix — the structural fingerprint of the
  per-source / per-repository / per-stream collection-duplication
  pattern (e.g. `IBEX_Documents` / `MAROCCHINO_Documents` /
  `MOR1KX_Documents` / `OR1200_Documents` ⇒ family `Document`).

  Detection is deterministic, snapshot-only (no DB round-trip, no LLM
  call). Each family carries:

  - `name` / `suffix` — family label and the verbatim shared suffix.
  - `discriminator` — `{source: "field", field: <name>}` when every
    member declares a candidate discriminator field (default
    candidates: `repo`, `source`, `stream`, `upstream`); otherwise
    `{source: "collection_prefix"}` and the discriminator value is
    drawn from each member's name prefix.
  - `sharedProperties` — sorted property-name list (the bucket key).
  - `members[]` — for each member entity, its conceptual name,
    underlying `collectionName`, and `discriminatorValue` (the
    name-prefix portion).

  Downstream impact: NL→Cypher prompt builders and UI mapping panels
  can now emit UNION-aware guidance instead of silently picking one
  member alphabetically (the IBEX/MAROCCHINO/MOR1KX/OR1200 first-in-
  summary bias, defect D7 in `arango-cypher-py/docs/schema_inference_bugfix_prd.md`).

  Output is sorted by `(name, suffix)` for deterministic golden-snapshot
  output. `shardFamilies` is omitted entirely when the input has no
  usable entity dict; an empty list (`[]`) means detection ran and
  found no families (consumers can distinguish "didn't run" from "ran,
  found none").

### Tunables (`schema_analyzer/defaults.py`)

- `MIN_SHARD_FAMILY_SIZE` (default `2`) — minimum members for a family.
- `MIN_SHARD_FAMILY_SUFFIX_LEN` (default `4`) — minimum suffix length;
  short suffixes like `Op`/`Tx` are too noisy to be useful.
- `SHARD_FAMILY_DISCRIMINATOR_FIELDS` (default `("repo", "source",
  "stream", "upstream")`) — case-insensitive candidate field names
  probed in order; first one carried by *every* member wins.

### Backward compatibility

- `PhysicalMapping.shard_families` is `None` by default. `to_json`
  omits the `shardFamilies` key when `None`, preserving byte-identity
  with pre-detector output for callers that build mappings by hand.
- v1 response schema gains an additive `shardFamilies` array under
  `physicalMapping`. Existing fixtures continue to validate (the field
  is optional).
- No cache invalidation needed: `physical_fingerprint` is unchanged
  and the new field is purely additive.

## 0.5.0

Additive, non-breaking. Existing exports continue to validate against
the v1 contract unchanged.

### New features

- **Sharding-profile classification (`metadata.shardingProfile`).**
  First landing of the PRD §6.2 "Sharding-pattern detection" bullet
  (spec committed in `b3d4744`). The analyzer now classifies every
  analyzed database into one of five exclusive deployment styles, once
  per analysis:

  - `OneShard` — database-level `sharding == "single"`. Single-shard
    databases where cross-collection traversal never crosses DBServers.
    Carries an `oneShardLeader` hint when every user collection shares
    a consistent `distributeShardsLike` leader.
  - `DisjointSmartGraph` — at least one named graph is both
    `isSmart == true` and `isDisjoint == true`. The canonical
    ArangoDB multi-tenant pattern: traversal across the disjoint
    attribute is forbidden by the storage layer.
  - `SmartGraph` — at least one smart (but non-disjoint) named graph.
    Vertex collections share the smart attribute as their shard key;
    edge traversals are locality-aware.
  - `SatelliteGraph` — every user collection is a satellite (typical
    of meta-graph / ontology / reference databases).
  - `Sharded` — fall-through default for everything else; standard
    hash-sharded collections.

  Classification is deterministic and snapshot-only — no new DB round
  trip. Missing fields (older ArangoDB versions, restricted users
  whose `db.properties()` returned partial data) degrade to `Sharded`
  with `shardingProfile.status == "degraded"` and a human-readable
  `statusReason` instead of raising.

  The block carries per-graph evidence (`graphs[*].{isSmart,
  isDisjoint, smartGraphAttribute, vertexCollections,
  edgeCollections}`), per-collection evidence
  (`collections[*].{kind, numberOfShards, shardKeys,
  replicationFactor, distributeShardsLike, smartGraphAttribute,
  isDisjoint, graphName}`), the database-level properties the
  classifier used (`database.{sharding, replicationFactor,
  writeConcern}`), and a `collectionKindCounts` summary so downstream
  consumers can branch on the breakdown without iterating the full
  collections map.

  `metadata.shardingProfileStatus` mirrors `shardingProfile.status`
  for callers that only need the `"ok"` / `"degraded"` bit, matching
  the existing `metadata.statisticsStatus` convention.

- **Snapshot now carries `database` block and SmartGraph flags on
  graphs.** `snapshot["database"]` captures `{name, sharding,
  replicationFactor, writeConcern}` from `db.properties()`;
  `snapshot["graphs_detailed"][*]` now also carries `isSmart`,
  `isDisjoint`, `smartGraphAttribute`, `isSatellite` when the server
  exposes them. Unlocks the sharding-profile classifier without
  another DB probe; backwards-compatible (older consumers simply
  ignore the new keys).

### Backward compatibility

- The `database` block defaults to `{}` when `db.properties()` fails
  or is unavailable. Snapshot JSON schema is permissive
  (`additionalProperties: true`), so no consumer needs to change.
- `metadata.shardingProfile` is optional in the v1 response schema;
  pre-Unreleased cached analysis results load unchanged.
- One-time analysis-cache invalidation on upgrade.
  `fingerprint_physical_schema` hashes the whole snapshot dict, so
  adding `snapshot["database"]` and the per-graph SmartGraph flags
  changes every fingerprint exactly once; the first analysis after
  upgrade re-runs the LLM workflow and then caches normally. This is
  the standard behaviour whenever a new structural field lands — it
  matches the cache-reset behaviour of prior snapshot extensions
  (e.g. VCI flags in 0.3.0).

## 0.4.0

Additive, non-breaking. Existing single-tenant exports continue to
validate against the v1 contract unchanged.

### New features

- **#13 tenant-scope annotations.** The analyzer now classifies every
  entity in the physical mapping by its tenant role and stamps the
  result onto `physicalMapping.entities[*].tenantScope`. Three roles
  are recognised:

  - `tenant_root` — the entity that anchors the tenant hierarchy
    (default name: `Tenant`; configurable via
    `SCHEMA_ANALYZER_TENANT_ROOT_NAMES`).
  - `tenant_scoped` — belongs to a single tenant. Carries
    `tenantField` when the entity has a denormalised tenant-reference
    column (default regex: `^tenant[_-]?(id|key)$`, configurable via
    `SCHEMA_ANALYZER_TENANT_FIELD_REGEX`); omits it when the entity
    is only reachable from the tenant root via traversal.
  - `global` — cross-tenant reference data (e.g. `Cve`,
    `AppVersion`). Consumers MUST NOT add a tenant filter to queries
    against these collections.

  Detection is deterministic and depends only on the conceptual
  schema + physical mapping (no LLM call). It runs after
  reconciliation so backfilled entities are also classified, and
  before validation so the response-schema check covers the new
  field. Operators can pre-stamp `tenantScope` on any entry to
  override the heuristic — explicit annotations always win. BFS depth
  for traversal-only classification is capped at
  `TENANT_SCOPE_MAX_HOPS` (default 5; env-var overridable).

  A per-run summary lands in `metadata.tenantScopeReport` with
  `tenantEntity`, `denormScopedCount`, `traversalScopedCount`,
  `globalCount`, `tenantFieldRegex`, and a `discovery` breakdown by
  source (explicit / denorm-field / traversal).

  No-op (and no metadata block) when no tenant root is detected, so
  single-tenant graphs are byte-identical to their `0.3.0` exports.

### Tool contract additions (additive)

- `physicalMapping.entities[*].tenantScope` is now defined as an
  optional object with `role` (enum: `tenant_root` /
  `tenant_scoped` / `global`), optional `tenantField`, and optional
  `tenantEntity`. See the new `TenantScope` `$def` in
  `tool_contract/v1/response.schema.json`.
- `metadata.tenantScopeReport` is now defined as an optional object
  carrying the per-run classification summary.

### Configuration

New tunables in `defaults.py`:

- `TENANT_SCOPE_ROOT_NAMES = ("Tenant",)`
- `TENANT_SCOPE_FIELD_REGEX = r"^tenant[_-]?(id|key)$"`
- `TENANT_SCOPE_MAX_HOPS = 5`

Each is overridable at runtime via the matching
`SCHEMA_ANALYZER_TENANT_*` environment variable.

## 0.3.0

First PyPI release. Consolidates the quality + contract work originally
slated for `0.2.0` (issues #2-#6) with the cheap schema-change probes
(#7) and PRD amendment (#8) that landed on `main` shortly after.
Version `0.2.0` was prepared on `main` but never published; `0.3.0` is
the first tag to reach PyPI.

### Tool contract changes (breaking)

- **#6 key rename.** Property mappings now emit `field` (was
  `physicalFieldName`). Relationship mappings now emit
  `edgeCollectionName` and MUST NOT emit `collectionName` — the JSON
  schema rejects the latter. Entity mappings still use
  `collectionName` (unchanged). See `tool_contract/v1/response.schema.json`.
  Consumers that previously ran a `_normalize_analyzer_pm` /
  `_normalize_props` shim can delete it.

### New features

- **#7 cheap schema-change probes.** Two new top-level helpers in
  `schema_analyzer.snapshot` (re-exported from the package root):
  - `fingerprint_physical_shape(db, *, exclude_collections=None)` — hashes
    only the user-collection set, per-collection type (document vs edge),
    and per-collection sorted index digests (`type`, `fields`, `unique`,
    `sparse`, `vci`, `deduplicate`). Auto-generated index `name` / `id`
    are excluded so restarts and rebuilds don't produce false positives.
    Stable under ordinary INSERT / UPDATE / REMOVE writes.
  - `fingerprint_physical_counts(db, *, exclude_collections=None)` —
    shape fingerprint combined with `col.count()` per included
    collection; changes whenever the shape or any row count changes.
  Both probes read only python-arango primitives (`db.collections()`,
  `col.indexes()`, `col.count()`) — no AQL, no samples, no analyzer
  logic — so consumers can answer "has it changed?" in a few dozen
  milliseconds instead of running the full `snapshot_physical_schema`.
  Collection-level failures degrade gracefully (sentinel contribution)
  rather than raising. `exclude_collections` lets callers using a
  database-resident cache self-exclude their bookkeeping collection.
- **#3 statistics block.** `AgenticSchemaAnalyzer` now stamps
  `metadata.statistics` with per-collection counts, per-entity
  `estimated_count`, and a per-relationship bundle of `edge_count`,
  `source_count`, `target_count`, `avg_out_degree`, `avg_in_degree`,
  `cardinality_pattern` (`1:1` / `1:N` / `N:1` / `N:M`) and
  `selectivity`. When no live DB is available
  `metadata.statistics_status = "skipped_no_db"` and `statistics` is
  absent. Bounded AQL cost: one `LENGTH` per collection, one filtered
  `COLLECT` per LABEL / GENERIC_WITH_TYPE subset.
- **#5 reconciliation step.** After the LLM returns, the analyzer
  diffs its collection coverage against the snapshot and backfills any
  missing collections via baseline inference. The merge is reported in
  `metadata.reconciliation` with `llm_covered_collections`,
  `snapshot_collections`, `backfilled_collections`, and `strategy`; a
  user-visible warning is appended. No-op when the LLM's output is
  already complete.

### Quality

- **#4 discriminator hardening.** `_pick_best_type_field` now rejects
  candidate type fields that look like identifiers (`*Id`, `*_id`,
  `uuid`, etc.), carry too many distinct values
  (`MAX_TYPE_FIELD_DISTINCT_VALUES=32`), or cover too little of the
  collection (`MIN_TYPE_FIELD_COVERAGE_FRACTION=0.80`). Single-distinct-
  value edge discriminators are still accepted under the
  single-value-edge fallback. New tunables live in `defaults.py`.
- **#2 richer index flags.** `physicalMapping[...].indexes[*]` now
  propagates `vci`, `deduplicate`, and `storedValues` from the raw
  ArangoDB index metadata. Vertex-Centric Indexes are excluded from the
  `indexed=True` heuristic on properties.

### Documentation

- **#8 PRD §3.13.3 / §4.1 update.** The PRD now sanctions the two-
  fingerprint model (shape vs counts), a four-state change-status
  contract (`unchanged` / `stats_changed` / `shape_changed` /
  `no_cache`), stats-only refresh as the product behavior for
  `stats_changed`, storage-agnostic caching, and self-exclusion of
  database-resident cache collections from the shape fingerprint.

### CI / Release infrastructure

- Trusted-publisher GitHub Actions workflow (`publish.yml`) targeting
  PyPI and TestPyPI via OIDC — no long-lived tokens.
- `sdist` allow-list tightened in `pyproject.toml` so source
  distributions include only package code, licence, readme, changelog,
  and the tool-contract JSON schemas.
- `schema_analyzer/py.typed` added so downstream type checkers pick up
  the package's inline annotations.
- Ruff lint + format are enforced by CI; mypy runs in advisory mode.

## 0.2.0 (never published)

`0.2.0` was bumped on `main` as the planned first PyPI release but the
tag was never cut — #7 and #8 landed before release and the scope was
rolled forward into `0.3.0`. Everything originally slated for `0.2.0`
is part of `0.3.0` above.

## 0.1.0

### Initial release

- Physical schema snapshotting with deterministic ordering and fingerprinting
- Conceptual schema inference (entities, relationships, properties)
- Physical mapping generation (COLLECTION, LABEL, DEDICATED_COLLECTION, GENERIC_WITH_TYPE)
- AQL fragment helpers (`aql_entity_match`, `aql_relationship_traversal`) with injection-safe bind parameters
- LLM-assisted analysis with generate → validate → repair loop
- Provider support: OpenAI, Anthropic, OpenRouter (pluggable registry)
- Deterministic baseline inference when no LLM is configured (graceful degradation)
- Filesystem caching keyed by schema fingerprint with configurable TTL
- Tool contract v1: stable JSON API (stdin/stdout) with request/response schema validation
- CLI: tool mode (stdin JSON) and eval subcommand
- Output formats: analysis JSON, snapshot, export (Cypher), Markdown docs, OWL Turtle
- Evaluation harness with 5 domain packs, physical schema generator, and F1/accuracy scoring
- Eval report comparison for tracking quality regressions

### Quality improvements

- Centralized tunable defaults in `defaults.py` (LLM parameters, timeouts, confidence, cache)
- Unified `pascal_case()` utility replacing duplicate implementations
- Shared test helpers extracted into `conftest.py`
- Consolidated sync/async workflow via shared `_parse_and_validate()` helper
- Catch-all error handler in tool entrypoint for contract-shaped error responses
- Eliminated redundant snapshot work (tool passes pre-built snapshot to analyzer)
- TYPE_CHECKING guards for `StandardDatabase` imports
- Proper exception chaining (`raise ... from e`) across all providers and workflow
- Logging in tool.py (operation tracking) and cache.py (corrupt file warnings)
- CI: pip caching, integration tests trigger on PRs, coverage threshold at 65%
- Test coverage for: cache, docs, exports, OWL export, validation, providers, conceptual schema, CLI, tool happy paths
