"""Multitenancy detection (PRD §6.2 bullet 4).

Classifies how (or whether) the analysed database encodes
multitenancy and emits a ``metadata.multitenancy`` block. Detection
layers on top of the already-computed ``metadata.shardingProfile``
(:mod:`schema_analyzer.sharding_profile`) — when the snapshot is a
disjoint smart graph, the smart attribute IS the tenant key and we
emit ``style: "disjoint_smartgraph"`` outright. Otherwise we walk down
the priority order:

    1. ``disjoint_smartgraph`` — physical isolation by smart graph.
    2. ``shard_key`` — non-disjoint collections share a tenant-like
       shard key (e.g. ``["tenantId"]``).
    3. ``discriminator_field`` — a candidate property
       (``tenantId`` / ``org_id`` / …) is declared by ≥
       :data:`MIN_TENANT_FIELD_COVERAGE_FRACTION` of analyzed user
       collections, but is NOT a shard key.
    4. ``collection_per_tenant`` — collection naming follows a
       repeated ``<base>__<tenant>`` (or ``<tenant>_<base>``) pattern
       across a consistent set of bases.
    5. ``unknown_single_db`` — the snapshot scope is a single
       database whose own name matches a tenant naming pattern; we
       cannot prove database-per-tenant from one snapshot alone, so
       we flag it for a higher-level orchestrator to confirm.
    6. ``none`` — no signal triggers, multitenancy not detected.

Detection is deterministic and snapshot-only. No DB round-trip beyond
what :func:`schema_analyzer.snapshot.snapshot_physical_schema` already
makes; no LLM call. The LLM layer may *enrich* the human-readable
description but must not change the classification.

The module is a pure function over ``(data, snapshot, sharding_profile)``;
it has no dependency on :mod:`schema_analyzer.analyzer` and is safe
to import from tests and from alternative tooling that builds the
data and snapshot independently.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from .defaults import (
    MAX_TENANT_DISTINCT_VALUES,
    MIN_TENANT_FIELD_COVERAGE_FRACTION,
    TENANT_COLLECTION_NAMING_PATTERNS,
    TENANT_DATABASE_NAMING_PATTERNS,
    TENANT_DISCRIMINATOR_FIELDS,
)
from .utils import entity_property_names

logger = logging.getLogger(__name__)

# Pre-compile the default tenant-collection naming patterns once at import
# time so ``_detect_collection_per_tenant`` does not pay re.compile() cost
# on every call. Callers passing a custom ``patterns`` tuple still get
# per-call compilation (rare path).
_DEFAULT_TENANT_COLLECTION_PATTERNS_COMPILED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in TENANT_COLLECTION_NAMING_PATTERNS
)

MultitenancyStyle = Literal[
    "none",
    "disjoint_smartgraph",
    "shard_key",
    "discriminator_field",
    "collection_per_tenant",
    "database_per_tenant",
    "unknown_single_db",
]

MultitenancyStatus = Literal["ok", "degraded"]


def _user_collections(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the non-system collection entries from the snapshot.

    Tolerant of missing / malformed snapshot keys — empty list is the
    safe degraded value.
    """
    raw = snapshot.get("collections")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name or name.startswith("_"):
            continue
        props = entry.get("properties")
        if isinstance(props, dict) and props.get("isSystem") is True:
            continue
        out.append(entry)
    return out


def _collection_to_entity_index(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map ``collection name → entity-mapping dict`` for the user
    entities in ``physicalMapping.entities``.

    Used to look up the property declarations for a given underlying
    collection. Falls back to keying on the conceptual entity name
    when ``collectionName`` is absent.
    """
    pm = data.get("physicalMapping")
    if not isinstance(pm, dict):
        return {}
    entities = pm.get("entities")
    if not isinstance(entities, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for ent_name, ent_data in entities.items():
        if not isinstance(ent_data, dict):
            continue
        col = ent_data.get("collectionName")
        key = col if isinstance(col, str) and col else ent_name
        out[key] = ent_data
    return out


def _shard_keys_for(entry: dict[str, Any]) -> list[str]:
    """Extract the shard-key list from a snapshot collection entry.

    Returns an empty list when the entry has no usable
    ``properties.shardKeys`` array. Tolerates the field being absent
    (older snapshots) or wrong-typed without raising.
    """
    props = entry.get("properties")
    if not isinstance(props, dict):
        return []
    sk = props.get("shardKeys")
    if not isinstance(sk, list):
        return []
    return [str(k) for k in sk if isinstance(k, str) and k and k != "_key"]


def _looks_tenant_keyish(field: str) -> bool:
    """Heuristic: does ``field`` look like a tenant identifier?

    Used by the shard-key path to decide whether a shared shard-key
    is plausibly a tenant key. Case-insensitive substring match
    against the canonical roots.
    """
    f = field.lower()
    return any(token in f for token in ("tenant", "org", "account", "customer", "workspace"))


# ---------------------------------------------------------------------------
# Style detectors. Each returns a (block, evidence) tuple or None.
# ---------------------------------------------------------------------------


def _detect_disjoint_smartgraph(
    sharding_profile: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Style 1: disjoint smart graph ⇒ smart attribute IS the tenant key.

    Trusts the upstream sharding profile; we don't re-derive the
    smart-attribute walk here. Returns ``None`` when sharding profile
    is absent or the style isn't ``DisjointSmartGraph``.
    """
    if not isinstance(sharding_profile, dict):
        return None
    if sharding_profile.get("style") != "DisjointSmartGraph":
        return None
    smart_attrs: set[str] = set()
    disjoint_graphs: list[str] = []
    for g in sharding_profile.get("graphs") or []:
        if not isinstance(g, dict):
            continue
        if g.get("isDisjoint") is True:
            attr = g.get("smartGraphAttribute")
            if isinstance(attr, str) and attr:
                smart_attrs.add(attr)
            name = g.get("name")
            if isinstance(name, str) and name:
                disjoint_graphs.append(name)
    if not smart_attrs:
        return {
            "style": "disjoint_smartgraph",
            "tenantKey": [],
            "physicalEnforcement": True,
            "evidence": {
                "shardingProfileStyle": "DisjointSmartGraph",
                "disjointGraphs": sorted(disjoint_graphs),
                "note": (
                    "DisjointSmartGraph style detected but no smart attribute could be extracted from graph evidence"
                ),
            },
            "_status": "degraded",
        }
    return {
        "style": "disjoint_smartgraph",
        "tenantKey": sorted(smart_attrs),
        "physicalEnforcement": True,
        "evidence": {
            "shardingProfileStyle": "DisjointSmartGraph",
            "disjointGraphs": sorted(disjoint_graphs),
            "smartGraphAttributes": sorted(smart_attrs),
        },
    }


def _detect_shard_key(
    snapshot: dict[str, Any],
    user_cols: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Style 2: a tenant-like field is the shard key on multiple collections.

    Triggers when at least two user collections share a shard-key list
    whose first element looks like a tenant identifier
    (:func:`_looks_tenant_keyish`). The shared shard key wins over
    discriminator-field detection because it is *physically* enforced.
    """
    if len(user_cols) < 2:
        return None

    by_key: dict[str, list[str]] = {}
    for entry in user_cols:
        keys = _shard_keys_for(entry)
        if not keys:
            continue
        primary = keys[0]
        if not _looks_tenant_keyish(primary):
            continue
        by_key.setdefault(primary, []).append(entry["name"])

    if not by_key:
        return None

    # Pick the shard key with the widest coverage; tie-break alphabetically
    # on the field name for determinism.
    best_field, best_cols = max(
        by_key.items(),
        key=lambda kv: (len(kv[1]), -ord(kv[0][0]) if kv[0] else 0),
    )
    if len(best_cols) < 2:
        return None

    return {
        "style": "shard_key",
        "tenantKey": [best_field],
        "physicalEnforcement": True,
        "tenantKeyCollections": [{"collection": c, "shardKey": best_field} for c in sorted(best_cols)],
        "evidence": {
            "sharedShardKey": best_field,
            "collectionCount": len(best_cols),
            "totalUserCollections": len(user_cols),
        },
    }


def _candidate_field_match(declared: set[str], candidates: tuple[str, ...]) -> str | None:
    """Return the first candidate (case-insensitive) carried by ``declared``."""
    lowered = {d.lower(): d for d in declared}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def _sample_field_coverage(
    entry: dict[str, Any],
    field: str,
) -> tuple[float | None, int | None, list[str]]:
    """Sample-based coverage and cardinality for ``field`` on a collection.

    When ``entry["properties"]["sample_documents"]`` is present (the
    snapshot was built with ``include_samples_in_snapshot=True``),
    return ``(fraction-of-samples-with-field, distinct-value-count,
    sorted-sample-of-distinct-values-up-to-MAX)``. Otherwise return
    ``(None, None, [])`` — caller falls back to the schema-level
    declaration as binary coverage.
    """
    samples = entry.get("sample_documents")
    if not isinstance(samples, list) or not samples:
        return None, None, []
    n = 0
    seen: set[str] = set()
    present = 0
    for doc in samples:
        if not isinstance(doc, dict):
            continue
        n += 1
        if field in doc:
            present += 1
            v = doc[field]
            if isinstance(v, (str, int, float, bool)) and len(seen) < MAX_TENANT_DISTINCT_VALUES:
                seen.add(str(v))
    if n == 0:
        return None, None, []
    return present / n, len(seen), sorted(seen)


def _detect_discriminator_field(
    data: dict[str, Any],
    user_cols: list[dict[str, Any]],
    *,
    discriminator_fields: tuple[str, ...] = TENANT_DISCRIMINATOR_FIELDS,
    min_coverage: float = MIN_TENANT_FIELD_COVERAGE_FRACTION,
) -> dict[str, Any] | None:
    """Style 3: discriminator-field multitenancy.

    A candidate property (``tenantId`` / ``org_id`` / …) is declared
    by enough collections to call it the convention. *Convention*
    is the operative word: the field is NOT a shard key (that would
    have been picked up by :func:`_detect_shard_key` earlier and won),
    so tenant isolation here is enforced by application code, not by
    storage. We surface ``physicalEnforcement = false`` and a warning
    flag so consumers can decide whether to trust the convention.
    """
    if not user_cols:
        return None
    col_to_entity = _collection_to_entity_index(data)

    candidate_hits: dict[str, list[dict[str, Any]]] = {}
    for entry in user_cols:
        col_name = entry["name"]
        ent = col_to_entity.get(col_name) or {}
        declared = set(entity_property_names(ent))
        match = _candidate_field_match(declared, discriminator_fields)
        if match is None:
            continue
        coverage, cardinality, sample_values = _sample_field_coverage(entry, match)
        if coverage is None:
            coverage = 1.0
        record = {
            "collection": col_name,
            "field": match,
            "coverage": round(coverage, 4),
        }
        if cardinality is not None:
            record["distinctValues"] = cardinality
            if sample_values:
                record["sampleValues"] = sample_values
        candidate_hits.setdefault(match.lower(), []).append(record)

    if not candidate_hits:
        return None

    best_field_key, best_records = max(
        candidate_hits.items(),
        key=lambda kv: (len(kv[1]), kv[0]),
    )
    fraction = len(best_records) / len(user_cols)
    if fraction < min_coverage:
        return None

    field_name = best_records[0]["field"]
    return {
        "style": "discriminator_field",
        "tenantKey": [field_name],
        "physicalEnforcement": False,
        "tenantKeyCollections": sorted(
            best_records,
            key=lambda r: r["collection"],
        ),
        "evidence": {
            "candidateField": field_name,
            "fraction": round(fraction, 4),
            "minCoverageThreshold": min_coverage,
            "matchedCollectionCount": len(best_records),
            "totalUserCollections": len(user_cols),
            "warning": ("tenancy enforced by application convention only; no shard-key or smart-graph isolation"),
        },
    }


def _detect_collection_per_tenant(
    user_cols: list[dict[str, Any]],
    *,
    patterns: tuple[str, ...] = TENANT_COLLECTION_NAMING_PATTERNS,
) -> dict[str, Any] | None:
    """Style 4: ``<base>__<tenant>`` (or ``<tenant>_<base>``) naming.

    Triggers when at least two distinct tenant values cluster against
    a consistent ``base`` set (i.e. each tenant has the same set of
    base names). Single-pattern matches don't trigger — naming-only
    evidence is fragile and we want at least 2 tenants × ≥1 base
    before we claim physical-per-tenant separation.
    """
    if len(user_cols) < 2:
        return None
    if patterns is TENANT_COLLECTION_NAMING_PATTERNS:
        compiled: list[re.Pattern[str]] = list(_DEFAULT_TENANT_COLLECTION_PATTERNS_COMPILED)
    else:
        compiled = [re.compile(p) for p in patterns]

    by_tenant: dict[str, set[str]] = {}
    by_base: dict[str, set[str]] = {}
    for entry in user_cols:
        name = entry["name"]
        for rx in compiled:
            m = rx.match(name)
            if not m:
                continue
            tenant = m.group("tenant")
            base = m.group("base")
            by_tenant.setdefault(tenant, set()).add(base)
            by_base.setdefault(base, set()).add(tenant)
            break
    if len(by_tenant) < 2:
        return None
    if not any(len(tenants) >= 2 for tenants in by_base.values()):
        return None

    return {
        "style": "collection_per_tenant",
        "tenantKey": [],
        "physicalEnforcement": True,
        "evidence": {
            "tenantCount": len(by_tenant),
            "baseCount": len(by_base),
            "tenants": sorted(by_tenant.keys())[:MAX_TENANT_DISTINCT_VALUES],
            "bases": sorted(by_base.keys()),
            "namingPatternIndex": next(
                (i for i, _ in enumerate(compiled)),
                0,
            ),
        },
    }


def _detect_database_per_tenant_hint(
    snapshot: dict[str, Any],
    *,
    patterns: tuple[str, ...] = TENANT_DATABASE_NAMING_PATTERNS,
) -> dict[str, Any] | None:
    """Style 5: lone-snapshot hint that THIS database is one tenant.

    A single-database snapshot cannot prove ``database_per_tenant``
    on its own — that's an orchestrator-level call. The best we can
    do from one snapshot is flag ``unknown_single_db`` when the
    database's own name matches a tenant naming pattern, so the
    orchestrator knows to compare against sibling databases.
    """
    db = snapshot.get("database")
    if not isinstance(db, dict):
        return None
    name = db.get("name")
    if not isinstance(name, str) or not name:
        return None
    for rx in patterns:
        if re.match(rx, name):
            return {
                "style": "unknown_single_db",
                "tenantKey": [],
                "physicalEnforcement": True,
                "evidence": {
                    "databaseName": name,
                    "matchedPattern": rx,
                    "note": (
                        "database name matches a tenant-naming pattern; "
                        "confirm via cross-database aggregation in an "
                        "orchestrator before treating as database_per_tenant"
                    ),
                },
            }
    return None


def classify_multitenancy(
    data: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    sharding_profile: dict[str, Any] | None = None,
    discriminator_fields: tuple[str, ...] = TENANT_DISCRIMINATOR_FIELDS,
    min_coverage: float = MIN_TENANT_FIELD_COVERAGE_FRACTION,
) -> dict[str, Any] | None:
    """Classify the analysed data + snapshot by multitenancy pattern.

    Parameters
    ----------
    data:
        The in-flight analyzer ``data`` dict (carries
        ``physicalMapping.entities`` for property declarations).
    snapshot:
        The raw snapshot dict (carries ``collections[*].properties``
        for shard keys + ``database.name`` for the per-tenant DB
        hint).
    sharding_profile:
        Pre-computed ``metadata.shardingProfile`` block from
        :func:`schema_analyzer.sharding_profile.classify_sharding_profile`.
        When ``None``, the disjoint-smart-graph branch is skipped and
        we fall through to the structural detectors. Pass
        ``data["metadata"]["shardingProfile"]`` from the analyzer
        wiring.
    discriminator_fields, min_coverage:
        Tunables — exposed here so tests and downstream consumers can
        tighten or relax them per call.

    Returns
    -------
    dict | None
        The ``multitenancy`` block, or ``None`` when the snapshot is
        too minimal to classify (no user collections at all). Callers
        treat ``None`` as "omit the block from metadata", matching the
        contract used by other analyzer post-processors.

        When at least one user collection exists but no signal
        triggers, returns ``{"style": "none", ...}`` rather than
        ``None`` — consumers can then distinguish "didn't run" from
        "ran, found no tenancy".
    """
    if not isinstance(snapshot, dict):
        return None
    user_cols = _user_collections(snapshot)
    if not user_cols:
        return None

    detectors: list[dict[str, Any] | None] = [
        _detect_disjoint_smartgraph(sharding_profile),
        _detect_shard_key(snapshot, user_cols),
        _detect_discriminator_field(
            data,
            user_cols,
            discriminator_fields=discriminator_fields,
            min_coverage=min_coverage,
        ),
        _detect_collection_per_tenant(user_cols),
        _detect_database_per_tenant_hint(snapshot),
    ]

    block: dict[str, Any] | None = None
    for candidate in detectors:
        if candidate is not None:
            block = candidate
            break

    if block is None:
        block = {
            "style": "none",
            "tenantKey": [],
            "physicalEnforcement": False,
            "evidence": {
                "userCollectionCount": len(user_cols),
                "note": "no multitenancy signal detected",
            },
        }

    status: MultitenancyStatus = "degraded" if block.pop("_status", None) == "degraded" else "ok"
    block["status"] = status

    logger.info(
        "Multitenancy: style=%s physicalEnforcement=%s status=%s",
        block["style"],
        block["physicalEnforcement"],
        status,
    )
    return block


__all__ = [
    "MultitenancyStatus",
    "MultitenancyStyle",
    "classify_multitenancy",
]
