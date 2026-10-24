from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import SchemaAnalyzerError
from .utils import assert_aql_identifier

EntityMappingStyle = Literal["COLLECTION", "LABEL", "LABELS"]
RelationshipMappingStyle = Literal[
    "DEDICATED_COLLECTION",
    "GENERIC_WITH_TYPE",
    # Relational-in-ArangoDB patterns (PRD §3.3). Names are deliberately shared with
    # `relational-schema-analyzer`, which already emits both for the same physical
    # patterns — the addressing differs (collection/field vs table/column), the pattern
    # does not, and forking the vocabulary of a deliberately-shared bundle shape would
    # cost every downstream consumer a translation layer.
    "FOREIGN_KEY",
    "JOIN_TABLE",
]


@dataclass
class PhysicalMapping:
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationships: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Shard-family records produced by
    # :func:`schema_analyzer.shard_families.detect_shard_families`
    # (PRD §6.2 bullet 5). Optional and additive: ``None`` means the
    # detector did not run (older fixtures, baseline-only paths that
    # opt out, or hand-crafted mappings imported via ``from_json``);
    # an empty list means the detector ran but found no families.
    # Preserves byte-identity with pre-detector output when ``None``.
    shard_families: list[dict[str, Any]] | None = None

    @classmethod
    def empty(cls) -> PhysicalMapping:
        return cls()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PhysicalMapping:
        ent = data.get("entities", {})
        rel = data.get("relationships", {})
        fam = data.get("shardFamilies")
        return cls(
            entities=dict(ent) if isinstance(ent, dict) else {},
            relationships=dict(rel) if isinstance(rel, dict) else {},
            shard_families=list(fam) if isinstance(fam, list) else None,
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"entities": self.entities, "relationships": self.relationships}
        if self.shard_families is not None:
            out["shardFamilies"] = self.shard_families
        return out

    def get_entity_mapping(self, entity_type: str) -> dict[str, Any] | None:
        return self.entities.get(entity_type)

    def get_relationship_mapping(self, rel_type: str) -> dict[str, Any] | None:
        return self.relationships.get(rel_type)

    def aql_entity_match(self, *, variable: str, entity_type: str) -> dict[str, Any]:
        """
        Injection-safe AQL fragment for matching an entity.
        Returns: {"query": str, "bind_vars": dict}
        """
        assert_aql_identifier("variable", variable)
        if not isinstance(entity_type, str) or not entity_type:
            raise SchemaAnalyzerError("Invalid entity_type", code="INVALID_ARGUMENT")

        mapping = self.get_entity_mapping(entity_type)
        if not mapping:
            raise SchemaAnalyzerError(f"No entity mapping for: {entity_type}", code="MAPPING_NOT_FOUND")

        style = mapping.get("style")
        bind_vars: dict[str, Any] = {}

        if style == "COLLECTION":
            collection_name = mapping.get("collectionName")
            if not collection_name:
                raise SchemaAnalyzerError(
                    f"COLLECTION mapping missing collectionName for: {entity_type}", code="INVALID_MAPPING"
                )
            bind_vars["@collection"] = collection_name
            return {"query": f"FOR {variable} IN @@collection", "bind_vars": bind_vars}

        if style == "LABEL":
            collection_name = mapping.get("collectionName")
            type_field = mapping.get("typeField")
            type_value = mapping.get("typeValue")
            if not (collection_name and type_field and type_value):
                raise SchemaAnalyzerError(
                    f"LABEL mapping requires collectionName, typeField, typeValue for: {entity_type}",
                    code="INVALID_MAPPING",
                )
            bind_vars["@collection"] = collection_name
            bind_vars["typeField"] = type_field
            bind_vars["typeValue"] = type_value
            return {
                "query": f"FOR {variable} IN @@collection FILTER {variable}[@typeField] == @typeValue",
                "bind_vars": bind_vars,
            }

        if style == "LABELS":
            # Labels held in an array field of a shared collection (a node may carry several).
            collection_name = mapping.get("collectionName")
            labels_field = mapping.get("labelsField")
            label = mapping.get("typeValue") or entity_type
            if not (collection_name and labels_field):
                raise SchemaAnalyzerError(
                    f"LABELS mapping requires collectionName, labelsField for: {entity_type}",
                    code="INVALID_MAPPING",
                )
            bind_vars["@collection"] = collection_name
            bind_vars["labelsField"] = labels_field
            bind_vars["label"] = label
            return {
                "query": f"FOR {variable} IN @@collection FILTER @label IN {variable}[@labelsField]",
                "bind_vars": bind_vars,
            }

        raise SchemaAnalyzerError(f"Unsupported entity mapping style: {style}", code="INVALID_MAPPING")

    def aql_class_extent(
        self,
        *,
        variable: str,
        realizations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Injection-safe AQL enumerating every document belonging to an abstract class.

        The abstract-class analogue of :meth:`aql_entity_match`, and the piece that makes an
        aggregate over a synthesized class — "total balance across all accounts" — something
        a consumer can actually compile. ``realizations`` comes from
        ``conceptual-taxonomy``'s ``abstractClasses[*].realizations``; it deliberately mixes
        styles, because that is what makes the sibling-collection and discriminated-collection
        layouts indistinguishable at the conceptual layer.

        ``COLLECTION`` members contribute their whole collection; ``LABEL`` members
        contribute a discriminator-filtered slice. Members sharing one collection are folded
        into a single pass with an ``IN`` over their type values rather than a UNION of
        near-identical scans.

        Returns ``{"query", "bind_vars"}`` where the query binds ``variable`` to each member
        document exactly once.
        """
        assert_aql_identifier("variable", variable)
        if not isinstance(realizations, list) or not realizations:
            raise SchemaAnalyzerError("realizations must be a non-empty list", code="INVALID_ARGUMENT")

        whole: list[str] = []
        filtered: dict[str, tuple[str, list[str]]] = {}

        for index, realization in enumerate(realizations):
            if not isinstance(realization, dict):
                raise SchemaAnalyzerError(f"realization {index} is not an object", code="INVALID_MAPPING")
            collection_name = realization.get("collectionName")
            if not collection_name:
                raise SchemaAnalyzerError(f"realization {index} is missing collectionName", code="INVALID_MAPPING")
            style = realization.get("style")
            if style == "LABEL":
                type_field = realization.get("typeField")
                type_value = realization.get("typeValue")
                if not (type_field and type_value):
                    raise SchemaAnalyzerError(
                        f"LABEL realization {index} requires typeField and typeValue",
                        code="INVALID_MAPPING",
                    )
                existing = filtered.get(collection_name)
                if existing is None:
                    filtered[collection_name] = (str(type_field), [str(type_value)])
                elif existing[0] != type_field:
                    raise SchemaAnalyzerError(
                        f"conflicting typeField for collection '{collection_name}'",
                        code="INVALID_MAPPING",
                    )
                elif type_value not in existing[1]:
                    existing[1].append(str(type_value))
            else:
                if collection_name not in whole:
                    whole.append(collection_name)

        # A collection read in full subsumes any discriminator-filtered slice of itself.
        for name in whole:
            filtered.pop(name, None)

        bind_vars: dict[str, Any] = {}
        branches: list[str] = []

        for index, name in enumerate(whole):
            bind_vars[f"@extent{index}"] = name
            branches.append(f"(FOR d IN @@extent{index} RETURN d)")

        for offset, (name, (type_field, type_values)) in enumerate(sorted(filtered.items())):
            index = len(whole) + offset
            bind_vars[f"@extent{index}"] = name
            bind_vars[f"typeField{index}"] = type_field
            bind_vars[f"typeValues{index}"] = sorted(type_values)
            branches.append(f"(FOR d IN @@extent{index} FILTER d[@typeField{index}] IN @typeValues{index} RETURN d)")

        if len(branches) == 1:
            query = f"FOR {variable} IN {branches[0]}"
        else:
            query = f"FOR {variable} IN UNION({', '.join(branches)})"
        return {"query": query, "bind_vars": bind_vars}

    def aql_relationship_traversal(
        self,
        *,
        from_variable: str,
        rel_type: str,
        to_variable: str,
        edge_variable: str = "e",
        direction: Literal["outbound", "inbound"] = "outbound",
    ) -> dict[str, Any]:
        """
        Minimal AQL fragment to traverse an edge collection and load the other endpoint with DOCUMENT().
        Returns: {"query": str, "bind_vars": dict, "edge_variable": str}
        """
        assert_aql_identifier("from_variable", from_variable)
        assert_aql_identifier("to_variable", to_variable)
        assert_aql_identifier("edge_variable", edge_variable)
        if not isinstance(rel_type, str) or not rel_type:
            raise SchemaAnalyzerError("Invalid rel_type", code="INVALID_ARGUMENT")
        if direction not in ("outbound", "inbound"):
            raise SchemaAnalyzerError(f"Invalid direction: {direction}", code="INVALID_ARGUMENT")

        mapping = self.get_relationship_mapping(rel_type)
        if not mapping:
            raise SchemaAnalyzerError(f"No relationship mapping for: {rel_type}", code="MAPPING_NOT_FOUND")

        from_field = "_from" if direction == "outbound" else "_to"
        to_field = "_to" if direction == "outbound" else "_from"

        bind_vars: dict[str, Any] = {}
        style = mapping.get("style")

        if style == "DEDICATED_COLLECTION":
            edge_collection_name = mapping.get("edgeCollectionName")
            if not edge_collection_name:
                raise SchemaAnalyzerError(
                    f"DEDICATED_COLLECTION mapping missing edgeCollectionName for: {rel_type}", code="INVALID_MAPPING"
                )
            bind_vars["@edgeCollection"] = edge_collection_name
            query = "\n".join(
                [
                    f"FOR {edge_variable} IN @@edgeCollection",
                    f"  FILTER {edge_variable}.{from_field} == {from_variable}._id",
                    f"  LET {to_variable} = DOCUMENT({edge_variable}.{to_field})",
                ]
            )
            return {"edge_variable": edge_variable, "bind_vars": bind_vars, "query": query}

        if style == "GENERIC_WITH_TYPE":
            edge_collection_name = mapping.get("edgeCollectionName")
            type_field = mapping.get("typeField")
            type_value = mapping.get("typeValue")
            if not (edge_collection_name and type_field and type_value):
                raise SchemaAnalyzerError(
                    f"GENERIC_WITH_TYPE mapping requires edgeCollectionName, typeField, typeValue for: {rel_type}",
                    code="INVALID_MAPPING",
                )
            bind_vars["@edgeCollection"] = edge_collection_name
            bind_vars["typeField"] = type_field
            bind_vars["typeValue"] = type_value
            query = "\n".join(
                [
                    f"FOR {edge_variable} IN @@edgeCollection",
                    f"  FILTER {edge_variable}.{from_field} == {from_variable}._id",
                    f"  FILTER {edge_variable}[@typeField] == @typeValue",
                    f"  LET {to_variable} = DOCUMENT({edge_variable}.{to_field})",
                ]
            )
            return {"edge_variable": edge_variable, "bind_vars": bind_vars, "query": query}

        if style == "FOREIGN_KEY":
            return self._aql_foreign_key(
                mapping=mapping,
                rel_type=rel_type,
                from_variable=from_variable,
                to_variable=to_variable,
                direction=direction,
            )

        if style == "JOIN_TABLE":
            return self._aql_join_table(
                mapping=mapping,
                rel_type=rel_type,
                from_variable=from_variable,
                to_variable=to_variable,
                edge_variable=edge_variable,
                direction=direction,
            )

        raise SchemaAnalyzerError(f"Unsupported relationship mapping style: {style}", code="INVALID_MAPPING")

    @staticmethod
    def _field_list(mapping: dict[str, Any], key: str, rel_type: str) -> list[str]:
        raw = mapping.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw or not all(isinstance(f, str) and f for f in raw):
            raise SchemaAnalyzerError(
                f"{mapping.get('style')} mapping requires a non-empty {key} for: {rel_type}",
                code="INVALID_MAPPING",
            )
        return list(raw)

    def _aql_foreign_key(
        self,
        *,
        mapping: dict[str, Any],
        rel_type: str,
        from_variable: str,
        to_variable: str,
        direction: str,
    ) -> dict[str, Any]:
        """A relationship carried by a scalar attribute rather than an edge collection.

        There is no edge document, so ``edge_variable`` is ``None`` — a consumer that needs
        edge properties should check for it rather than assume one exists. Composite keys
        are supported: ``fromFields`` and ``toKeyFields`` are zipped positionally.
        """
        from_collection = mapping.get("fromCollection")
        to_collection = mapping.get("toCollection")
        if not from_collection or not to_collection:
            raise SchemaAnalyzerError(
                f"FOREIGN_KEY mapping requires fromCollection and toCollection for: {rel_type}",
                code="INVALID_MAPPING",
            )

        from_fields = self._field_list(mapping, "fromFields", rel_type)
        to_key_fields = self._field_list(mapping, "toKeyFields", rel_type)
        if len(from_fields) != len(to_key_fields):
            raise SchemaAnalyzerError(
                f"FOREIGN_KEY mapping fromFields/toKeyFields length mismatch for: {rel_type}",
                code="INVALID_MAPPING",
            )

        # outbound: the bound variable holds the referencing document, so we resolve the
        # referenced one. inbound reverses it — find every document pointing at this one.
        outbound = direction == "outbound"
        target_collection = to_collection if outbound else from_collection
        bound_fields = from_fields if outbound else to_key_fields
        target_fields = to_key_fields if outbound else from_fields

        bind_vars: dict[str, Any] = {"@collection": target_collection}
        filters: list[str] = []
        for index, (target_field, bound_field) in enumerate(zip(target_fields, bound_fields, strict=True)):
            bind_vars[f"targetField{index}"] = target_field
            bind_vars[f"boundField{index}"] = bound_field
            filters.append(f"{to_variable}[@targetField{index}] == {from_variable}[@boundField{index}]")

        query = "\n".join(
            [
                f"FOR {to_variable} IN @@collection",
                f"  FILTER {' AND '.join(filters)}",
            ]
        )
        return {"edge_variable": None, "bind_vars": bind_vars, "query": query}

    def _aql_join_table(
        self,
        *,
        mapping: dict[str, Any],
        rel_type: str,
        from_variable: str,
        to_variable: str,
        edge_variable: str,
        direction: str,
    ) -> dict[str, Any]:
        """A relationship reified as a document collection linking two entities.

        Emits the full two-hop traversal for a conceptually one-hop relationship — the
        reification is a physical detail and must not leak into the conceptual layer. The
        join row is bound to ``edge_variable`` because it plays exactly the role an edge
        document does: it is where ``attributeFields`` live.
        """
        join_collection = mapping.get("joinCollection")
        from_collection = mapping.get("fromCollection")
        to_collection = mapping.get("toCollection")
        if not join_collection or not from_collection or not to_collection:
            raise SchemaAnalyzerError(
                f"JOIN_TABLE mapping requires joinCollection, fromCollection and toCollection for: {rel_type}",
                code="INVALID_MAPPING",
            )

        near_join = self._field_list(mapping, "joinFromFields", rel_type)
        near_parent = self._field_list(mapping, "joinFromParentFields", rel_type)
        far_join = self._field_list(mapping, "joinToFields", rel_type)
        far_parent = self._field_list(mapping, "joinToParentFields", rel_type)
        if len(near_join) != len(near_parent) or len(far_join) != len(far_parent):
            raise SchemaAnalyzerError(
                f"JOIN_TABLE mapping join/parent field length mismatch for: {rel_type}",
                code="INVALID_MAPPING",
            )

        if direction != "outbound":
            near_join, far_join = far_join, near_join
            near_parent, far_parent = far_parent, near_parent
            to_collection = from_collection

        bind_vars: dict[str, Any] = {
            "@joinCollection": join_collection,
            "@collection": to_collection,
        }

        near_filters: list[str] = []
        for index, (join_field, parent_field) in enumerate(zip(near_join, near_parent, strict=True)):
            bind_vars[f"nearJoinField{index}"] = join_field
            bind_vars[f"nearParentField{index}"] = parent_field
            near_filters.append(f"{edge_variable}[@nearJoinField{index}] == {from_variable}[@nearParentField{index}]")

        far_filters: list[str] = []
        for index, (join_field, parent_field) in enumerate(zip(far_join, far_parent, strict=True)):
            bind_vars[f"farJoinField{index}"] = join_field
            bind_vars[f"farParentField{index}"] = parent_field
            far_filters.append(f"{to_variable}[@farParentField{index}] == {edge_variable}[@farJoinField{index}]")

        query = "\n".join(
            [
                f"FOR {edge_variable} IN @@joinCollection",
                f"  FILTER {' AND '.join(near_filters)}",
                f"  FOR {to_variable} IN @@collection",
                f"    FILTER {' AND '.join(far_filters)}",
            ]
        )
        return {"edge_variable": edge_variable, "bind_vars": bind_vars, "query": query}
