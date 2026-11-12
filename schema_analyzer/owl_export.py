from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from .defaults import DEFAULT_OWL_BASE_IRI, DEFAULT_OWL_PHYSICAL_IRI
from .utils import normalize_analysis_dict

if TYPE_CHECKING:
    from .types import AnalysisResult


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
    cs = data.get("conceptualSchema") or {}
    pm_entities = (data.get("physicalMapping") or {}).get("entities")
    pm_entities = pm_entities if isinstance(pm_entities, dict) else {}
    entities = cs.get("entities")
    by_local: dict[str, dict[str, Any]] = {}
    for e in entities if isinstance(entities, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"]:
            continue
        mapping = pm_entities.get(e["name"])
        prop_map = mapping.get("properties") if isinstance(mapping, dict) else None
        prop_map = prop_map if isinstance(prop_map, dict) else {}
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
            spec = prop_map.get(raw)
            if isinstance(spec, dict) and spec.get("multiValued") is True:
                entry["multi"] = True
            path = spec.get("path") if isinstance(spec, dict) else None
            if isinstance(path, list) and path and all(isinstance(seg, str) for seg in path):
                # A value inside a nested object (issue #33) has no single stored
                # attribute name: it travels as phys:attributePath instead.
                entry["fields"].add(tuple(path) if len(path) > 1 else path[0])
                continue
            field = spec.get("field") if isinstance(spec, dict) else None
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
    for e in cs.get("entities") or []:
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"]:
            names.add(_sanitize_iri_local(e["name"]))
    for r in cs.get("relationships") or []:
        if isinstance(r, dict) and isinstance(r.get("type"), str) and r["type"]:
            names.add(_sanitize_iri_local(r["type"]))
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

    cs = data.get("conceptualSchema") or {}
    pm = data.get("physicalMapping") or {}

    entities = cs.get("entities") or []
    rels = cs.get("relationships") or []

    lines: list[str] = []
    lines.append(f"@prefix : <{base_iri}> .")
    lines.append("@prefix owl: <http://www.w3.org/2002/07/owl#> .")
    lines.append("@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .")
    lines.append("@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .")
    lines.append("@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .")
    lines.append(f"@prefix phys: <{phys_iri}> .")
    lines.append("")
    lines.append(": a owl:Ontology ;")
    lines.append('  rdfs:label "Conceptual Schema" ;')
    lines.append('  rdfs:comment "Conceptual schema inferred from ArangoDB physical schema." .')
    lines.append("")

    # Declare annotation properties for physical mappings (best-effort; aligns with docs/OWL-FOR-CONCEPTUAL-SCHEMA.md)
    for ap in [
        "mappingStyle",
        "collectionName",
        "typeField",
        "typeValue",
        "labelsField",
        "labelValue",
        "edgeCollectionName",
        "attributeName",
        "attributePath",
        "multiValued",
    ]:
        lines.append(f"phys:{ap} a owl:AnnotationProperty .")
    lines.append("")

    # Classes
    if isinstance(entities, list):
        for e in entities:
            if not isinstance(e, dict):
                continue
            name = e.get("name")
            if not isinstance(name, str) or not name:
                continue
            safe = _sanitize_iri_local(name)
            iri = f":{safe}"
            lines.append(f"{iri} a owl:Class ;")
            lines.append(f'  rdfs:label "{_ttl_escape(name)}" .')
            mapping = (pm.get("entities") or {}).get(name) if isinstance(pm.get("entities"), dict) else None
            if isinstance(mapping, dict):
                style = mapping.get("style")
                if style:
                    lines.append(f'{iri} phys:mappingStyle "{_ttl_escape(str(style))}" .')
                if mapping.get("collectionName"):
                    lines.append(f'{iri} phys:collectionName "{_ttl_escape(str(mapping["collectionName"]))}" .')
                if mapping.get("typeField"):
                    lines.append(f'{iri} phys:typeField "{_ttl_escape(str(mapping["typeField"]))}" .')
                if mapping.get("typeValue"):
                    lines.append(f'{iri} phys:typeValue "{_ttl_escape(str(mapping["typeValue"]))}" .')
                if mapping.get("labelsField"):
                    # LABELS style: labelValue is one of the labels listed in this field.
                    lines.append(f'{iri} phys:labelsField "{_ttl_escape(str(mapping["labelsField"]))}" .')
                if mapping.get("labelValue"):
                    lines.append(f'{iri} phys:labelValue "{_ttl_escape(str(mapping["labelValue"]))}" .')
            lines.append("")

    # Class hierarchy — shard families plus discovered abstractions (rdfs:subClassOf)
    subclass_edges = _subclass_edges(data)
    if subclass_edges:
        declared = {e["name"] for e in entities if isinstance(e, dict) and isinstance(e.get("name"), str)}
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
    if isinstance(rels, list):
        for r in rels:
            if not isinstance(r, dict):
                continue
            rtype = r.get("type")
            if not isinstance(rtype, str) or not rtype:
                continue
            from_e = r.get("fromEntity")
            to_e = r.get("toEntity")
            safe_rtype = _sanitize_iri_local(rtype)
            iri = f":{safe_rtype}"
            lines.append(f"{iri} a owl:ObjectProperty ;")
            lines.append(f'  rdfs:label "{_ttl_escape(rtype)}" ;')
            if isinstance(from_e, str) and from_e:
                lines.append(f"  rdfs:domain :{_sanitize_iri_local(from_e)} ;")
            if isinstance(to_e, str) and to_e:
                lines.append(f"  rdfs:range :{_sanitize_iri_local(to_e)} ;")
            lines[-1] = lines[-1].rstrip(";") + " ." if lines[-1].endswith(";") else lines[-1]

            mapping = (pm.get("relationships") or {}).get(rtype) if isinstance(pm.get("relationships"), dict) else None
            if isinstance(mapping, dict):
                style = mapping.get("style")
                if style:
                    lines.append(f'{iri} phys:mappingStyle "{_ttl_escape(str(style))}" .')
                if mapping.get("edgeCollectionName"):
                    val = _ttl_escape(str(mapping["edgeCollectionName"]))
                    lines.append(f'{iri} phys:edgeCollectionName "{val}" .')
                if mapping.get("typeField"):
                    lines.append(f'{iri} phys:typeField "{_ttl_escape(str(mapping["typeField"]))}" .')
                if mapping.get("typeValue"):
                    lines.append(f'{iri} phys:typeValue "{_ttl_escape(str(mapping["typeValue"]))}" .')

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
    cardinality_by_rel = _cardinality_by_relationship(data)

    graph: list[dict[str, Any]] = []
    class_nodes: dict[str, dict[str, Any]] = {}

    for e in entities:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"]:
            continue
        name = e["name"]
        node: dict[str, Any] = {
            "@id": _sanitize_iri_local(name),
            "@type": "owl:Class",
            "rdfs:label": name,
        }
        mapping = pm_entities.get(name)
        if isinstance(mapping, dict):
            for phys_key, mkey in (
                ("mappingStyle", "style"),
                ("collectionName", "collectionName"),
                ("typeField", "typeField"),
                ("typeValue", "typeValue"),
                ("labelsField", "labelsField"),
                ("labelValue", "labelValue"),
            ):
                if mapping.get(mkey):
                    node[f"phys:{phys_key}"] = str(mapping[mkey])
        class_nodes[name] = node
        graph.append(node)

    for member, fam in _subclass_edges(data):
        fam_id = _sanitize_iri_local(fam)
        if fam not in class_nodes:
            fam_node = {"@id": fam_id, "@type": "owl:Class", "rdfs:label": fam}
            class_nodes[fam] = fam_node
            graph.append(fam_node)
        member_node = class_nodes.get(member)
        if member_node is None:
            member_node = {"@id": _sanitize_iri_local(member), "@type": "owl:Class"}
            class_nodes[member] = member_node
            graph.append(member_node)
        member_node["rdfs:subClassOf"] = {"@id": fam_id}

    for r in rels:
        if not isinstance(r, dict) or not isinstance(r.get("type"), str) or not r["type"]:
            continue
        rtype = r["type"]
        types: list[str] = ["owl:ObjectProperty"]
        pattern = cardinality_by_rel.get(rtype)
        functional, inverse_functional = _cardinality_characteristics(pattern)
        if functional:
            types.append("owl:FunctionalProperty")
        if inverse_functional:
            types.append("owl:InverseFunctionalProperty")
        node = {"@id": _sanitize_iri_local(rtype), "@type": types, "rdfs:label": rtype}
        if isinstance(r.get("fromEntity"), str) and r["fromEntity"]:
            node["rdfs:domain"] = {"@id": _sanitize_iri_local(r["fromEntity"])}
        if isinstance(r.get("toEntity"), str) and r["toEntity"]:
            node["rdfs:range"] = {"@id": _sanitize_iri_local(r["toEntity"])}
        if pattern:
            node["phys:observedCardinality"] = pattern
        if isinstance(r.get("inverseOf"), str) and r["inverseOf"]:
            node["owl:inverseOf"] = {"@id": _sanitize_iri_local(r["inverseOf"])}
        mapping = pm_rels.get(rtype)
        if isinstance(mapping, dict):
            if mapping.get("style"):
                node["phys:mappingStyle"] = str(mapping["style"])
            if mapping.get("edgeCollectionName"):
                node["phys:edgeCollectionName"] = str(mapping["edgeCollectionName"])
        graph.append(node)

    for local, label, domains, xsd, field, path, multi in _datatype_properties(data, reserved=_reserved_locals(data)):
        dp: dict[str, Any] = {"@id": local, "@type": "owl:DatatypeProperty", "rdfs:label": label}
        domain_ids = [{"@id": _sanitize_iri_local(d)} for d in domains]
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
            "owl": "http://www.w3.org/2002/07/owl#",
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
            "phys": phys_iri,
        },
        "@graph": graph,
    }
