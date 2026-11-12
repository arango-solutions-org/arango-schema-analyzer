# Transpiler integration guide

This library does **not** translate Cypher/SPARQL/SQL queries to AQL. It produces
a stable **mapping contract** that a transpiler consumes to do that translation
itself. This document is the contract for transpiler authors: what the export
shapes contain and how to turn each into AQL.

Two entry points, both stable and JSON-serializable:

| Export | Function | v1 operation | For |
|---|---|---|---|
| Cypher resolution index | `build_cypher_resolution_index(analysis)` | `resolve` | Cypher → AQL |
| SPARQL vocabulary view | `export_mapping(analysis, target="sparql")` | `export` (`exportTarget=sparql`) | SPARQL → AQL |
| Raw bundle | `export_mapping(analysis, target="cypher")` | `export` (`exportTarget=cypher`) | anything (raw conceptual + mapping) |

All AQL fragments are **injection-safe**: collection and discriminator values are
passed as bind variables, never string-interpolated. Never reconstruct AQL by
concatenating names yourself — use the provided `query` + `bindVars`.

---

## Cypher resolution index (`resolve`)

```jsonc
{
  "target": "cypher",
  "entities": {
    "User": {
      "style": "COLLECTION", "collectionName": "users",
      "aql": { "query": "FOR n IN @@col_n", "bindVars": { "@col_n": "users" } }
    },
    "Post": {
      "style": "LABEL", "collectionName": "nodes", "typeField": "type", "typeValue": "Post",
      "aql": { "query": "FOR n IN @@col_n FILTER n.@tf_n == @tv_n",
               "bindVars": { "@col_n": "nodes", "tf_n": "type", "tv_n": "Post" } }
    }
  },
  "relationships": {
    "WROTE": {
      "style": "DEDICATED_COLLECTION", "edgeCollectionName": "wrote",
      "aql": { "query": "FOR b, r IN 1..1 OUTBOUND a @@edge_r", "bindVars": { "@edge_r": "wrote" },
               "edgeVariable": "r" }
    }
  }
}
```

**How to consume:**

1. **Node pattern** `(n:User)` → look up `entities["User"].aql`. Splice its `query`
   as the `FOR` clause and merge its `bindVars`. For a `LABEL`-style entity the
   query already includes the `FILTER n.type == "Post"` discriminator.
2. **Relationship pattern** `(a)-[r:WROTE]->(b)` → look up
   `relationships["WROTE"].aql`. It is a traversal rooted at the variable `a`
   (rename to match your plan); `edgeVariable` is the bound edge variable for
   `GENERIC_WITH_TYPE` styles that also emit a `FILTER r.relType == ...`.
3. **Variable renaming:** the fragments use placeholder variables (`n`, `a`, `b`,
   `r`) and **suffix their bind keys with the variable** (`@col_n`, `tf_n`) so two
   fragments compose without bind-key collisions. Rename consistently.

**Incomplete mappings.** When a mapping can't produce AQL (e.g. a `LABEL` entity
missing its `typeValue`), the entry carries an `error` block **instead of** `aql`:

```jsonc
"Comment": { "style": "LABEL", "collectionName": "nodes",
             "error": { "code": "INVALID_ARGUMENT", "message": "LABEL mapping requires typeValue" } }
```

Surface `error.message` as a diagnostic; do not synthesize a fallback query.

---

## SPARQL vocabulary view (`export`, `exportTarget=sparql`)

```jsonc
{
  "target": "sparql",
  "prefixes": { "": "https://.../", "phys": "...", "owl": "...", "rdf": "...", "rdfs": "...", "xsd": "..." },
  "classes": [
    { "iri": ":User", "localName": "User", "label": "User",
      "physical": { "style": "COLLECTION", "collectionName": "users" } }
  ],
  "objectProperties": [
    { "iri": ":wrote", "localName": "wrote", "label": "WROTE",
      "domain": ":User", "range": ":Post",
      "physical": { "style": "DEDICATED_COLLECTION", "edgeCollectionName": "wrote" } }
  ],
  "datatypeProperties": [
    { "iri": ":email", "localName": "email", "label": "email",
      "domain": ":User", "attribute": "email",
      "physical": { "style": "COLLECTION", "collectionName": "users" } }
  ],
  "physicalMapping": { /* full mapping, for anything not pre-resolved */ }
}
```

**How to resolve a triple pattern `?s p ?o`:**

1. **`?s rdf:type :User`** → `classes[].physical`. Iterate the matching
   collection; for a `LABEL` style add the `typeField == typeValue` filter
   (mirrors the Cypher index above), and for a `LABELS` style the
   `labelValue IN doc[labelsField]` filter (`labelValue` defaults to the class name).
2. **`?s :wrote ?o`** (predicate in `objectProperties`) → a graph traversal over
   `physical.edgeCollectionName`, from the `domain` class to the `range` class.
   `GENERIC_WITH_TYPE` edges add a `typeField == typeValue` filter on the edge.
3. **`?s :email ?o`** (predicate in `datatypeProperties`) → **not** a traversal:
   it's a document-field access. Read `physical.attribute` (the field name) on the
   `domain` class's collection — `RETURN s.email`, or `FILTER s.email == @v` for a
   bound object. This is the piece that lets literal-valued patterns resolve;
   without it a SPARQL transpiler can only handle entity/relationship patterns.

A datatype property for a value inside a nested object also carries `path`, one
name per level (`"path": ["address", "city"]`). Read it instead of `attribute`:
`RETURN s.address.city`, never `` s.`address.city` `` (see "Values inside nested
objects" below). The OWL export writes the same location as `phys:attributePath`.

A datatype property whose attribute holds a list carries `"multiValued": true` (CSI
1.2 property mappings carry the same key; the OWL export writes
`phys:multiValued "true"`). Each element is one value: bind `?o` once per element
(`FOR v IN s.questionsServed`) and match a bound object by membership
(`@v IN s.questionsServed`), not by `==`, which compares the whole list. Every other
datatype property is one value, so `==` and an index on it still apply.

`domain`/`range`/IRIs are absent only when the source analysis lacks an endpoint;
fall back to `physicalMapping` or report an unresolved pattern. The same datatype
predicate IRI may appear under multiple `domain`s (a property reused across
classes) — resolve by `(domain, predicate)`, not predicate alone.

---

## Named graphs

By default the analyzer covers the **whole database** and labels which ArangoDB
named graph(s) each element belongs to; you can also **scope** an analysis to a
single named graph. Both are additive — absent when the database has no named
graphs, so non-graph schemas are unaffected.

### What you receive

**Per-entry annotation** (on every `physicalMapping` entity/relationship; also
present in CSI's `arangoPhysicalMapping`):

```jsonc
"entities":      { "User":    { "style": "COLLECTION", "collectionName": "users", "graphs": ["content","social"] } },
"relationships": { "FOLLOWS": { "style": "DEDICATED_COLLECTION", "edgeCollectionName": "follows", "graphs": ["social"] } }
```

- `graphs` is sorted and **multi-valued** (a vertex collection shared across
  graphs lists all of them) and is **omitted** when the element is in no named
  graph (an *ungraphed* collection).

**Summary block** `metadata.graphMembership` (present in the `analyze` response;
**not** in a CSI document, which carries only provenance metadata — reconstruct
from per-entry `graphs` if you consume CSI):

```jsonc
{
  "status": "ok",
  "graphCount": 2,
  "graphs": {
    "social":  { "entities": ["User"], "relationships": ["FOLLOWS"],
                 "vertexCollections": ["users"], "edgeCollections": ["follows"] },
    "content": { "entities": ["Post","User"], "relationships": ["WROTE"],
                 "vertexCollections": ["posts","users"], "edgeCollections": ["wrote"] }
  },
  "ungraphed": { "entities": ["Loose"], "relationships": [] }
}
```

**Scoping input** `analysisOptions.graphScope: "<graphName>"` (or
`analyze_physical_schema(graph_scope=...)`): restricts the snapshot/analysis to
that graph's collections (edge collections + their `from`/`to` vertices +
orphans). A missing graph yields `{ ok:false, error:{ code:"INVALID_ARGUMENT" }}`.

### How to consume

**Cypher → AQL:**

- When a relationship's mapping has `graphs: ["<name>"]`, you may emit a
  **named-graph traversal** (`FOR v,e IN OUTBOUND start GRAPH "<name>"`) instead
  of the edge-collection form. When the relationship is **ungraphed** (no
  `graphs`), keep the collection-based traversal — `GRAPH "<name>"` is not
  available for it.
- A label whose entity is shared across graphs has `graphs: [g1, g2]`;
  disambiguate by the relationship(s) in the query, or surface the choice — do
  not assume one.

**SPARQL → AQL:**

- Map SPARQL named graphs to ArangoDB named graphs directly:
  `GRAPH :social { ?a :FOLLOWS ?b }` → restrict to
  `graphMembership.graphs["social"]`'s vertex/edge collections (or `GRAPH
  "social"`). Use the `graphs` keys as the available named-graph IRIs.
- The **default graph** (no `GRAPH` clause) is your dataset policy; **ungraphed**
  collections belong only to the default graph (they are in no named graph).
- Per-class / per-predicate membership comes from the entity/relationship
  `graphs` lists, complementing the IRIs in the SPARQL vocabulary view above.

Either transpiler can request `graphScope` to obtain a focused, single-graph
mapping when a query is meant to run within one named graph.

> **Note (≥ this release):** graph definitions are now parsed correctly from
> live databases (python-arango's normalized `edge_definitions` /
> `edge_collection` / `*_vertex_collections` shape). Earlier builds left
> `graphs_detailed` edge definitions empty against real ArangoDB, so any
> graph-derived signals were unreliable.

---

## Values inside nested objects (`path`, CSI 1.1)

A property mapping says where a value is stored, in one of two ways:

- **`field`** is one literal attribute name. `{"field": "address.city"}` means a
  single top-level attribute whose name contains a dot. It never means `city`
  inside `address`.
- **`path`** is a list with one name per level: `{"path": ["address", "city"]}`
  is `city` inside the object `address`. A segment may itself contain a dot and
  is still one level.

A mapping carries at most one of the two. With neither, the property name is the
attribute.

**Asking for nested values.** Pass `analysisOptions.nestedFieldDepth` (1-5; the
default 0 keeps today's top-level-only output). The analyzer then walks the
sampled documents into nested objects and decides, once at analysis time, whether
each dotted name is nested (`path`) or literal (`field`). A transpiler never has
to guess at query time.

**What it will not guess.** Suppose one collection holds a top-level attribute
named `address.city` and also a `city` inside `address`. The sample cannot say
which one a query means, so neither gets a mapping. The name is listed under
`metadata.nestedFields.conflicts`, and in a CSI document under
`provenance.unresolvedFieldLocations`, for a person to settle. A list of objects
partway along a path is not walked (lists are a later step); each one is listed
under `metadata.nestedFields.arraysSkipped`.

**CSI version.** A CSI document whose property mappings use `path` declares
`csiVersion: "1.1"`. Every other document still declares `"1"`, so readers that
accept only `"1"` keep working after an analyzer upgrade. A reader that sees
`path` in a `"1"` document must refuse it. Entity property mappings in CSI are
strict: besides `field` / `path` they may hold only `type`, `indexed`, `unique`,
`required`, `role`, `sentinelValues`, `numericLike`, `sampleValues`, or a key
starting with `x-`. The reader side of this format is arango-sparql-py#84.

---

## Stability & versioning

- The fields above are a contract: keys are added compatibly, not removed or
  renamed within v1. Both export objects set `additionalProperties: true`, so
  tolerate unknown keys.
- The bundled JSON Schemas (`schema_analyzer/tool_contract/v1/response.schema.json`,
  `SparqlExport` / resolution `$defs`) are the machine-readable source of truth and
  are asserted byte-identical to the published copies under `docs/tool-contract/v1/`.
- `style` values are the four mapping styles in PRD §3.3
  (`COLLECTION`, `LABEL`, `DEDICATED_COLLECTION`, `GENERIC_WITH_TYPE`), plus the
  opt-in entity style `LABELS` (`analysisOptions.entityStrategy: "labels"`): the
  node's labels are a list in `labelsField`, and it matches when `labelValue` is one
  of them (`FILTER @label IN n[@labelsField]`). A node carrying several labels
  matches each. The additive `TRIPLE` / VCI annotations (PRD §6.1) appear
  alongside, never replacing the native `style`.
