from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .defaults import DEFAULT_OWL_BASE_IRI, DEFAULT_OWL_PHYSICAL_IRI
from .errors import SchemaAnalyzerError
from .mapping import PhysicalMapping
from .owl_export import _sanitize_iri_local
from .utils import normalize_analysis_dict

if TYPE_CHECKING:
    from .types import AnalysisResult

SUPPORTED_EXPORT_TARGETS = ("cypher", "sparql")

# Resolution keys copied verbatim from a physical-mapping entry into a
# transpiler-facing block. Kept small and explicit so the export contract is
# stable even if the internal mapping grows new bookkeeping fields.
# ``aliases`` carries the raw discriminator value(s) whose PascalCase-derived
# entity name is lossy (FIN_METRIC → FINMETRIC), so a transpiler can resolve
# the label a Cypher author actually sees in the data.
_ENTITY_RESOLUTION_KEYS = (
    "style",
    "collectionName",
    "typeField",
    "typeValue",
    "labelsField",
    "labelValue",
    "aliases",
)
_RELATIONSHIP_RESOLUTION_KEYS = ("style", "edgeCollectionName", "typeField", "typeValue")


def _physical_block(mapping: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    return {k: mapping[k] for k in keys if k in mapping and mapping[k] is not None}


def _property_path(mapping: Any, prop: str) -> list[str] | None:
    """The ``path`` of *prop* in an entity mapping, or ``None`` when it has none."""
    props = mapping.get("properties") if isinstance(mapping, dict) else None
    spec = props.get(prop) if isinstance(props, dict) else None
    path = spec.get("path") if isinstance(spec, dict) else None
    if isinstance(path, list) and path and all(isinstance(seg, str) for seg in path):
        return list(path)
    return None


def _property_multi_valued(mapping: Any, prop: str) -> bool:
    """Whether an entity mapping marks *prop* ``multiValued`` (its attribute holds a list)."""
    props = mapping.get("properties") if isinstance(mapping, dict) else None
    spec = props.get(prop) if isinstance(props, dict) else None
    return isinstance(spec, dict) and spec.get("multiValued") is True


def export_mapping(analysis: AnalysisResult | dict[str, Any], target: str = "cypher") -> dict[str, Any]:
    """Export the analysis into a stable JSON contract consumable by transpilers.

    Supported targets:

    * ``"cypher"`` — the normalized ``{conceptualSchema, physicalMapping,
      metadata}`` bundle that the Cypher transpiler consumes directly. See
      :func:`build_cypher_resolution_index` for a flattened label/rel-type → AQL
      lookup built on top of this.
    * ``"sparql"`` — an RDF vocabulary view (classes, object properties with
      IRIs/domains/ranges, and datatype properties for entity literal
      attributes) annotated with the physical mapping so a SPARQL→AQL
      transpiler can resolve triple patterns to collections, edge traversals,
      and document-field accesses. See ``docs/transpiler-integration.md``.
    """
    if target not in SUPPORTED_EXPORT_TARGETS:
        raise ValueError(f"Unsupported export target: {target}")

    data = normalize_analysis_dict(analysis)

    if target == "sparql":
        return _export_sparql(data)

    return {
        "conceptualSchema": data.get("conceptualSchema"),
        "physicalMapping": data.get("physicalMapping"),
        "metadata": data.get("metadata"),
    }


def _export_sparql(
    data: dict[str, Any],
    *,
    base_iri: str = DEFAULT_OWL_BASE_IRI,
    phys_iri: str = DEFAULT_OWL_PHYSICAL_IRI,
) -> dict[str, Any]:
    cs = data.get("conceptualSchema") or {}
    pm = data.get("physicalMapping") or {}
    raw_entities = cs.get("entities")
    entities = raw_entities if isinstance(raw_entities, list) else []
    raw_rels = cs.get("relationships")
    rels = raw_rels if isinstance(raw_rels, list) else []
    raw_pm_entities = pm.get("entities")
    pm_entities = raw_pm_entities if isinstance(raw_pm_entities, dict) else {}
    raw_pm_rels = pm.get("relationships")
    pm_rels = raw_pm_rels if isinstance(raw_pm_rels, dict) else {}

    classes: list[dict[str, Any]] = []
    for e in entities:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"]:
            continue
        name = e["name"]
        local = _sanitize_iri_local(name)
        classes.append(
            {
                "iri": f"{base_iri}{local}",
                "localName": local,
                "label": name,
                "physical": _physical_block(pm_entities.get(name), _ENTITY_RESOLUTION_KEYS),
            }
        )

    # Datatype properties: literal attributes on each entity, so a SPARQL→AQL
    # transpiler can resolve literal-valued triple patterns (e.g. ?u :email ?e)
    # to a document-field access on the entity's collection. One entry per
    # (entity, attribute); the same attribute on two entities yields two entries
    # with distinct domains (the RDF "property reused across classes" case).
    datatype_properties: list[dict[str, Any]] = []
    for e in entities:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"]:
            continue
        name = e["name"]
        domain_iri = f"{base_iri}{_sanitize_iri_local(name)}"
        phys = _physical_block(pm_entities.get(name), _ENTITY_RESOLUTION_KEYS)
        props = e.get("properties")
        for prop in props if isinstance(props, list) else []:
            attr = prop.get("name") if isinstance(prop, dict) else None
            if not isinstance(attr, str) or not attr:
                continue
            local = _sanitize_iri_local(attr)
            dp: dict[str, Any] = {
                "iri": f"{base_iri}{local}",
                "localName": local,
                "label": attr,
                "domain": domain_iri,
                # The physical document field the literal lives in, plus the
                # owning entity's collection resolution.
                "attribute": attr,
                "physical": phys,
            }
            # A value inside a nested object (issue #33): one name per level, so a
            # dotted ``attribute`` is never read as one literal name by mistake.
            path = _property_path(pm_entities.get(name), attr)
            if path is not None:
                dp["path"] = path
            # The attribute holds a list, one value per element (CDF #220).
            if _property_multi_valued(pm_entities.get(name), attr):
                dp["multiValued"] = True
            datatype_properties.append(dp)

    object_properties: list[dict[str, Any]] = []
    for r in rels:
        if not isinstance(r, dict) or not isinstance(r.get("type"), str) or not r["type"]:
            continue
        rtype = r["type"]
        local = _sanitize_iri_local(rtype)
        entry: dict[str, Any] = {
            "iri": f"{base_iri}{local}",
            "localName": local,
            "label": rtype,
            "physical": _physical_block(pm_rels.get(rtype), _RELATIONSHIP_RESOLUTION_KEYS),
        }
        frm = r.get("fromEntity")
        to = r.get("toEntity")
        if isinstance(frm, str) and frm:
            entry["domain"] = f"{base_iri}{_sanitize_iri_local(frm)}"
        if isinstance(to, str) and to:
            entry["range"] = f"{base_iri}{_sanitize_iri_local(to)}"
        object_properties.append(entry)

    return {
        "target": "sparql",
        "prefixes": {
            "": base_iri,
            "phys": phys_iri,
            "owl": "http://www.w3.org/2002/07/owl#",
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
        },
        "classes": classes,
        "objectProperties": object_properties,
        "datatypeProperties": datatype_properties,
        "physicalMapping": pm,
    }


def build_cypher_resolution_index(analysis: AnalysisResult | dict[str, Any]) -> dict[str, Any]:
    """Flattened label/relationship-type → AQL lookup for a Cypher transpiler.

    Where the ``"cypher"`` export target hands back the raw conceptual schema +
    physical mapping, this adapter does the next step the
    `arango-cypher <https://pypi.org/project/arango-cypher/>`_ transpiler would
    otherwise repeat: for every entity label it precomputes the injection-safe
    AQL match fragment, and for every relationship type the traversal fragment,
    via :class:`~schema_analyzer.mapping.PhysicalMapping`.

    Entries whose mapping is incomplete (e.g. a ``LABEL`` style missing its
    ``typeValue``) are emitted with an ``error`` field instead of ``aql`` so the
    transpiler can surface a precise diagnostic rather than crash.
    """
    data = normalize_analysis_dict(analysis)
    pm = PhysicalMapping.from_json(data.get("physicalMapping") or {})

    entities: dict[str, Any] = {}
    for label, mapping in sorted(pm.entities.items()):
        block = _physical_block(mapping, _ENTITY_RESOLUTION_KEYS)
        try:
            frag = pm.aql_entity_match(variable="n", entity_type=label)
            block["aql"] = {"query": frag["query"], "bindVars": frag["bind_vars"]}
        except SchemaAnalyzerError as e:
            block["error"] = {"code": e.code, "message": str(e)}
        entities[label] = block

    relationships: dict[str, Any] = {}
    for rtype, mapping in sorted(pm.relationships.items()):
        block = _physical_block(mapping, _RELATIONSHIP_RESOLUTION_KEYS)
        try:
            frag = pm.aql_relationship_traversal(from_variable="a", rel_type=rtype, to_variable="b")
            block["aql"] = {
                "query": frag["query"],
                "bindVars": frag["bind_vars"],
                "edgeVariable": frag["edge_variable"],
            }
        except SchemaAnalyzerError as e:
            block["error"] = {"code": e.code, "message": str(e)}
        relationships[rtype] = block

    return {"target": "cypher", "entities": entities, "relationships": relationships}
