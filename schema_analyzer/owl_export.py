from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from .defaults import DEFAULT_OWL_BASE_IRI, DEFAULT_OWL_PHYSICAL_IRI
from .utils import conceptual_entities, conceptual_relationships, normalize_analysis_dict

if TYPE_CHECKING:
    from .types import AnalysisResult


#: The standard W3C prefixes every export declares, besides the base and ``phys:`` IRIs.
_W3C_PREFIXES: dict[str, str] = {
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}

#: ``phys:`` annotations a class carries, as ``(annotation local name, entity-mapping key)``.
#: The Turtle and JSON-LD exports both read this, so they carry the same annotations.
_ENTITY_ANNOTATIONS: tuple[tuple[str, str], ...] = (
    ("mappingStyle", "style"),
    ("collectionName", "collectionName"),
    ("typeField", "typeField"),
    ("typeValue", "typeValue"),
    # LABELS style: labelValue is one of the labels listed in labelsField.
    ("labelsField", "labelsField"),
    ("labelValue", "labelValue"),
)

#: ``phys:`` annotations an object property carries, as ``(annotation local name,
#: relationship-mapping key)``.
_RELATIONSHIP_ANNOTATIONS: tuple[tuple[str, str], ...] = (
    ("mappingStyle", "style"),
    ("edgeCollectionName", "edgeCollectionName"),
    ("typeField", "typeField"),
    ("typeValue", "typeValue"),
)

#: Every ``phys:`` annotation property the exports can write, declared once in the
#: Turtle header: the two mapping tables, the observed cardinality of a relationship,
#: and the location annotations of a datatype property.
_ANNOTATION_PROPERTIES: tuple[str, ...] = (
    *dict.fromkeys(name for name, _ in _ENTITY_ANNOTATIONS + _RELATIONSHIP_ANNOTATIONS),
    "observedCardinality",
    "attributeName",
    "attributePath",
    "multiValued",
)


def _phys_annotations(mapping: Any, table: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    """``(annotation local name, value)`` for each key of *table* set in *mapping*."""
    if not isinstance(mapping, dict):
        return []
    return [(name, str(mapping[key])) for name, key in table if mapping.get(key)]


def _mapped_schema(
    data: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """``(entities, relationships, entity mappings, relationship mappings)`` of an analysis.

    The well-formed entities and relationships of the conceptual schema, and the
    physical-mapping entries keyed by entity name and by relationship type (``{}``
    when missing).
    """
    cs = data.get("conceptualSchema") or {}
    pm = data.get("physicalMapping") or {}
    pm_entities = pm.get("entities")
    pm_rels = pm.get("relationships")
    return (
        conceptual_entities(cs),
        conceptual_relationships(cs),
        pm_entities if isinstance(pm_entities, dict) else {},
        pm_rels if isinstance(pm_rels, dict) else {},
    )


def _property_spec(mapping: Any, prop: str) -> dict[str, Any]:
    """The ``properties.<prop>`` entry of an entity mapping, or ``{}`` when it has none."""
    props = mapping.get("properties") if isinstance(mapping, dict) else None
    spec = props.get(prop) if isinstance(props, dict) else None
    return spec if isinstance(spec, dict) else {}


def _property_path(mapping: Any, prop: str) -> list[str] | None:
    """The ``path`` of *prop* in an entity mapping, or ``None`` when it has none."""
    path = _property_spec(mapping, prop).get("path")
    if isinstance(path, list) and path and all(isinstance(seg, str) for seg in path):
        return list(path)
    return None


def _property_multi_valued(mapping: Any, prop: str) -> bool:
    """Whether an entity mapping marks *prop* ``multiValued`` (its attribute holds a list)."""
    return _property_spec(mapping, prop).get("multiValued") is True


def _ttl_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _cardinality_characteristics(pattern: str | None) -> tuple[bool, bool]:
    """Map an observed cardinality pattern to (functional, inverse_functional).

    Functional ⇔ each subject has ≤1 object (low out-degree); inverse-functional
    ⇔ each object has ≤1 subject (low in-degree). Derived from sampled
    statistics, so it is an *observed* characteristic, not a guaranteed axiom.
    """
    return {
        "1:1": (True, True),
        "N:1": (True, False),
        "1:N": (False, True),
        "N:M": (False, False),
    }.get(pattern or "", (False, False))


def _cardinality_by_relationship(data: dict[str, Any]) -> dict[str, str]:
    meta = data.get("metadata")
    stats = meta.get("statistics") if isinstance(meta, dict) else None
    rels = stats.get("relationships") if isinstance(stats, dict) else None
    out: dict[str, str] = {}
    if isinstance(rels, dict):
        for rtype, block in rels.items():
            if isinstance(block, dict) and isinstance(block.get("cardinality_pattern"), str):
                out[rtype] = block["cardinality_pattern"]
    return out


def _subclass_edges(data: dict[str, Any]) -> list[tuple[str, str]]:
    """``(subclass, superclass)`` pairs from every source that proposes subsumption.

    Two sources today, and the function is shaped to take more:

    * **shard families** — each member of a structurally-identical group is modelled as an
      ``rdfs:subClassOf`` the synthesized family class. This is *physical duplication*, not
      conceptual subsumption, and was the only source until abstraction discovery landed.
    * **``conceptualSchema.subClassOfProposals``** — genuine taxonomy from
      ``conceptual-taxonomy`` (PRD §6.3): discriminator values, sibling abstraction via
      formal concept analysis, and shared-key specialization.

    Deduplicated, since a class can be proposed by more than one mechanism.
    """
    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(sub: Any, sup: Any) -> None:
        if isinstance(sub, str) and sub and isinstance(sup, str) and sup and sub != sup and (sub, sup) not in seen:
            seen.add((sub, sup))
            edges.append((sub, sup))

    physical_mapping = data.get("physicalMapping") or {}
    families = physical_mapping.get("shardFamilies")
    if isinstance(families, list):
        for fam in families:
            if not isinstance(fam, dict):
                continue
            fam_name = fam.get("name")
            members = fam.get("members")
            if not isinstance(fam_name, str) or not fam_name or not isinstance(members, list):
                continue
            for m in members:
                if isinstance(m, dict):
                    add(m.get("entity"), fam_name)

    conceptual = data.get("conceptualSchema") or {}
    proposals = conceptual.get("subClassOfProposals")
    if isinstance(proposals, list):
        for proposal in proposals:
            if isinstance(proposal, dict):
                add(proposal.get("subClass"), proposal.get("superClass"))

    return edges


def _sanitize_iri_local(name: str) -> str:
    """Sanitize a string for use as a Turtle IRI local name (PN_LOCAL)."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized or "_Unknown"


#: Property ``type`` values that name an XSD datatype. ``"string"`` is left out
#: on purpose: the deterministic baseline gives *every* property ``"string"``
#: without looking at the values (``baseline._extract_properties``), so it is
#: not evidence of a range; asserting ``xsd:string`` for an integer field would
#: be wrong. A property gets ``rdfs:range`` only from a type that was inferred.
_XSD_RANGE: dict[str, str] = {
    "integer": "integer",
    "int": "integer",
    "float": "double",
    "double": "double",
    "number": "double",
    "decimal": "decimal",
    "boolean": "boolean",
    "bool": "boolean",
    "date": "date",
    "datetime": "dateTime",
    "date-time": "dateTime",
    "timestamp": "dateTime",
}


def _datatype_properties(
    data: dict[str, Any], *, reserved: set[str]
) -> list[tuple[str, str, list[str], str | None, str | None, list[str] | None, bool]]:
    """Each entity property as one OWL datatype property.

    Returns ``(local_name, label, domain_entities, xsd_range, field, path, multi_valued)``
    sorted by local name. A property name shared by several entities (``content`` on a
    Chunk and a Document) is one property whose domain lists them all — the
    caller writes that as an ``owl:unionOf`` domain, since repeated
    ``rdfs:domain`` triples mean the *intersection* of the classes.

    * ``xsd_range`` — only when every entity that has the property agrees on an
      :data:`_XSD_RANGE` type; else ``None`` (no range asserted).
    * ``field`` — the stored document field when it is not the IRI local name
      (``physicalMapping.entities.*.properties.<name>.field``, or a name that
      had to be sanitized), so a consumer reads the right attribute; ``None``
      when the local name *is* the field, or when entities disagree.
    * ``path`` — for a value inside a nested object (issue #33), one name per
      level (``properties.<name>.path``); ``field`` is then ``None``, because no
      single stored attribute holds the value.
    * ``multi_valued`` — some entity's mapping marks the attribute as holding a list
      (``properties.<name>.multiValued``, :mod:`.list_values`): the export writes
      ``phys:multiValued "true"`` so a reader takes one value per element. One entity
      is enough, since reading a scalar as a one-element list loses nothing.

    A local name already used by a class or an object property (*reserved*) is
    skipped rather than punned, and so is a sanitized name two different
    property names collapse to — neither IRI could say which field it means.
    """
    entities, _, pm_entities, _ = _mapped_schema(data)
    by_local: dict[str, dict[str, Any]] = {}
    for e in entities:
        mapping = pm_entities.get(e["name"])
        props = e.get("properties")
        for prop in props if isinstance(props, list) else []:
            if not isinstance(prop, dict) or not isinstance(prop.get("name"), str) or not prop["name"]:
                continue
            raw = prop["name"]
            entry = by_local.setdefault(
                _sanitize_iri_local(raw),
                {"raws": set(), "domains": [], "types": set(), "fields": set(), "multi": False},
            )
            entry["raws"].add(raw)
            if e["name"] not in entry["domains"]:
                entry["domains"].append(e["name"])
            ptype = prop.get("type")
            entry["types"].add(_XSD_RANGE.get(ptype.strip().lower()) if isinstance(ptype, str) else None)
            if _property_multi_valued(mapping, raw):
                entry["multi"] = True
            path = _property_path(mapping, raw)
            if path is not None:
                # A value inside a nested object (issue #33) has no single stored
                # attribute name: it travels as phys:attributePath instead.
                entry["fields"].add(tuple(path) if len(path) > 1 else path[0])
                continue
            field = _property_spec(mapping, raw).get("field")
            entry["fields"].add(field if isinstance(field, str) and field else raw)

    out: list[tuple[str, str, list[str], str | None, str | None, list[str] | None, bool]] = []
    for local, entry in sorted(by_local.items()):
        if local in reserved or len(entry["raws"]) != 1:
            continue
        (raw,) = entry["raws"]
        xsd = next(iter(entry["types"])) if len(entry["types"]) == 1 else None
        location = next(iter(entry["fields"])) if len(entry["fields"]) == 1 else None
        if isinstance(location, tuple):
            out.append((local, raw, list(entry["domains"]), xsd, None, list(location), entry["multi"]))
        else:
            field = location if location != local else None
            out.append((local, raw, list(entry["domains"]), xsd, field, None, entry["multi"]))
    return out


def _reserved_locals(data: dict[str, Any]) -> set[str]:
    """IRI local names the export already gives a class or an object property."""
    cs = data.get("conceptualSchema") or {}
    names: set[str] = set()
    names.update(_sanitize_iri_local(e["name"]) for e in conceptual_entities(cs))
    names.update(_sanitize_iri_local(r["type"]) for r in conceptual_relationships(cs))
    for sub, sup in _subclass_edges(data):
        names.update((_sanitize_iri_local(sub), _sanitize_iri_local(sup)))
    return names


def export_conceptual_model_as_owl_turtle(
    analysis: AnalysisResult | dict[str, Any],
    *,
    base_iri: str = DEFAULT_OWL_BASE_IRI,
    phys_iri: str = DEFAULT_OWL_PHYSICAL_IRI,
) -> str:
    """
    Minimal OWL Turtle export for the conceptual schema + physical mappings.
    This is a normalized conceptual model intended to be used as the basis for:
    - LPG-like conceptual Cypher (labels + rel types)
    - SPARQL conceptual queries
    - physical mappings to guide translation to AQL
    """
    data = normalize_analysis_dict(analysis)

    entities, rels, pm_entities, pm_rels = _mapped_schema(data)

    lines: list[str] = []
    lines.append(f"@prefix : <{base_iri}> .")
    lines.extend(f"@prefix {prefix}: <{iri}> ." for prefix, iri in _W3C_PREFIXES.items())
    lines.append(f"@prefix phys: <{phys_iri}> .")
    lines.append("")
    lines.append(": a owl:Ontology ;")
    lines.append('  rdfs:label "Conceptual Schema" ;')
    lines.append('  rdfs:comment "Conceptual schema inferred from ArangoDB physical schema." .')
    lines.append("")

    # Declare annotation properties for physical mappings (best-effort; aligns with docs/OWL-FOR-CONCEPTUAL-SCHEMA.md)
    for ap in _ANNOTATION_PROPERTIES:
        lines.append(f"phys:{ap} a owl:AnnotationProperty .")
    lines.append("")

    # Classes
    for e in entities:
        name = e["name"]
        iri = f":{_sanitize_iri_local(name)}"
        lines.append(f"{iri} a owl:Class ;")
        lines.append(f'  rdfs:label "{_ttl_escape(name)}" .')
        for ap, value in _phys_annotations(pm_entities.get(name), _ENTITY_ANNOTATIONS):
            lines.append(f'{iri} phys:{ap} "{_ttl_escape(value)}" .')
        lines.append("")

    # Class hierarchy — shard families plus discovered abstractions (rdfs:subClassOf)
    subclass_edges = _subclass_edges(data)
    if subclass_edges:
        declared = {e["name"] for e in entities}
        # Only mint a class for a superclass that is not already an entity. A specialization
        # parent is a real collection, and a synthesized abstraction is merged into
        # `entities` — declaring either again would duplicate its label.
        family_names = sorted({fam for _, fam in subclass_edges if fam not in declared})
        for fam in family_names:
            fam_iri = f":{_sanitize_iri_local(fam)}"
            lines.append(f"{fam_iri} a owl:Class ;")
            lines.append(f'  rdfs:label "{_ttl_escape(fam)}" .')
        for member, fam in sorted(subclass_edges):
            lines.append(f":{_sanitize_iri_local(member)} rdfs:subClassOf :{_sanitize_iri_local(fam)} .")
        lines.append("")

    # Object properties (relationships)
    cardinality_by_rel = _cardinality_by_relationship(data)
    for r in rels:
        rtype = r["type"]
        from_e = r.get("fromEntity")
        to_e = r.get("toEntity")
        iri = f":{_sanitize_iri_local(rtype)}"
        lines.append(f"{iri} a owl:ObjectProperty ;")
        lines.append(f'  rdfs:label "{_ttl_escape(rtype)}" ;')
        if isinstance(from_e, str) and from_e:
            lines.append(f"  rdfs:domain :{_sanitize_iri_local(from_e)} ;")
        if isinstance(to_e, str) and to_e:
            lines.append(f"  rdfs:range :{_sanitize_iri_local(to_e)} ;")
        lines[-1] = lines[-1].rstrip(";") + " ." if lines[-1].endswith(";") else lines[-1]

        for ap, value in _phys_annotations(pm_rels.get(rtype), _RELATIONSHIP_ANNOTATIONS):
            lines.append(f'{iri} phys:{ap} "{_ttl_escape(value)}" .')

        pattern = cardinality_by_rel.get(rtype)
        if pattern:
            lines.append(f'{iri} phys:observedCardinality "{_ttl_escape(pattern)}" .')
            functional, inverse_functional = _cardinality_characteristics(pattern)
            if functional:
                lines.append(f"{iri} a owl:FunctionalProperty .")
            if inverse_functional:
                lines.append(f"{iri} a owl:InverseFunctionalProperty .")

        inverse_of = r.get("inverseOf")
        if isinstance(inverse_of, str) and inverse_of:
            lines.append(f"{iri} owl:inverseOf :{_sanitize_iri_local(inverse_of)} .")
        lines.append("")

    # Datatype properties — the entities' fields (docs/OWL-FOR-CONCEPTUAL-SCHEMA.md
    # "Data Properties"). Without these the ontology carried no field at all.
    for local, label, domains, xsd, field, path, multi in _datatype_properties(data, reserved=_reserved_locals(data)):
        iri = f":{local}"
        lines.append(f"{iri} a owl:DatatypeProperty ;")
        lines.append(f'  rdfs:label "{_ttl_escape(label)}" ;')
        domain_iris = [f":{_sanitize_iri_local(d)}" for d in domains]
        if len(domain_iris) == 1:
            lines.append(f"  rdfs:domain {domain_iris[0]} ;")
        else:
            lines.append(f"  rdfs:domain [ a owl:Class ; owl:unionOf ( {' '.join(domain_iris)} ) ] ;")
        if xsd:
            lines.append(f"  rdfs:range xsd:{xsd} ;")
        lines[-1] = lines[-1].rstrip(";").rstrip() + " ."
        if field:
            lines.append(f'{iri} phys:attributeName "{_ttl_escape(field)}" .')
        if path:
            # A JSON array, one name per level, as arango-sparql-py#84 reads it.
            lines.append(f'{iri} phys:attributePath "{_ttl_escape(json.dumps(path))}" .')
        if multi:
            # The attribute holds a list, one value per element (CDF #220).
            lines.append(f'{iri} phys:multiValued "true" .')
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def export_conceptual_model_as_jsonld(
    analysis: AnalysisResult | dict[str, Any],
    *,
    base_iri: str = DEFAULT_OWL_BASE_IRI,
    phys_iri: str = DEFAULT_OWL_PHYSICAL_IRI,
) -> dict[str, Any]:
    """JSON-LD serialization of the same OWL conceptual model as the Turtle export.

    Carries classes, ``rdfs:subClassOf`` hierarchy (from shard families), object
    properties with domain/range, observed cardinality + functional /
    inverse-functional characteristics (from statistics), explicit
    ``owl:inverseOf``, datatype properties (the entities' fields), and the
    ``phys:`` physical-mapping annotations.
    """
    data = normalize_analysis_dict(analysis)
    entities, rels, pm_entities, pm_rels = _mapped_schema(data)
    cardinality_by_rel = _cardinality_by_relationship(data)

    def iri(name: str) -> str:
        # A full IRI, as Turtle's ``:Local`` expands to. A bare local name is a relative
        # IRI: ``@vocab`` covers only property and type names, so a processor resolved it
        # against the document's location, and the JSON-LD named different resources
        # than the Turtle (relational-schema-analyzer#63). ``@base`` would drop the "#".
        return base_iri + _sanitize_iri_local(name)

    graph: list[dict[str, Any]] = []
    class_nodes: dict[str, dict[str, Any]] = {}

    for e in entities:
        name = e["name"]
        node: dict[str, Any] = {
            "@id": iri(name),
            "@type": "owl:Class",
            "rdfs:label": name,
        }
        for ap, value in _phys_annotations(pm_entities.get(name), _ENTITY_ANNOTATIONS):
            node[f"phys:{ap}"] = value
        class_nodes[name] = node
        graph.append(node)

    for member, fam in _subclass_edges(data):
        fam_id = iri(fam)
        if fam not in class_nodes:
            fam_node = {"@id": fam_id, "@type": "owl:Class", "rdfs:label": fam}
            class_nodes[fam] = fam_node
            graph.append(fam_node)
        member_node = class_nodes.get(member)
        if member_node is None:
            member_node = {"@id": iri(member), "@type": "owl:Class"}
            class_nodes[member] = member_node
            graph.append(member_node)
        member_node["rdfs:subClassOf"] = {"@id": fam_id}

    for r in rels:
        rtype = r["type"]
        types: list[str] = ["owl:ObjectProperty"]
        pattern = cardinality_by_rel.get(rtype)
        functional, inverse_functional = _cardinality_characteristics(pattern)
        if functional:
            types.append("owl:FunctionalProperty")
        if inverse_functional:
            types.append("owl:InverseFunctionalProperty")
        node = {"@id": iri(rtype), "@type": types, "rdfs:label": rtype}
        if isinstance(r.get("fromEntity"), str) and r["fromEntity"]:
            node["rdfs:domain"] = {"@id": iri(r["fromEntity"])}
        if isinstance(r.get("toEntity"), str) and r["toEntity"]:
            node["rdfs:range"] = {"@id": iri(r["toEntity"])}
        if pattern:
            node["phys:observedCardinality"] = pattern
        if isinstance(r.get("inverseOf"), str) and r["inverseOf"]:
            node["owl:inverseOf"] = {"@id": iri(r["inverseOf"])}
        for ap, value in _phys_annotations(pm_rels.get(rtype), _RELATIONSHIP_ANNOTATIONS):
            node[f"phys:{ap}"] = value
        graph.append(node)

    for local, label, domains, xsd, field, path, multi in _datatype_properties(data, reserved=_reserved_locals(data)):
        dp: dict[str, Any] = {"@id": base_iri + local, "@type": "owl:DatatypeProperty", "rdfs:label": label}
        domain_ids = [{"@id": iri(d)} for d in domains]
        dp["rdfs:domain"] = (
            domain_ids[0] if len(domain_ids) == 1 else {"@type": "owl:Class", "owl:unionOf": {"@list": domain_ids}}
        )
        if xsd:
            dp["rdfs:range"] = {"@id": f"xsd:{xsd}"}
        if field:
            dp["phys:attributeName"] = field
        if path:
            dp["phys:attributePath"] = json.dumps(path)
        if multi:
            dp["phys:multiValued"] = "true"
        graph.append(dp)

    return {
        "@context": {
            "@vocab": base_iri,
            **_W3C_PREFIXES,
            "phys": phys_iri,
        },
        "@graph": graph,
    }
