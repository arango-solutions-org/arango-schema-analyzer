from __future__ import annotations

import copy
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable

    from arango.database import StandardDatabase

from .baseline import infer_baseline_from_snapshot, type_value_caps_from_snapshot
from .cache import AnalysisCache, cache_from_config
from .conceptual import ConceptualSchema
from .defaults import (
    BASELINE_NO_LLM_CONFIDENCE,
    CONFIDENCE_BASE,
    CONFIDENCE_FLOOR,
    CONFIDENCE_MAX_PENALTY,
    CONFIDENCE_WARNING_PENALTY,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_REVIEW_THRESHOLD,
    DEFAULT_TIMEOUT_MS,
    MAX_REPAIR_ATTEMPTS,
    MIN_LLM_BUDGET_MS,
    SAMPLE_VALUE_TOP_K,
)
from .domain_detect import DomainHint, detect_domain, domain_hint_from_context
from .enrichment import (
    _apply_collection_name_allowlist,
    _apply_graph_membership,
    _apply_graphrag,
    _apply_multitenancy,
    _apply_rdf_topology,
    _apply_reconciliation,
    _apply_shard_families,
    _apply_sharding_profile,
    _apply_statistics,
    _apply_tenant_scope,
    _apply_vci,
    _arango_product_dict_for,
    _arango_product_status_for,
)
from .errors import SchemaAnalyzerError
from .incremental import (
    CHANGE_NO_CACHE,
    CHANGE_SHAPE_CHANGED,
    CHANGE_STATS_CHANGED,
    assess_change_state,
    coerce_prior,
    refresh_statistics,
)
from .mapping import PhysicalMapping
from .provenance import (
    annotate_provenance,
    carry_forward_first_seen,
    compute_valid_time,
    prior_valid_from,
    stamp_temporal_provenance,
)
from .providers import create_provider, get_default_model, get_provider_env_var
from .quality import build_quality_block
from .redaction import (
    RedactionOptions,
    build_field_name_map,
    redact_snapshot_for_egress,
    unmask_field_names,
)
from .snapshot import (
    fingerprint_physical_counts,
    fingerprint_physical_schema,
    fingerprint_physical_shape,
    snapshot_physical_schema,
)
from .types import AnalysisMetadata, AnalysisResult, now_iso
from .utils import analysis_cache_storage_key, llm_cache_segment, stable_dumps
from .workflow import async_generate_validate_repair, run_generate_validate_repair

logger = logging.getLogger(__name__)


_PROVENANCE_CACHE_STRIP = (
    "run_id",
    "analysis_started_at",
    "analysis_completed_at",
    "physical_schema_fingerprint",
    "cache_hit",
    "prompt_version",
    # Per-run bitemporal stamps (§3.13.5) — re-stamped on every cache hit by
    # _stamp_metadata, so they must not be frozen into the cached payload.
    "transaction_time",
    "valid_time",
    "valid_time_source",
    "predecessor_fingerprint",
)


def _strip_provenance_for_cache(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    md = dict(out.get("metadata") or {})
    for k in _PROVENANCE_CACHE_STRIP:
        md.pop(k, None)
    out["metadata"] = md
    return out


def _is_cached_fallback(cached: dict[str, Any]) -> bool:
    """True for a cache entry holding a baseline fallback (``metadata.used_baseline``).

    Only meaningful under an LLM key: no-LLM entries are always baseline output.
    Both the field name and its alias are checked because ``model_dump`` writes
    field names, but a hand-edited entry may use the alias.
    """
    md = cached.get("metadata")
    if not isinstance(md, dict):
        return False
    return bool(md.get("used_baseline") or md.get("usedBaseline"))


def _default_system_prompt() -> str:
    return (
        "You are a schema analysis engine. Return ONLY a single JSON object matching the provided schema. "
        "Do not include any markdown fences, explanations, or extra text."
    )


def _bound_snapshot_value_counts(snapshot: dict[str, Any], top_k: int) -> dict[str, Any]:
    """Return a copy of ``snapshot`` with each collection's
    ``sample_field_value_counts`` truncated to the ``top_k`` highest-count values.

    Used only to bound the LLM prompt in full-label-set mode: the deterministic
    baseline path keeps the entire label vocabulary, but the model prompt must
    stay within budget, so it sees only the top-K values (already sorted by
    count DESC). The original snapshot is never mutated.
    """
    cols = snapshot.get("collections")
    if not isinstance(cols, list):
        return snapshot
    bounded_cols: list[Any] = []
    for col in cols:
        vc = col.get("sample_field_value_counts") if isinstance(col, dict) else None
        if isinstance(vc, dict) and any(isinstance(v, list) and len(v) > top_k for v in vc.values()):
            new_col = dict(col)
            new_col["sample_field_value_counts"] = {
                f: (items[:top_k] if isinstance(items, list) else items) for f, items in vc.items()
            }
            bounded_cols.append(new_col)
        else:
            bounded_cols.append(col)
    new_snap = dict(snapshot)
    new_snap["collections"] = bounded_cols
    return new_snap


def _build_prompt(snapshot: dict[str, Any], *, domain_hint: DomainHint | None = None) -> str:
    snapshot_json = stable_dumps(snapshot)

    domain_block = ""
    if domain_hint:
        domain_block = (
            "BUSINESS DOMAIN CONTEXT (auto-detected from schema signals):\n"
            f"{domain_hint.prompt_context()}\n"
            "Use this domain knowledge to choose semantically accurate entity and "
            "relationship names. Prefer domain-standard terminology over generic names.\n\n"
        )

    return (
        "You will be given an ArangoDB physical schema snapshot JSON.\n"
        "Your job: infer a conceptual schema and a conceptual→physical mapping.\n\n"
        + domain_block
        + "Return ONLY a single JSON object with EXACTLY these top-level keys:\n"
        "- conceptualSchema\n"
        "- physicalMapping\n"
        "- metadata\n\n"
        "Required JSON shape (example skeleton; fill it in):\n"
        "{\n"
        '  "conceptualSchema": {\n'
        '    "entities": [{"name":"EntityType","labels":["EntityType"],'
        '"properties":[{"name":"prop","type":"string","indexed":true,"unique":false}]}],\n'
        '    "relationships": [{"type":"REL_TYPE","fromEntity":"EntityType",'
        '"toEntity":"EntityType","properties":[{"name":"prop","type":"string"}]}],\n'
        '    "properties": []\n'
        "  },\n"
        '  "physicalMapping": {\n'
        '    "entities": {"EntityType":{"style":"COLLECTION","collectionName":"collection",'
        '"indexes":[{"type":"persistent","fields":["prop"],"unique":false}],'
        '"properties":{"prop":{"field":"prop","indexed":true}}}},\n'
        '    "relationships": {"REL_TYPE":{"style":"DEDICATED_COLLECTION","edgeCollectionName":"edges",'
        '"indexes":[],"properties":{}}}\n'
        "  },\n"
        '  "metadata": {\n'
        '    "confidence": 0.0,\n'
        '    "timestamp": "ISO-8601 string",\n'
        '    "analyzedCollectionCounts": {"documentCollections": 0, "edgeCollections": 0},\n'
        '    "detectedPatterns": [],\n'
        '    "warnings": [],\n'
        '    "assumptions": []\n'
        "  }\n"
        "}\n\n"
        "Mapping styles vocabulary:\n"
        "- Entity mapping style: COLLECTION | LABEL\n"
        "- Relationship mapping style: DEDICATED_COLLECTION | GENERIC_WITH_TYPE\n\n"
        "Important:\n"
        "- Prefer entity/relationship names that match collection names, "
        "type-field values, and edge collection names found in the snapshot.\n"
        "- Always include non-empty arrays for conceptualSchema.entities "
        "and conceptualSchema.relationships if any are inferable.\n\n"
        "Per-collection entity rule (CRITICAL):\n"
        "- If you see document collections that represent distinct entity "
        "types (i.e. NOT a single generic 'entities' collection with many "
        "type values), then EVERY document collection should become an "
        "entity type.\n"
        "- Use collection.inferred_entity_type as the entity name and "
        "create a physicalMapping.entities entry with style=COLLECTION "
        "and collectionName=<collection.name>.\n\n"
        "Generic edge collection rule (CRITICAL):\n"
        "- If an edge collection has sample_field_value_counts for a "
        "field like 'relation'/'relType'/'type', then EACH DISTINCT "
        "VALUE is a relationship type.\n"
        "- For those, add a conceptualSchema.relationships entry with "
        "type=<value> and add a physicalMapping.relationships entry "
        "mapping that type to GENERIC_WITH_TYPE on that edge collection "
        "and typeField=<field> typeValue=<value>.\n\n"
        "Generic entity collection rule:\n"
        "- If a document collection has sample_field_value_counts for "
        "a field like 'type'/'kind'/'entityType', then EACH DISTINCT "
        "VALUE is an entity type.\n"
        "- For those, add conceptualSchema.entities entries and "
        "physicalMapping.entities entries mapping to LABEL with "
        "typeField/typeValue.\n\n"
        "Property and index mapping rule:\n"
        "- For EACH entity/relationship in physicalMapping, include:\n"
        "  - 'indexes': array of non-primary indexes from the snapshot "
        "(type, fields, unique, sparse, name).\n"
        "  - 'properties': object mapping conceptual property name → "
        "{'field': str, 'indexed': bool, 'unique': bool}.\n"
        "- In conceptualSchema entity/relationship properties, include "
        "'indexed': true and 'unique': true when the field is indexed.\n\n"
        f"PHYSICAL_SCHEMA_SNAPSHOT_JSON:\n{snapshot_json}\n"
    )


def _compute_confidence(errors: list[str], warnings: list[str]) -> float:
    if errors:
        return 0.0
    penalty = min(CONFIDENCE_MAX_PENALTY, CONFIDENCE_WARNING_PENALTY * len(warnings))
    return max(CONFIDENCE_FLOOR, CONFIDENCE_BASE - penalty)


def _api_key_from_env(provider: str) -> str | None:
    env_var = get_provider_env_var(provider)
    return os.environ.get(env_var) if env_var else None


class _AnalysisContext(NamedTuple):
    """Prepared context for LLM analysis workflow."""

    snapshot: dict[str, Any]
    fingerprint: str
    cache_storage_key: str
    provider: Any
    model: str
    remaining_ms: int
    system: str
    prompt: str
    max_repair_attempts: int
    domain_hint: DomainHint | None = None
    # real-name→token map when field-name masking is active; used to un-mask the
    # LLM response so the result carries real field names (PRD §4.3).
    field_name_map: dict[str, str] | None = None
    # baseline entity modeling strategy: "auto" (LPG/PG heuristic), "collection"
    # (force one entity per collection — correct for pure property-graph schemas) or
    # "labels" (as "auto", but label arrays map to LABELS-style entities).
    entity_strategy: str = "auto"


@dataclass
class _ProvenanceStamp:
    run_id: str
    started_at: str
    # Cheap change-detection fingerprints, filled in during _prepare_analysis
    # (§3.13.3) and stamped onto the result metadata by _stamp_metadata.
    shape_fingerprint: str | None = None
    counts_fingerprint: str | None = None


@dataclass
class AgenticSchemaAnalyzer:
    llm_provider: Literal["openai", "anthropic", "openrouter"] | str | None = None
    api_key: str | None = None
    model: str | None = None
    cache: AnalysisCache | dict[str, Any] | None = None
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    review_threshold: float = DEFAULT_REVIEW_THRESHOLD
    system_prompt: str | None = None
    prompt_version: str | None = None
    max_repair_attempts: int | None = None
    redaction: RedactionOptions | None = None
    # Optional gold reference (domain-pack-style dict with entities/relationships)
    # for precision/recall scoring of the conceptual schema (PRD §3.12.3). When
    # set, metadata.qualityMetrics gains a ``gold`` block and its overlap folds
    # into the health score.
    gold_reference: dict[str, Any] | None = None
    # Optional caller-supplied domain context (PRD §4.7): a domain name string
    # or dict {domain, description, entities, relationships}. When set it
    # overrides automatic domain detection for LLM prompt priors.
    domain_context: dict[str, Any] | str | None = None
    # ── Relational-pattern and taxonomy enrichment (PRD §6.2 / §6.3) ──────────
    # Detect relationships carried by a scalar attribute rather than an edge collection.
    # Candidate generation is snapshot-only; ``sample_fk_overlap`` additionally confirms
    # each candidate with a value-containment probe, which is the first cross-collection
    # database cost here and therefore separately gated.
    detect_foreign_keys: bool = False
    sample_fk_overlap: bool = False
    # Curator-declared attribute references (``{"references": [...]}``), verified against
    # the database by full containment and merged as declared relationships. Unlike the
    # enrichments above, a declaration that does not hold fails the analysis (it is a
    # statement about the data, not a guess). See ``declared_references``.
    declared_references: dict[str, Any] | None = None
    # Discover class abstractions (rdfs:subClassOf) via the shared conceptual-taxonomy
    # library. Deterministic and snapshot-only unless ``measure_key_containment`` is set,
    # which probes for the Arango analogue of class-table inheritance.
    discover_taxonomy: bool = False
    measure_key_containment: bool = False

    def __post_init__(self) -> None:
        # Set for the duration of an analysis so enrichment can probe; the analyzer itself
        # stays stateless between runs.
        self._db: Any = None
        if isinstance(self.cache, dict) or self.cache is None:
            self.cache = cache_from_config(self.cache if isinstance(self.cache, dict) else None)

    def _effective_system_prompt(self) -> str:
        return self.system_prompt if self.system_prompt else _default_system_prompt()

    def _repair_limit(self) -> int:
        return self.max_repair_attempts if self.max_repair_attempts is not None else MAX_REPAIR_ATTEMPTS

    def _stamp_metadata(
        self,
        meta: AnalysisMetadata,
        *,
        prov: _ProvenanceStamp,
        physical_fingerprint: str,
        cache_hit: bool,
    ) -> AnalysisMetadata:
        completed = now_iso()
        # Bitemporal stamping (PRD §3.13.5). This is the non-incremental path (no
        # prior run threaded), so valid time is `observed` at completion; the
        # incremental path (`analyze_incremental` / `refresh_statistics`) overrides
        # with fingerprint-continuity when a prior run matches.
        valid_time, valid_time_source, predecessor = compute_valid_time(
            completed_at=completed,
            current_shape_fingerprint=prov.shape_fingerprint,
        )
        return meta.model_copy(
            update={
                "run_id": prov.run_id,
                "analysis_started_at": prov.started_at,
                "analysis_completed_at": completed,
                "transaction_time": completed,
                "physical_schema_fingerprint": physical_fingerprint,
                "shape_fingerprint": prov.shape_fingerprint,
                "counts_fingerprint": prov.counts_fingerprint,
                "valid_time": valid_time,
                "valid_time_source": valid_time_source,
                "predecessor_fingerprint": predecessor,
                "cache_hit": cache_hit,
                "prompt_version": self.prompt_version,
            }
        )

    def _prepare_analysis(
        self,
        db: StandardDatabase,
        *,
        prov: _ProvenanceStamp,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        sample_limit_per_collection: int = 0,
        include_samples_in_snapshot: bool = False,
        use_cache: bool = True,
        graph_scope: str | None = None,
        entity_strategy: Literal["auto", "collection", "labels"] = "auto",
        max_entity_types: int | None = None,
        min_type_value_count: int = 0,
        _snapshot: dict[str, Any] | None = None,
    ) -> AnalysisResult | _AnalysisContext:
        """Shared setup for sync and async analysis paths.

        Returns an ``AnalysisResult`` on cache hit or no-provider baseline,
        or an ``_AnalysisContext`` with prepared values for the LLM workflow.
        """
        started = time.time()

        snapshot = _snapshot or snapshot_physical_schema(
            db,
            sample_limit_per_collection=sample_limit_per_collection,
            include_samples_in_snapshot=include_samples_in_snapshot,
            graph_scope=graph_scope,
            sample_value_top_k=max_entity_types,
            min_type_value_count=min_type_value_count,
        )
        snapshot["generated_at"] = now_iso()
        fingerprint = fingerprint_physical_schema(snapshot, include_samples=False)

        # Cheap change-detection probes (§3.13.3), stamped onto the result so a
        # later incremental re-probe can derive the change state without a full
        # snapshot. Best-effort: degrade to None if the DB handle can't answer.
        try:
            prov.shape_fingerprint = fingerprint_physical_shape(db)
            prov.counts_fingerprint = fingerprint_physical_counts(db)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("shape/counts fingerprint probe failed: %s", exc)

        api_key = self.api_key or (_api_key_from_env(self.llm_provider) if self.llm_provider else None)
        use_llm = bool(self.llm_provider and api_key)
        system_effective = self._effective_system_prompt()
        llm_segment: str | None = None
        model: str | None = None
        if use_llm:
            assert self.llm_provider is not None  # guaranteed by use_llm
            # Resolved before the cache lookup so the key names the model that would
            # answer — a cached answer from another model must not be returned.
            model = self.model or get_default_model(self.llm_provider)
            llm_segment = llm_cache_segment(
                provider=self.llm_provider,
                model=model,
                prompt_version=self.prompt_version,
                system_prompt=system_effective,
            )
        cache_storage_key = analysis_cache_storage_key(fingerprint, llm_cache_segment=llm_segment)

        if use_cache and self.cache is not None:
            cached = self.cache.get(cache_storage_key)
            if cached and use_llm and _is_cached_fallback(cached):
                # Fallbacks are no longer written under an LLM key, and those cached by
                # older releases sit under keys that no longer match (provider and model
                # joined the key). Should one be found anyway, retry the model rather
                # than replay the baseline; a successful answer then overwrites it.
                logger.info("Ignoring cached baseline fallback for key prefix %s", cache_storage_key[:16])
                cached = None
            if cached:
                logger.info("Cache hit for key prefix %s", cache_storage_key[:16])
                parsed = AnalysisResult.model_validate(cached)
                stamped = self._stamp_metadata(
                    parsed.metadata,
                    prov=prov,
                    physical_fingerprint=fingerprint,
                    cache_hit=True,
                )
                return AnalysisResult(
                    conceptual_schema=parsed.conceptual_schema,
                    physical_mapping=parsed.physical_mapping,
                    metadata=stamped,
                )

        domain_hint = domain_hint_from_context(self.domain_context) or detect_domain(snapshot)
        if domain_hint:
            source = "provided" if "caller-provided" in domain_hint.matched_signals else "detected"
            logger.info("Domain %s=%s (confidence=%.2f)", source, domain_hint.domain, domain_hint.confidence)

        if not use_llm:
            logger.info("No LLM provider configured; falling back to baseline inference")
            doc_count = sum(1 for c in snapshot.get("collections", []) if c.get("type") == "document")
            edge_count = sum(1 for c in snapshot.get("collections", []) if c.get("type") == "edge")
            baseline = infer_baseline_from_snapshot(
                snapshot, collection_per_entity=entity_strategy == "collection", multi_label=entity_strategy == "labels"
            )
            stats_holder: dict[str, Any] = {
                "physicalMapping": baseline.get("physicalMapping", {}),
                "conceptualSchema": baseline.get("conceptualSchema", {}),
                "metadata": {"detectedPatterns": list(baseline.get("detectedPatterns", []))},
            }
            _apply_sharding_profile(stats_holder, snapshot)
            _apply_shard_families(stats_holder)
            _apply_multitenancy(stats_holder, snapshot)
            _apply_vci(stats_holder, snapshot)
            _apply_rdf_topology(stats_holder, snapshot)
            _apply_graphrag(stats_holder, snapshot)
            _apply_graph_membership(stats_holder, snapshot)
            _apply_statistics(db, stats_holder, snapshot)
            # Enrichment must run on this path too. It is the *default* path — no LLM
            # provider configured — so hooking only the LLM path in `_build_result` would
            # leave the capability unreachable for most callers.
            baseline_fk_status = self._detect_attribute_relationships(stats_holder, snapshot)
            baseline_declared_status = self._apply_declared_references(stats_holder)
            baseline_taxonomy_status = self._discover_taxonomy(stats_holder)

            baseline_conceptual = ConceptualSchema.from_json(stats_holder.get("conceptualSchema", {})).to_json()
            baseline_physical = PhysicalMapping.from_json(stats_holder.get("physicalMapping", {})).to_json()
            # ConceptualSchema keeps only entities/relationships/properties, so the
            # abstraction blocks are carried across explicitly.
            for key in ("abstractClasses", "subClassOfProposals"):
                if stats_holder.get("conceptualSchema", {}).get(key):
                    baseline_conceptual[key] = stats_holder["conceptualSchema"][key]
            baseline_payload = {
                "conceptualSchema": baseline_conceptual,
                "physicalMapping": baseline_physical,
                "metadata": {},
            }
            annotate_provenance(baseline_payload, used_baseline=True)
            stamp_temporal_provenance(baseline_payload, now=now_iso())
            baseline_quality, baseline_health = build_quality_block(
                baseline_conceptual, baseline_physical, snapshot, BASELINE_NO_LLM_CONFIDENCE, self.gold_reference
            )
            meta = AnalysisMetadata(
                confidence=BASELINE_NO_LLM_CONFIDENCE,
                timestamp=now_iso(),
                analyzed_collection_counts={"documentCollections": doc_count, "edgeCollections": edge_count},
                detected_patterns=list(stats_holder.get("metadata", {}).get("detectedPatterns", [])),
                entity_type_caps=baseline.get("entityTypeCaps") or None,
                relationship_type_caps=baseline.get("relationshipTypeCaps") or None,
                warnings=["LLM provider not configured; returning deterministic baseline inference"],
                foreign_key_status=baseline_fk_status,
                declared_reference_status=baseline_declared_status,
                taxonomy_status=baseline_taxonomy_status,
                assumptions=[],
                review_required=True,
                provider=str(self.llm_provider) if self.llm_provider else None,
                model=None,
                repair_attempts=0,
                used_baseline=True,
                detected_domain=domain_hint.domain if domain_hint else None,
                detected_domain_confidence=domain_hint.confidence if domain_hint else None,
                statistics=stats_holder["metadata"].get("statistics"),
                statistics_status=stats_holder["metadata"].get("statistics_status"),
                sharding_profile=stats_holder["metadata"].get("shardingProfile"),
                sharding_profile_status=stats_holder["metadata"].get("shardingProfileStatus"),
                multitenancy=stats_holder["metadata"].get("multitenancy"),
                multitenancy_status=stats_holder["metadata"].get("multitenancyStatus"),
                vci=stats_holder["metadata"].get("vci"),
                rdf_topology=stats_holder["metadata"].get("rdfTopology"),
                graph_rag=stats_holder["metadata"].get("graphRag"),
                graph_membership=stats_holder["metadata"].get("graphMembership"),
                arango_product=_arango_product_dict_for(snapshot),
                arango_product_status=_arango_product_status_for(snapshot),
                quality_metrics=baseline_quality,
                health_score=baseline_health,
            )
            meta = self._stamp_metadata(meta, prov=prov, physical_fingerprint=fingerprint, cache_hit=False)
            result = AnalysisResult(
                conceptual_schema=baseline_conceptual,
                physical_mapping=baseline_physical,
                metadata=meta,
            )
            if use_cache and isinstance(self.cache, AnalysisCache):
                self.cache.set(
                    cache_storage_key,
                    _strip_provenance_for_cache(result.model_dump()),
                    ttl_seconds=self.cache_ttl_seconds,
                )
            return result

        logger.info("Using LLM provider=%s", self.llm_provider)
        assert self.llm_provider is not None and api_key is not None and model is not None  # guaranteed by use_llm
        provider = create_provider(self.llm_provider, api_key=api_key)

        elapsed_ms = int((time.time() - started) * 1000)
        remaining = max(MIN_LLM_BUDGET_MS, timeout_ms - elapsed_ms)

        field_name_map: dict[str, str] | None = None
        if self.redaction is not None and self.redaction.mask_field_names:
            cols = snapshot.get("collections")
            field_name_map = build_field_name_map(cols) if isinstance(cols, list) else {}
        egress_snapshot = snapshot
        if min_type_value_count and min_type_value_count > 0:
            # The baseline path (above) consumed the full label set; the LLM
            # prompt must stay bounded, so feed the model only the top-K values.
            egress_snapshot = _bound_snapshot_value_counts(snapshot, SAMPLE_VALUE_TOP_K)
        prompt = _build_prompt(
            redact_snapshot_for_egress(egress_snapshot, self.redaction, field_name_map=field_name_map),
            domain_hint=domain_hint,
        )

        return _AnalysisContext(
            snapshot=snapshot,
            fingerprint=fingerprint,
            cache_storage_key=cache_storage_key,
            provider=provider,
            model=model,
            remaining_ms=remaining,
            system=system_effective,
            prompt=prompt,
            max_repair_attempts=self._repair_limit(),
            domain_hint=domain_hint,
            field_name_map=field_name_map,
            entity_strategy=entity_strategy,
        )

    def analyze_physical_schema(
        self,
        db: StandardDatabase,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        sample_limit_per_collection: int = 0,
        include_samples_in_snapshot: bool = False,
        use_cache: bool = True,
        graph_scope: str | None = None,
        entity_strategy: Literal["auto", "collection", "labels"] = "auto",
        max_entity_types: int | None = None,
        min_type_value_count: int = 0,
        _snapshot: dict[str, Any] | None = None,
    ) -> AnalysisResult:
        prov = _ProvenanceStamp(run_id=str(uuid.uuid4()), started_at=now_iso())
        self._db = db
        prep = self._prepare_analysis(
            db,
            prov=prov,
            timeout_ms=timeout_ms,
            sample_limit_per_collection=sample_limit_per_collection,
            include_samples_in_snapshot=include_samples_in_snapshot,
            use_cache=use_cache,
            graph_scope=graph_scope,
            entity_strategy=entity_strategy,
            max_entity_types=max_entity_types,
            min_type_value_count=min_type_value_count,
            _snapshot=_snapshot,
        )
        if isinstance(prep, AnalysisResult):
            return prep

        errors: list[str] = []
        warnings: list[str] = []
        try:
            wf = run_generate_validate_repair(
                provider=prep.provider,
                model=prep.model,
                system=prep.system,
                prompt=prep.prompt,
                timeout_ms=prep.remaining_ms,
                max_repair_attempts=prep.max_repair_attempts,
            )
            data = wf.data
            repair_attempts = wf.repair_attempts
            if prep.field_name_map:
                data = unmask_field_names(data, prep.field_name_map)
            _apply_collection_name_allowlist(data, prep.snapshot, warnings)
            _apply_reconciliation(data, prep.snapshot, warnings)
        except SchemaAnalyzerError as e:
            logger.warning("LLM workflow failed, falling back to baseline: %s", e)
            baseline = infer_baseline_from_snapshot(
                prep.snapshot,
                collection_per_entity=prep.entity_strategy == "collection",
                multi_label=prep.entity_strategy == "labels",
            )
            data = {
                "conceptualSchema": baseline.get("conceptualSchema", {}),
                "physicalMapping": baseline.get("physicalMapping", {}),
                "metadata": {
                    "warnings": [str(e)],
                    "detectedPatterns": baseline.get("detectedPatterns", []),
                },
            }
            warnings.append("LLM workflow failed; returning deterministic baseline inference")
            errors.append(str(e))
            repair_attempts = 0

        _apply_sharding_profile(data, prep.snapshot)
        _apply_shard_families(data)
        _apply_multitenancy(data, prep.snapshot)
        _apply_vci(data, prep.snapshot)
        _apply_rdf_topology(data, prep.snapshot)
        _apply_graphrag(data, prep.snapshot)
        _apply_graph_membership(data, prep.snapshot)
        _apply_tenant_scope(data)
        _apply_statistics(db, data, prep.snapshot)

        return self._build_result(
            snapshot=prep.snapshot,
            data=data,
            model=prep.model,
            errors=errors,
            warnings=warnings,
            repair_attempts=repair_attempts,
            fingerprint=prep.fingerprint,
            cache_storage_key=prep.cache_storage_key,
            use_cache=use_cache,
            prov=prov,
            domain_hint=prep.domain_hint,
            entity_strategy=prep.entity_strategy,
        )

    def analyze_incremental(
        self,
        db: StandardDatabase,
        *,
        prior: AnalysisResult | dict[str, Any] | None = None,
        exclude_collections: Iterable[str] | None = None,
        **analyze_kwargs: Any,
    ) -> AnalysisResult:
        """Analyze only as much as the schema change warrants (PRD §3.13.3).

        Given a ``prior`` result (carrying ``metadata.shapeFingerprint`` /
        ``countsFingerprint``), cheaply probe the database and:

        * ``shape_changed`` / ``no_cache`` → run a full ``analyze_physical_schema``
          (forwarding ``analyze_kwargs``);
        * ``stats_changed`` → recompute only the statistics block, preserving the
          cached conceptual schema + physical mapping;
        * ``unchanged`` → return the prior result annotated ``incrementalRefresh
          = "unchanged"``.

        With no ``prior``, this is just a full analysis.
        """
        if prior is None:
            return self.analyze_physical_schema(db, **analyze_kwargs)

        pr = coerce_prior(prior)
        state = assess_change_state(
            db,
            prior_shape=pr.metadata.shape_fingerprint,
            prior_counts=pr.metadata.counts_fingerprint,
            exclude_collections=exclude_collections,
        )
        status = state["status"]
        logger.info("Incremental analysis change-state: %s", status)

        if status in (CHANGE_SHAPE_CHANGED, CHANGE_NO_CACHE):
            result = self.analyze_physical_schema(db, **analyze_kwargs)
            # Elements that survived the schema change keep the firstSeenAt of
            # the run that first discovered them (PRD §3.13.2).
            carry_forward_first_seen(
                {"conceptualSchema": result.conceptual_schema, "physicalMapping": result.physical_mapping},
                {"conceptualSchema": pr.conceptual_schema, "physicalMapping": pr.physical_mapping},
            )
            # §3.13.5: the shape changed (or there was no prior fingerprint), so valid
            # time stays `observed` (the full analysis already stamped it); record the
            # predecessor shape fingerprint so consumers can link the two versions.
            return AnalysisResult(
                conceptual_schema=result.conceptual_schema,
                physical_mapping=result.physical_mapping,
                metadata=result.metadata.model_copy(update={"predecessor_fingerprint": pr.metadata.shape_fingerprint}),
            )
        if status == CHANGE_STATS_CHANGED:
            return refresh_statistics(db, pr)

        # unchanged — the fingerprint match just revalidated every element.
        # Stamp copies so the caller's ``prior`` is never mutated in place.
        completed = now_iso()
        conceptual = copy.deepcopy(pr.conceptual_schema)
        physical = copy.deepcopy(pr.physical_mapping)
        stamp_temporal_provenance({"conceptualSchema": conceptual, "physicalMapping": physical}, now=completed)
        # §3.13.5: shape fingerprint matches the prior run, so valid time is carried
        # back along the unbroken chain (fingerprint-continuity).
        valid_time, valid_time_source, predecessor = compute_valid_time(
            completed_at=completed,
            current_shape_fingerprint=pr.metadata.shape_fingerprint,
            prior_shape_fingerprint=pr.metadata.shape_fingerprint,
            prior_valid_from=prior_valid_from(pr.metadata),
        )
        meta = pr.metadata.model_copy(
            update={
                "incremental_refresh": "unchanged",
                "cache_hit": True,
                "analysis_completed_at": completed,
                "transaction_time": completed,
                "valid_time": valid_time,
                "valid_time_source": valid_time_source,
                "predecessor_fingerprint": predecessor,
            }
        )
        return AnalysisResult(
            conceptual_schema=conceptual,
            physical_mapping=physical,
            metadata=meta,
        )

    async def analyze_physical_schema_async(
        self,
        db: StandardDatabase,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        sample_limit_per_collection: int = 0,
        include_samples_in_snapshot: bool = False,
        use_cache: bool = True,
        graph_scope: str | None = None,
        entity_strategy: Literal["auto", "collection", "labels"] = "auto",
        max_entity_types: int | None = None,
        min_type_value_count: int = 0,
        _snapshot: dict[str, Any] | None = None,
    ) -> AnalysisResult:
        """Async version of analyze_physical_schema. Requires provider with agenerate()."""
        prov = _ProvenanceStamp(run_id=str(uuid.uuid4()), started_at=now_iso())
        self._db = db
        prep = self._prepare_analysis(
            db,
            prov=prov,
            timeout_ms=timeout_ms,
            sample_limit_per_collection=sample_limit_per_collection,
            include_samples_in_snapshot=include_samples_in_snapshot,
            use_cache=use_cache,
            graph_scope=graph_scope,
            entity_strategy=entity_strategy,
            max_entity_types=max_entity_types,
            min_type_value_count=min_type_value_count,
            _snapshot=_snapshot,
        )
        if isinstance(prep, AnalysisResult):
            return prep

        errors: list[str] = []
        warnings: list[str] = []
        try:
            wf = await async_generate_validate_repair(
                provider=prep.provider,
                model=prep.model,
                system=prep.system,
                prompt=prep.prompt,
                timeout_ms=prep.remaining_ms,
                max_repair_attempts=prep.max_repair_attempts,
            )
            data = wf.data
            repair_attempts = wf.repair_attempts
            if prep.field_name_map:
                data = unmask_field_names(data, prep.field_name_map)
            _apply_collection_name_allowlist(data, prep.snapshot, warnings)
            _apply_reconciliation(data, prep.snapshot, warnings)
        except SchemaAnalyzerError as e:
            logger.warning("Async LLM workflow failed, falling back to baseline: %s", e)
            baseline = infer_baseline_from_snapshot(
                prep.snapshot,
                collection_per_entity=prep.entity_strategy == "collection",
                multi_label=prep.entity_strategy == "labels",
            )
            data = {
                "conceptualSchema": baseline.get("conceptualSchema", {}),
                "physicalMapping": baseline.get("physicalMapping", {}),
                "metadata": {
                    "warnings": [str(e)],
                    "detectedPatterns": baseline.get("detectedPatterns", []),
                },
            }
            warnings.append("LLM workflow failed; returning deterministic baseline inference")
            errors.append(str(e))
            repair_attempts = 0

        _apply_sharding_profile(data, prep.snapshot)
        _apply_shard_families(data)
        _apply_multitenancy(data, prep.snapshot)
        _apply_vci(data, prep.snapshot)
        _apply_rdf_topology(data, prep.snapshot)
        _apply_graphrag(data, prep.snapshot)
        _apply_graph_membership(data, prep.snapshot)
        _apply_tenant_scope(data)
        _apply_statistics(db, data, prep.snapshot)

        return self._build_result(
            snapshot=prep.snapshot,
            data=data,
            model=prep.model,
            errors=errors,
            warnings=warnings,
            repair_attempts=repair_attempts,
            fingerprint=prep.fingerprint,
            cache_storage_key=prep.cache_storage_key,
            use_cache=use_cache,
            prov=prov,
            domain_hint=prep.domain_hint,
            entity_strategy=prep.entity_strategy,
        )

    def _detect_attribute_relationships(self, data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """Relationships carried by a scalar attribute rather than an edge collection.

        Off unless asked for: candidate generation is snapshot-only and cheap, but value
        containment probing is the first cross-collection database cost in this analyzer
        (PRD §6.2), so ``detect_foreign_keys`` gates both and the probe is separately gated
        by having a database handle.
        """
        if not self.detect_foreign_keys:
            return None
        from .fk_inference import InferenceOptions, apply_to_analysis
        from .fk_sampler import ArangoValueSampler

        sampler = None
        options = InferenceOptions()
        if self._db is not None and self.sample_fk_overlap:
            sampler = ArangoValueSampler(self._db)
            options = InferenceOptions(sample_overlap=True)

        try:
            status = apply_to_analysis(data, snapshot, sampler=sampler, options=options)
        except Exception as err:  # noqa: BLE001 - enrichment must never fail an analysis
            logger.warning("foreign-key detection failed: %s", err)
            return {"status": "degraded", "reason": str(err)}

        if sampler is not None:
            probe = sampler.status()
            if probe.get("status") == "degraded":
                status.update(probe)
        return status

    def _apply_declared_references(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Verify and merge curator-declared attribute references (issue #27).

        Runs after attribute-reference inference so a declaration can outrank an inferred
        relationship for the same reference. Deliberately not wrapped like the enrichments:
        a declaration that does not hold raises ``DeclaredReferenceError`` rather than
        degrading, because a curator statement that stops being true must announce itself.
        """
        if not self.declared_references:
            return None
        from .declared_references import DeclaredReferenceError, apply_reference_overlay, aql_containment

        if self._db is None:
            raise DeclaredReferenceError(
                "declared references are verified against the database, and no database handle is available"
            )
        return apply_reference_overlay(data, self.declared_references, containment=aql_containment(self._db))

    def _discover_taxonomy(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Class abstractions, via the shared ``conceptual-taxonomy`` library (PRD §6.3)."""
        if not self.discover_taxonomy:
            return None
        from .taxonomy import TAXONOMY_AVAILABLE, discover, merge_into_analysis

        if not TAXONOMY_AVAILABLE:
            return {"status": "unavailable", "reason": "conceptual-taxonomy is not installed"}
        try:
            proposals = discover(
                data,
                db=self._db,
                measure_containment=self.measure_key_containment and self._db is not None,
            )
            merge_into_analysis(data, proposals)
        except Exception as err:  # noqa: BLE001 - enrichment must never fail an analysis
            logger.warning("abstraction discovery failed: %s", err)
            return {"status": "degraded", "reason": str(err)}

        classes = (proposals or {}).get("abstractClasses") or []
        return {"status": "ok", "abstractClasses": len(classes)}

    def _build_result(
        self,
        *,
        snapshot: dict[str, Any],
        data: dict[str, Any],
        model: str,
        errors: list[str],
        warnings: list[str],
        repair_attempts: int,
        fingerprint: str,
        cache_storage_key: str,
        use_cache: bool,
        prov: _ProvenanceStamp,
        domain_hint: DomainHint | None = None,
        entity_strategy: str = "auto",
    ) -> AnalysisResult:
        doc_count = sum(1 for c in snapshot.get("collections", []) if c.get("type") == "document")
        edge_count = sum(1 for c in snapshot.get("collections", []) if c.get("type") == "edge")

        # The LLM only ever saw the top-K discriminator values the snapshot
        # sampled, so value drops apply to this path as much as to baseline
        # inference. Not applicable under entity_strategy="collection"
        # (discriminators are not used at all).
        entity_type_caps: list[dict[str, Any]] = []
        relationship_type_caps: list[dict[str, Any]] = []
        if entity_strategy != "collection":
            entity_type_caps, relationship_type_caps = type_value_caps_from_snapshot(snapshot)

        if errors:
            confidence = 0.0
        else:
            confidence = (
                float(data.get("metadata", {}).get("confidence"))
                if isinstance(data.get("metadata"), dict)
                and isinstance(data.get("metadata", {}).get("confidence"), (int, float))
                else _compute_confidence(errors, warnings)
            )
        confidence = max(0.0, min(1.0, confidence))
        review_required = confidence < self.review_threshold or bool(errors)

        # Relational-pattern and taxonomy enrichment run here because this is where the
        # baseline and LLM paths converge: both produce `data`, and both should gain the
        # same relationships and class hierarchy. Running earlier would mean doing it twice.
        fk_status = self._detect_attribute_relationships(data, snapshot)
        declared_status = self._apply_declared_references(data)
        taxonomy_status = self._discover_taxonomy(data)

        annotate_provenance(data, used_baseline=bool(errors))
        stamp_temporal_provenance(data, now=now_iso())
        conceptual_schema = ConceptualSchema.from_json(
            data.get("conceptualSchema", {}) if isinstance(data.get("conceptualSchema"), dict) else {}
        ).to_json()
        physical_mapping = PhysicalMapping.from_json(
            data.get("physicalMapping", {}) if isinstance(data.get("physicalMapping"), dict) else {}
        ).to_json()
        quality_metrics, health_score = build_quality_block(
            conceptual_schema, physical_mapping, snapshot, confidence, self.gold_reference
        )

        metadata = AnalysisMetadata(
            confidence=confidence,
            timestamp=str(data.get("metadata", {}).get("timestamp") or now_iso()),
            analyzed_collection_counts={"documentCollections": doc_count, "edgeCollections": edge_count},
            detected_patterns=list(data.get("metadata", {}).get("detectedPatterns") or []),
            warnings=list(data.get("metadata", {}).get("warnings") or []) + warnings + errors,
            assumptions=list(data.get("metadata", {}).get("assumptions") or []),
            review_required=review_required,
            provider=str(self.llm_provider).lower() if self.llm_provider else None,
            model=model,
            repair_attempts=int(repair_attempts),
            used_baseline=bool(errors),
            detected_domain=domain_hint.domain if domain_hint else None,
            detected_domain_confidence=domain_hint.confidence if domain_hint else None,
            reconciliation=data.get("metadata", {}).get("reconciliation")
            if isinstance(data.get("metadata"), dict)
            else None,
            entity_type_caps=entity_type_caps or None,
            relationship_type_caps=relationship_type_caps or None,
            statistics=data.get("metadata", {}).get("statistics") if isinstance(data.get("metadata"), dict) else None,
            statistics_status=data.get("metadata", {}).get("statistics_status")
            if isinstance(data.get("metadata"), dict)
            else None,
            tenant_scope_report=data.get("metadata", {}).get("tenantScopeReport")
            if isinstance(data.get("metadata"), dict)
            else None,
            arango_product=_arango_product_dict_for(snapshot),
            arango_product_status=_arango_product_status_for(snapshot),
            sharding_profile=data.get("metadata", {}).get("shardingProfile")
            if isinstance(data.get("metadata"), dict)
            else None,
            sharding_profile_status=data.get("metadata", {}).get("shardingProfileStatus")
            if isinstance(data.get("metadata"), dict)
            else None,
            multitenancy=data.get("metadata", {}).get("multitenancy")
            if isinstance(data.get("metadata"), dict)
            else None,
            multitenancy_status=data.get("metadata", {}).get("multitenancyStatus")
            if isinstance(data.get("metadata"), dict)
            else None,
            foreign_key_status=fk_status,
            declared_reference_status=declared_status,
            taxonomy_status=taxonomy_status,
            vci=data.get("metadata", {}).get("vci") if isinstance(data.get("metadata"), dict) else None,
            rdf_topology=data.get("metadata", {}).get("rdfTopology")
            if isinstance(data.get("metadata"), dict)
            else None,
            graph_rag=data.get("metadata", {}).get("graphRag") if isinstance(data.get("metadata"), dict) else None,
            graph_membership=data.get("metadata", {}).get("graphMembership")
            if isinstance(data.get("metadata"), dict)
            else None,
            quality_metrics=quality_metrics,
            health_score=health_score,
        )
        metadata = self._stamp_metadata(metadata, prov=prov, physical_fingerprint=fingerprint, cache_hit=False)

        result = AnalysisResult(
            conceptual_schema=conceptual_schema,
            physical_mapping=physical_mapping,
            metadata=metadata,
        )

        # A fallback (the LLM failed and the baseline answered) is not written under
        # the model's key: it would be served on every later run until the TTL
        # expired, so a transient provider failure would stop the model being tried.
        if errors:
            logger.info("Not caching a baseline fallback under the LLM cache key")
        elif use_cache and isinstance(self.cache, AnalysisCache):
            logger.debug("Caching result for cache key prefix %s", cache_storage_key[:16])
            self.cache.set(
                cache_storage_key,
                _strip_provenance_for_cache(result.model_dump()),
                ttl_seconds=self.cache_ttl_seconds,
            )

        logger.info(
            "Analysis complete: confidence=%.2f, review_required=%s, repair_attempts=%d",
            confidence,
            review_required,
            repair_attempts,
        )
        return result
