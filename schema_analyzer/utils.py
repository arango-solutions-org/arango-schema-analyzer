from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_AQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: ArangoDB's system attributes, which no schema property maps to.
SYSTEM_FIELDS = frozenset({"_key", "_id", "_rev", "_from", "_to"})


def assert_aql_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not _AQL_IDENT_RE.match(value):
        raise ValueError(f"Invalid AQL identifier for {name}")


def stable_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entity_property_names(entity: dict[str, Any]) -> list[str]:
    """
    Return the property-name list declared on a schema entity, tolerating
    every shape the analyzer ever emits.

    Three shapes are accepted:

    * ``properties`` is a **dict** keyed by property name (the
      ``physicalMapping.entities[*].properties`` shape used by the LLM
      output and by ``infer_baseline_from_snapshot``).
    * ``properties`` is a **list of dicts** with a ``name`` key (the
      ``conceptualSchema.entities[*].properties`` shape).
    * ``properties`` is a **list of bare strings** (older fixtures /
      truncated LLM output).

    Any other shape (including ``None``) yields ``[]``. Order of the
    returned list mirrors the input order; callers that need a stable
    order should sort.
    """
    props = entity.get("properties")
    if isinstance(props, dict):
        return [str(k) for k in props]
    if isinstance(props, list):
        out: list[str] = []
        for item in props:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                out.append(item["name"])
        return out
    return []


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_domain_tokens(name: str) -> list[str]:
    """Lowercase ``name`` and split on common separators (``-`` / ``_``),
    returning the non-empty token list.

    Used by domain-detection scoring to derive matchable keyword tokens
    from collection / graph / type-value names. Empty input or
    non-strings yield ``[]``.
    """
    if not isinstance(name, str) or not name:
        return []
    return [p for p in name.lower().replace("-", "_").split("_") if p]


def iter_edge_definitions(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield well-formed ``edge_definitions`` records from a snapshot
    graph entry, dropping any malformed (non-dict / no-collection) rows.

    Centralises the common ``for ed in graph.get("edge_definitions") or []:
    if not isinstance(ed, dict): continue ...`` pattern that previously
    appeared in ``baseline.py`` and ``sharding_profile.py``.
    """
    out: list[dict[str, Any]] = []
    for ed in graph.get("edge_definitions") or []:
        if not isinstance(ed, dict):
            continue
        col = ed.get("collection")
        if not isinstance(col, str) or not col:
            continue
        out.append(ed)
    return out


def graph_collections(graph: dict[str, Any]) -> tuple[set[str], set[str]]:
    """``(vertex collections, edge collections)`` of a snapshot graph entry.

    Vertices are every edge definition's ``from`` / ``to`` plus the orphan
    collections; edges are the edge definitions' collections. Non-string and
    empty names are dropped on both sides.
    """
    vertices: set[str] = set()
    edges: set[str] = set()
    for ed in iter_edge_definitions(graph):
        edges.add(str(ed["collection"]))
        for side in ("from", "to"):
            vals = ed.get(side)
            if isinstance(vals, list):
                vertices.update(v for v in vals if isinstance(v, str) and v)
    orphans = graph.get("orphan_collections")
    if isinstance(orphans, list):
        vertices.update(v for v in orphans if isinstance(v, str) and v)
    return vertices, edges


def index_edge_definitions_by_collection(
    graphs: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Build a deterministic ``{edge_collection_name: edge_def}`` index
    over every named graph in ``graphs``.

    When two graphs declare the same edge collection the
    lexicographically first graph name wins (baseline endpoint
    enrichment relies on this determinism).
    """
    out: dict[str, dict[str, Any]] = {}
    if not graphs:
        return out
    dicts = [g for g in graphs if isinstance(g, dict)]
    for g in sorted(dicts, key=lambda x: str(x.get("name") or "")):
        for ed in iter_edge_definitions(g):
            out.setdefault(str(ed["collection"]), ed)
    return out


def observed_field_names(entry: dict[str, Any]) -> set[str]:
    """Union of field names observed on a snapshot collection entry (PG
    ``fields`` or LPG ``by_type`` groupings)."""
    observed = entry.get("observed_fields")
    names: set[str] = set()
    if not isinstance(observed, dict):
        return names
    fields = observed.get("fields")
    if isinstance(fields, list):
        names.update(f for f in fields if isinstance(f, str))
    by_type = observed.get("by_type")
    if isinstance(by_type, dict):
        for field_list in by_type.values():
            if isinstance(field_list, list):
                names.update(f for f in field_list if isinstance(f, str))
    return names


def collections_by_name(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The snapshot's collection entries keyed by name (entries without a
    string name are skipped)."""
    out: dict[str, dict[str, Any]] = {}
    for c in snapshot.get("collections") or []:
        if isinstance(c, dict) and isinstance(c.get("name"), str):
            out[c["name"]] = c
    return out


def meta_get(meta: Any, *keys: str) -> Any:
    """The first non-``None`` value of ``keys`` in ``meta``, else ``None``.

    For metadata read under its camelCase name with a snake_case fallback, as in a
    hand-built dict that uses the model's field names.
    """
    for k in keys:
        if isinstance(meta, dict) and meta.get(k) is not None:
            return meta[k]
    return None


def normalize_analysis_dict(analysis: Any) -> dict[str, Any]:
    """
    Coerce an ``AnalysisResult`` (pydantic model) or already-serialized
    dict into a plain ``dict`` and normalize the snake_case vs camelCase
    aliases under the ``conceptual_schema`` / ``physical_mapping`` keys.

    Returns a shallow copy where:

    * ``conceptualSchema`` is always present (sourced from
      ``conceptual_schema`` if needed) — and ``conceptual_schema`` is
      removed once it has been hoisted.
    * ``physicalMapping`` is always present (sourced from
      ``physical_mapping`` if needed) — and ``physical_mapping`` is
      removed once it has been hoisted.

    Centralises the conversion the OWL / docs / cypher exporters used to
    repeat (``analysis.model_dump() if hasattr(...) else analysis`` plus
    ``data.get("conceptual_schema") or data.get("conceptualSchema")``).
    """
    if hasattr(analysis, "model_dump"):
        # By alias, so a model's metadata has the same camelCase keys as the contract
        # (``healthScore``, not ``health_score``) and readers need only one spelling.
        data = dict(analysis.model_dump(by_alias=True))
    elif isinstance(analysis, dict):
        data = dict(analysis)
    else:
        raise TypeError(f"normalize_analysis_dict: unsupported type {type(analysis).__name__}")

    if "conceptualSchema" not in data and "conceptual_schema" in data:
        data["conceptualSchema"] = data.pop("conceptual_schema")
    elif "conceptual_schema" in data:
        data.pop("conceptual_schema")

    if "physicalMapping" not in data and "physical_mapping" in data:
        data["physicalMapping"] = data.pop("physical_mapping")
    elif "physical_mapping" in data:
        data.pop("physical_mapping")

    return data


def llm_cache_segment(*, provider: str, model: str, prompt_version: str | None, system_prompt: str) -> str:
    """
    The LLM-specific part of an analysis cache key.

    Everything that changes what the model is asked, or which model answers, belongs
    here: provider, model, prompt version and the effective system prompt. Without
    provider and model, switching ``--model`` (or provider) on an unchanged schema
    returned the previous model's cached answer. The provider is lower-cased so
    ``"OpenAI"`` and ``"openai"`` share entries, as they share a provider.
    """
    return "\x00".join((provider.strip().lower(), model.strip(), prompt_version or "", system_prompt))


def analysis_cache_storage_key(physical_fingerprint: str, *, llm_cache_segment: str | None) -> str:
    """
    Filesystem-safe cache filename stem.

    Baseline / no-LLM analysis is keyed only by the physical schema fingerprint.
    LLM runs also incorporate the segment built by :func:`llm_cache_segment`
    (provider, model, prompt version and effective system prompt) so cache
    entries do not collide when any of those differ.
    """
    if not llm_cache_segment:
        return physical_fingerprint
    return sha256_hex(f"{physical_fingerprint}\n{llm_cache_segment}")


def singularize(name: str) -> str:
    """Best-effort English singularization for collection/entity names.

    Delegates to :func:`schema_analyzer.csi.naming.singularize`, so FK
    inference, type detection, quality checks and CSI class names agree. The
    two used to be separate copies that disagreed (this one turned ``buses``
    into ``buse``; the other turned ``warehouses`` into ``warehous``;
    arango-solutions-org/r2g-arango#30). Surrounding whitespace is ignored.
    """
    from .csi.naming import singularize as _singularize

    return _singularize(name.strip())


def pascal_case(name: str) -> str:
    """Convert snake_case, kebab-case, or space-separated name to PascalCase."""
    parts = [p for p in str(name).replace("-", "_").replace(" ", "_").split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Unknown"


def extract_first_json_object(text: str) -> str:
    """
    Extract the first top-level JSON object from a string.
    Works with model outputs that include preamble/postamble.
    """
    if not isinstance(text, str):
        raise ValueError("text must be a string")

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object start found")

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    raise ValueError("Unterminated JSON object")
