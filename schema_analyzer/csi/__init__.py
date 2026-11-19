"""Conceptual Schema Interchange (CSI) v1 — a direction-agnostic interop format.

CSI is the minimal contract two tools standardize on to exchange a conceptual
model + its ArangoDB physical mapping, independent of *which direction* produced
it:

* ``arango-schema-analyzer`` (this library) emits CSI by reverse-engineering an
  existing ArangoDB graph (``direction = "reverse"``).
* A forward relational→graph tool (e.g. R2G) emits CSI for the graph it built
  (``direction = "forward"``), and can consume an analyzer CSI as a target
  shape.

The document is deliberately the same three blocks this library already
produces — ``conceptualModel`` (≡ ``conceptualSchema``), ``arangoPhysicalMapping``
(≡ ``physicalMapping``) — plus a small ``provenance`` envelope. Detection
enrichments (vci/rdfTopology/graphRag/source/… ) ride along as optional
additive fields; a CSI consumer never requires them.

``to_csi`` / ``from_csi`` are the producer / consumer adapters: ``from_csi``
returns the ``{conceptualSchema, physicalMapping, metadata}`` shape this
library's ``diff_analyses`` / ``compute_gold_comparison`` / quality / export
helpers already accept, so an R2G-produced CSI can be audited here directly.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any, cast
from urllib.parse import quote

from jsonschema import Draft202012Validator

from ..defaults import FALLBACK_LIBRARY_VERSION
from ..errors import SchemaAnalyzerError
from ..utils import normalize_analysis_dict

CSI_VERSION = "1"
#: The version a document declares when some property mapping uses ``path`` (a value
#: inside a nested object). Readers of version 1 refuse it rather than read the wrong
#: field, so :func:`to_csi` writes it only when ``path`` is actually used.
CSI_VERSION_NESTED = "1.1"
#: The version a document declares when some property mapping carries ``multiValued``
#: (its attribute holds a list, one value per element; CDF #220). It includes 1.1. A
#: reader that does not know the key would read the whole list as one value, so
#: :func:`to_csi` writes it only when the key is actually used.
CSI_VERSION_LISTS = "1.2"
CSI_VERSIONS = (CSI_VERSION, CSI_VERSION_NESTED, CSI_VERSION_LISTS)

#: Keys an entity property mapping may hold besides its location (``field`` / ``path``),
#: plus any key starting with ``x-``. Everything else is refused by readers.
PROPERTY_LOCATION_KEYS = ("field", "path")
PROPERTY_DESCRIPTIVE_KEYS = (
    "type",
    "indexed",
    "unique",
    "required",
    "role",
    "sentinelValues",
    "numericLike",
    "sampleValues",
    "multiValued",
)

#: A publisher URI or mapping id: absolute http(s), no query or fragment (``$defs/MappingId``).
_MAPPING_ID_RE = re.compile(r"^https?://[^\s?#]+$")


def _library_version() -> str:
    try:
        return _pkg_version("arangodb-schema-analyzer")
    except PackageNotFoundError:  # pragma: no cover - source checkout without install
        return FALLBACK_LIBRARY_VERSION


def _meta_get(meta: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if isinstance(meta, dict) and meta.get(k) is not None:
            return meta[k]
    return None


def build_provenance(
    metadata: dict[str, Any] | None,
    *,
    direction: str = "reverse",
    source: dict[str, Any] | None = None,
    producer: str = "arango-schema-analyzer",
    producer_version: str | None = None,
    publisher_uri: str | None = None,
) -> dict[str, Any]:
    """Build the CSI ``provenance`` envelope from analysis metadata.

    ``source`` defaults to an ArangoDB descriptor keyed off the snapshot
    fingerprint already on the metadata; callers (e.g. the tool layer) may pass
    a richer ``{"kind", "ref", "fingerprint"}``. ``publisher_uri`` is recorded as
    ``publisher.uri``, the namespace the document's mapping ids are minted under.
    """
    meta = metadata if isinstance(metadata, dict) else {}
    fingerprint = _meta_get(meta, "physicalSchemaFingerprint", "physical_schema_fingerprint")
    prov: dict[str, Any] = {
        "producer": producer,
        "producerVersion": producer_version or _library_version(),
        "direction": direction,
        "source": source if isinstance(source, dict) and source else {"kind": "arangodb", "fingerprint": fingerprint},
        "generatedAt": _meta_get(meta, "analysisCompletedAt", "analysis_completed_at", "timestamp"),
    }
    confidence = _meta_get(meta, "confidence")
    if isinstance(confidence, (int, float)):
        prov["confidence"] = confidence
    # Bitemporal stamping (PRD §3.13.5). transactionTime is mandatory (an explicit
    # alias of the completion timestamp); validTime is always present, defaulting to
    # observed-at-transaction when the analysis metadata predates §3.13.5, so CSI
    # documents from older runs still carry the two clocks the temporal store needs.
    tx = _meta_get(
        meta, "transactionTime", "transaction_time", "analysisCompletedAt", "analysis_completed_at", "timestamp"
    )
    if isinstance(tx, str) and tx:
        prov["transactionTime"] = tx
    vt = _meta_get(meta, "validTime", "valid_time")
    vts = _meta_get(meta, "validTimeSource", "valid_time_source")
    if isinstance(vt, dict) and isinstance(vt.get("from"), str):
        prov["validTime"] = {"from": vt["from"]}
        prov["validTimeSource"] = vts if isinstance(vts, str) and vts else "observed"
    elif isinstance(tx, str) and tx:
        prov["validTime"] = {"from": tx}
        prov["validTimeSource"] = "observed"
    predecessor = _meta_get(meta, "predecessorFingerprint", "predecessor_fingerprint")
    if isinstance(predecessor, str) and predecessor:
        prov["predecessorFingerprint"] = predecessor
    if publisher_uri:
        prov["publisher"] = {"uri": publisher_uri}
    return prov


def to_csi(
    analysis: Any,
    *,
    direction: str = "reverse",
    source: dict[str, Any] | None = None,
    producer: str = "arango-schema-analyzer",
    producer_version: str | None = None,
    owl_naming: bool = True,
    entity_overrides: dict[str, str] | None = None,
    property_overrides: dict[str, str] | None = None,
    publisher_uri: str | None = None,
) -> dict[str, Any]:
    """Produce a CSI v1 document from an ``AnalysisResult`` or serialized dict.

    ``owl_naming`` (default on, per CDF CC-12) normalizes the *conceptual*
    layer to the W3C-community OWL style — entities singular PascalCase,
    properties/relationships lowerCamel — recording each renamed property's
    physical field so consumers resolve back to stored names. Pass
    ``owl_naming=False`` for a raw, physically-named document.

    ``publisher_uri`` (issue #42; else the analysis's ``metadata.publisherUri``, which
    :func:`from_csi` keeps) is the namespace of this one published document. With it,
    every mapping entry gets a mapping id under it — ids already on an entry are kept,
    only missing ones are minted — and the mapping is also written under
    ``physicalMapping``, equal to ``arangoPhysicalMapping``. Without it the document is
    exactly what it was before: no ids, no ``physicalMapping`` key.

    Raises :class:`SchemaAnalyzerError` (``INVALID_REQUEST``) rather than return a
    document :func:`validate_csi` would refuse: a malformed publisher URI, ids carried
    from another publisher (never re-minted), or a ``propertyIds`` key whose property
    is gone (never dropped silently).
    """
    data = normalize_analysis_dict(analysis)
    conceptual = data.get("conceptualSchema")
    physical = data.get("physicalMapping")
    metadata = data.get("metadata")
    # Both sources are checked, so a malformed or conflicting one is never hidden by the other.
    supplied = {"publisher_uri": publisher_uri}
    if isinstance(metadata, dict) and metadata.get("publisherUri") is not None:
        supplied["metadata.publisherUri"] = metadata["publisherUri"]
    uris: dict[str, str] = {}
    for field, value in supplied.items():
        if value is None:
            continue
        if not isinstance(value, str) or not _MAPPING_ID_RE.match(value.rstrip("/")):
            raise SchemaAnalyzerError(
                f"{field} must be an absolute http(s) URI without query or fragment: {value!r}",
                code="INVALID_REQUEST",
            )
        uris[field] = value.rstrip("/")
    if len(set(uris.values())) > 1:
        raise SchemaAnalyzerError(
            f"publisher_uri {uris['publisher_uri']!r} differs from metadata.publisherUri "
            f"{uris['metadata.publisherUri']!r}, under which this analysis's mapping ids were published; "
            "ids are kept, never re-minted under another publisher",
            code="INVALID_REQUEST",
        )
    publisher_uri = next(iter(uris.values()), None)

    doc = {
        "csiVersion": CSI_VERSION,
        "conceptualModel": conceptual
        if isinstance(conceptual, dict)
        else {"entities": [], "relationships": [], "properties": []},
        "arangoPhysicalMapping": physical if isinstance(physical, dict) else {"entities": {}, "relationships": {}},
        "provenance": build_provenance(
            metadata if isinstance(metadata, dict) else {},
            direction=direction,
            source=source,
            producer=producer,
            producer_version=producer_version,
            publisher_uri=publisher_uri,
        ),
    }
    if owl_naming:
        from .naming import apply_owl_naming

        doc = apply_owl_naming(doc, entity_overrides=entity_overrides, property_overrides=property_overrides)
    doc = _lift_attribute_references(doc)
    doc = _settle_property_mappings(doc, metadata if isinstance(metadata, dict) else {})
    if publisher_uri:
        doc = _mint_mapping_ids(doc, publisher_uri)
        doc["physicalMapping"] = copy.deepcopy(doc["arangoPhysicalMapping"])
    issues = _identity_issues(doc)
    if issues:
        raise SchemaAnalyzerError(
            "refusing to write a CSI document with invalid mapping ids: " + "; ".join(issues),
            code="INVALID_REQUEST",
        )
    return doc


def _mint_mapping_ids(doc: dict[str, Any], base: str) -> dict[str, Any]:
    """Give every mapping entry without an id one under *base* (issue #42).

    An id an entry already carries is never replaced, so an id survives a rename once
    it has been published. A new id is built from names, not positions, so exporting
    again or reordering mints the same ids.
    """
    doc = copy.deepcopy(doc)  # never mutate the caller's analysis

    def seg(name: str) -> str:
        return quote(name, safe="")

    physical = doc["arangoPhysicalMapping"]
    for kind, label in (("entities", "entity"), ("relationships", "relationship")):
        specs = physical.get(kind)
        for name, spec in specs.items() if isinstance(specs, dict) else []:
            if not isinstance(spec, dict):
                continue
            prefix = f"{base}/mapping/{label}/{seg(name)}"
            spec.setdefault("id", prefix)
            props = spec.get("properties")
            ids = spec.setdefault("propertyIds", {}) if isinstance(props, dict) and props else None
            if isinstance(props, dict) and isinstance(ids, dict):  # a value present, valid or not, is kept
                for p in props:
                    ids.setdefault(p, f"{prefix}/property/{seg(p)}")
    conceptual = doc.get("conceptualModel")
    for rel in conceptual.get("relationships") or [] if isinstance(conceptual, dict) else []:
        if isinstance(rel, dict) and isinstance(rel.get("reference"), dict):
            rel["reference"].setdefault("id", f"{base}/mapping/relationship/{seg(str(rel.get('type')))}/reference")
    return doc


def _uses_path(physical: Any) -> bool:
    if not isinstance(physical, dict):
        return False
    for kind in ("entities", "relationships"):
        specs = physical.get(kind)
        for spec in specs.values() if isinstance(specs, dict) else []:
            props = spec.get("properties") if isinstance(spec, dict) else None
            for entry in props.values() if isinstance(props, dict) else []:
                if isinstance(entry, dict) and "path" in entry:
                    return True
    return False


def _uses_multi_valued(physical: Any) -> bool:
    if not isinstance(physical, dict):
        return False
    specs = physical.get("entities")
    for spec in specs.values() if isinstance(specs, dict) else []:
        props = spec.get("properties") if isinstance(spec, dict) else None
        for entry in props.values() if isinstance(props, dict) else []:
            if isinstance(entry, dict) and "multiValued" in entry:
                return True
    return False


def _csi_version_for(physical: Any) -> str:
    """The lowest version whose readers can read *physical*'s property mappings."""
    if _uses_multi_valued(physical):
        return CSI_VERSION_LISTS
    return CSI_VERSION_NESTED if _uses_path(physical) else CSI_VERSION


def _prefix_unknown_keys(entry: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """*entry* with each key CSI does not allow kept under an ``x-`` prefix.

    The prefix is repeated until the key is free (``description`` beside an existing
    ``x-description`` becomes ``x-x-description``), so no value is overwritten.
    """
    out = {k: v for k, v in entry.items() if k in allowed or k.startswith("x-")}
    for k, v in entry.items():
        if k in allowed or k.startswith("x-"):
            continue
        key = f"x-{k}"
        while key in out:
            key = f"x-{key}"
        out[key] = v
    return out


def _settle_property_mappings(doc: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """Make the document's property mappings valid CSI and pick its version (issue #33).

    * ``csiVersion`` is ``"1.1"`` only when some property mapping uses ``path``, and
      ``"1.2"`` only when some entity property mapping carries ``multiValued``, so an
      analyzer upgrade never turns a document an older reader can read into one it
      refuses.
    * An entity property mapping is strict: a key that is neither a location, a
      descriptive key nor ``x-`` prefixed (an LLM-written ``description``, say) is kept
      under ``x-<key>`` rather than dropped, so nothing is lost and readers accept it.
    * Dotted names the analyzer left unmapped because a collection holds them in two
      shapes travel in ``provenance.unresolvedFieldLocations`` for a person to settle.
    """
    physical = doc.get("arangoPhysicalMapping")
    entities = physical.get("entities") if isinstance(physical, dict) else None
    allowed = set(PROPERTY_LOCATION_KEYS) | set(PROPERTY_DESCRIPTIVE_KEYS)
    needs_copy = isinstance(entities, dict) and any(
        isinstance(entry, dict) and any(k not in allowed and not k.startswith("x-") for k in entry)
        for spec in entities.values()
        if isinstance(spec, dict) and isinstance(spec.get("properties"), dict)
        for entry in spec["properties"].values()
    )
    raw_nested = metadata.get("nestedFields")
    nested: dict[str, Any] = raw_nested if isinstance(raw_nested, dict) else {}
    raw_conflicts = nested.get("conflicts")
    conflicts: list[Any] = raw_conflicts if isinstance(raw_conflicts, list) else []
    if needs_copy or conflicts:
        doc = copy.deepcopy(doc)  # never mutate the caller's analysis
    if needs_copy:
        for spec in doc["arangoPhysicalMapping"]["entities"].values():
            props = spec.get("properties") if isinstance(spec, dict) else None
            if not isinstance(props, dict):
                continue
            for name, entry in list(props.items()):
                if isinstance(entry, dict):
                    props[name] = _prefix_unknown_keys(entry, allowed)
    if conflicts:
        doc.setdefault("provenance", {})["unresolvedFieldLocations"] = conflicts
    doc["csiVersion"] = _csi_version_for(doc.get("arangoPhysicalMapping"))
    return doc


#: Keys of a ``FOREIGN_KEY`` physical mapping carried onto the conceptual relationship.
#: ``id`` is its mapping identity (issue #42); it travels with the reference both ways.
_REFERENCE_FIELDS = (
    "fromCollection",
    "fromFields",
    "toCollection",
    "toKeyFields",
    "confidence",
    "method",
    "evidence",
    "id",
)


def _lift_attribute_references(doc: dict[str, Any]) -> dict[str, Any]:
    """Move ``FOREIGN_KEY`` relationships from the physical mapping onto the conceptual one.

    CSI's physical relationship ``style`` admits only edge-backed styles
    (``DEDICATED_COLLECTION`` / ``GENERIC_WITH_TYPE``): an attribute-carried reference has
    no edge collection to name. Emitting it physically produced an invalid document, so the
    relationship rides the conceptual layer — which CSI leaves open to additive fields — with
    its join described under ``reference`` (``origin`` = ``declared`` or ``inferred``).
    :func:`from_csi` restores the physical mapping, so the round trip is lossless.
    """
    physical = doc.get("arangoPhysicalMapping")
    rels = physical.get("relationships") if isinstance(physical, dict) else None
    if not isinstance(rels, dict) or not any(
        isinstance(spec, dict) and spec.get("style") == "FOREIGN_KEY" for spec in rels.values()
    ):
        return doc

    doc = copy.deepcopy(doc)  # never mutate the caller's analysis
    rels = doc["arangoPhysicalMapping"]["relationships"]
    conceptual = doc.get("conceptualModel")
    concept_rels = conceptual.get("relationships") if isinstance(conceptual, dict) else None
    by_type = {
        r.get("type"): r for r in (concept_rels if isinstance(concept_rels, list) else []) if isinstance(r, dict)
    }

    unattributed: list[str] = []
    for name in [n for n, spec in rels.items() if isinstance(spec, dict) and spec.get("style") == "FOREIGN_KEY"]:
        spec = rels.pop(name)
        reference = {key: spec[key] for key in _REFERENCE_FIELDS if key in spec}
        reference["origin"] = "declared" if spec.get("method") == "declared" else "inferred"
        target = by_type.get(name)
        if target is None:
            unattributed.append(name)
            continue
        target["reference"] = reference
    if unattributed:
        # Never silent: a reference with no conceptual relationship cannot be carried.
        doc.setdefault("provenance", {})["unattributedReferences"] = sorted(unattributed)
    return doc


def _restore_attribute_references(conceptual: Any, physical: Any) -> tuple[Any, Any]:
    """Inverse of :func:`_lift_attribute_references`, for :func:`from_csi`."""
    concept_rels = conceptual.get("relationships") if isinstance(conceptual, dict) else None
    if not isinstance(concept_rels, list) or not any(
        isinstance(r, dict) and isinstance(r.get("reference"), dict) for r in concept_rels
    ):
        return conceptual, physical

    conceptual = copy.deepcopy(conceptual)
    physical = copy.deepcopy(physical) if isinstance(physical, dict) else {"entities": {}, "relationships": {}}
    pm_rels = physical.setdefault("relationships", {})
    for rel in conceptual["relationships"]:
        if not isinstance(rel, dict) or not isinstance(rel.get("reference"), dict):
            continue
        reference = dict(rel.pop("reference"))
        reference.pop("origin", None)
        pm_rels[rel["type"]] = {"style": "FOREIGN_KEY", **reference, "enforced": False}
    return conceptual, physical


def from_csi(csi: dict[str, Any]) -> dict[str, Any]:
    """Adapt a CSI v1 document into the ``{conceptualSchema, physicalMapping,
    metadata}`` shape this library's consumers (diff, gold, quality, exports)
    accept. Provenance is folded into ``metadata`` (confidence / timestamp).

    Accepts ``csiVersion`` ``"1"``, ``"1.1"`` and ``"1.2"``; a property mapping's ``path``
    (a value inside a nested object) and ``multiValued`` (an attribute holding a list) are
    carried through unchanged, so ``to_csi`` → ``from_csi`` round-trips them.

    The mapping is read from ``arangoPhysicalMapping``, else from its alias
    ``physicalMapping``; a document carrying both with different values is refused.
    Mapping ids, ``relation`` / ``columns`` and ``provenance.publisher.uri`` (as
    ``metadata.publisherUri``) are kept, so ``to_csi`` writes the same ids again.
    """
    conceptual = csi.get("conceptualModel")
    physical = csi.get("arangoPhysicalMapping")
    if "physicalMapping" in csi:
        if physical is None:
            physical = csi["physicalMapping"]
        elif csi["physicalMapping"] != physical:
            raise ValueError("physicalMapping and arangoPhysicalMapping differ; a CSI document must carry one mapping")
    prov = csi.get("provenance")
    prov = prov if isinstance(prov, dict) else {}

    metadata: dict[str, Any] = {}
    if isinstance(prov.get("confidence"), (int, float)):
        metadata["confidence"] = prov["confidence"]
    if isinstance(prov.get("generatedAt"), str):
        metadata["timestamp"] = prov["generatedAt"]
    src = prov.get("source")
    if isinstance(src, dict) and isinstance(src.get("fingerprint"), str):
        metadata["physicalSchemaFingerprint"] = src["fingerprint"]
    publisher = prov.get("publisher")
    if isinstance(publisher, dict) and isinstance(publisher.get("uri"), str):
        metadata["publisherUri"] = publisher["uri"]

    conceptual, physical = _restore_attribute_references(conceptual, physical)
    return {
        "conceptualSchema": conceptual
        if isinstance(conceptual, dict)
        else {"entities": [], "relationships": [], "properties": []},
        "physicalMapping": physical if isinstance(physical, dict) else {"entities": {}, "relationships": {}},
        "metadata": metadata,
    }


#: Marks a ``propertyIds`` key with no matching ``properties`` key in :func:`_identity_issues`.
_NO_PROPERTY = object()


def _identity_issues(document: dict[str, Any]) -> list[str]:
    """What JSON Schema cannot check about the alias and the mapping ids (issue #42).

    * ``physicalMapping``, when present beside ``arangoPhysicalMapping``, equals it.
    * Once any mapping id is present the set is complete — every entity and relationship
      mapping, every property (``propertyIds`` keys are exactly the ``properties`` keys)
      and every lifted ``reference`` — and every id is unique and lies under
      ``provenance.publisher.uri``. A document without ids claims no identity.
    """
    if not isinstance(document, dict):
        return []
    issues: list[str] = []
    physical = document.get("arangoPhysicalMapping")
    if "physicalMapping" in document and physical is not None and document["physicalMapping"] != physical:
        issues.append("physicalMapping differs from arangoPhysicalMapping")

    ids: list[tuple[str, Any]] = []  # (where, id; None when absent, _NO_PROPERTY for a stray propertyIds key)
    for kind in ("entities", "relationships"):
        specs = physical.get(kind) if isinstance(physical, dict) else None
        for name, spec in specs.items() if isinstance(specs, dict) else []:
            if not isinstance(spec, dict):
                continue
            where = f"arangoPhysicalMapping.{kind}.{name}"
            ids.append((where, spec.get("id")))
            raw_props, raw_ids = spec.get("properties"), spec.get("propertyIds")
            props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
            prop_ids: dict[str, Any] = raw_ids if isinstance(raw_ids, dict) else {}
            ids.extend((f"{where}.propertyIds.{p}", prop_ids.get(p)) for p in props)
            ids.extend((f"{where}.propertyIds.{p}", _NO_PROPERTY) for p in prop_ids if p not in props)
    conceptual = document.get("conceptualModel")
    for rel in conceptual.get("relationships") or [] if isinstance(conceptual, dict) else []:
        if isinstance(rel, dict) and isinstance(rel.get("reference"), dict):
            ids.append((f"conceptualModel.relationships.{rel.get('type')}.reference", rel["reference"].get("id")))

    # Any id-carrying key present, even empty or stray, claims identity and is checked.
    if all(i is None for _, i in ids):
        return issues
    prov = document.get("provenance")
    publisher = prov.get("publisher") if isinstance(prov, dict) else None
    base = publisher.get("uri") if isinstance(publisher, dict) else None
    if not isinstance(base, str) or not base:
        issues.append("mapping ids are present but provenance.publisher.uri is missing")
    for where, mapping_id in ids:
        if mapping_id is _NO_PROPERTY:
            issues.append(f"{where} names no property in properties")
        elif not isinstance(mapping_id, str) or not mapping_id:
            issues.append(f"{where} has no mapping id")
        elif isinstance(base, str) and base and not mapping_id.startswith(base.rstrip("/") + "/"):
            issues.append(f"{where} id {mapping_id!r} is not under publisher {base!r}")
    counts = Counter(i for _, i in ids if isinstance(i, str) and i)
    issues.extend(f"duplicate mapping id {i!r}" for i, n in sorted(counts.items()) if n > 1)
    return issues


def load_csi_schema_v1() -> dict[str, Any]:
    """Load the bundled CSI v1 JSON Schema."""
    from importlib.resources import files

    p = files("schema_analyzer.csi.v1").joinpath("csi.schema.json")
    return cast("dict[str, Any]", json.loads(p.read_text(encoding="utf-8")))


def validate_csi(document: dict[str, Any], *, naming: bool = True) -> list[str]:
    """Return a sorted list of CSI v1 validation error messages (empty = valid).

    Checks the JSON Schema, the mapping alias and mapping ids (:func:`_identity_issues`)
    and, unless ``naming=False``, the CC-12 OWL naming convention on the conceptual
    layer (entities singular PascalCase, properties/relationships lowerCamel).
    """
    validator = Draft202012Validator(load_csi_schema_v1())
    issues = [err.message for err in sorted(validator.iter_errors(document), key=str)]
    issues.extend(_identity_issues(document))
    if naming:
        from .naming import naming_issues

        issues.extend(naming_issues(document))
    return issues
