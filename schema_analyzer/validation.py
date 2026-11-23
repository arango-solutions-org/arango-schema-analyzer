from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

#: Where a property is stored: ``field`` (one literal attribute name, dots included) or
#: ``path`` (one name per level, for a value inside a nested object; issue #33).
_PROPERTY_MAPPING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "anyOf": [{"required": ["field"]}, {"required": ["path"]}],
    "not": {"required": ["field", "path"]},
    "properties": {
        "field": {"type": "string"},
        "path": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "indexed": {"type": "boolean"},
        "unique": {"type": "boolean"},
    },
}

#: An entity's or relationship's physical indexes.
_INDEXES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["type", "fields"],
        "properties": {
            "type": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "string"}},
            "unique": {"type": "boolean"},
            "sparse": {"type": "boolean"},
            "name": {"type": "string"},
            "vci": {"type": "boolean"},
            "deduplicate": {"type": "boolean"},
            "storedValues": {"type": "array", "items": {"type": "string"}},
        },
    },
}

ANALYSIS_OUTPUT_SCHEMA: dict[str, Any] = {
    "$id": "https://arangodb.com/schema-analyzer/analysis-output.schema.json",
    "type": "object",
    "additionalProperties": False,
    "required": ["conceptualSchema", "physicalMapping", "metadata"],
    "properties": {
        "conceptualSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entities", "relationships", "properties"],
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "labels": {"type": "array", "items": {"type": "string"}},
                            "properties": {"type": "array", "items": {"type": "object"}},
                        },
                    },
                },
                "relationships": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["type", "fromEntity", "toEntity"],
                        "properties": {
                            "type": {"type": "string", "minLength": 1},
                            "fromEntity": {"type": "string", "minLength": 1},
                            "toEntity": {"type": "string", "minLength": 1},
                            "properties": {"type": "array", "items": {"type": "object"}},
                        },
                    },
                },
                "properties": {"type": "array", "items": {"type": "object"}},
            },
        },
        "physicalMapping": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entities", "relationships"],
            "properties": {
                "entities": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "required": ["style"],
                        "properties": {
                            "style": {"type": "string", "enum": ["COLLECTION", "LABEL", "LABELS"]},
                            "collectionName": {"type": "string"},
                            "typeField": {"type": "string"},
                            "typeValue": {"type": "string"},
                            "labelsField": {"type": "string"},
                            "labelValue": {"type": "string"},
                            "aliases": {"type": "array", "items": {"type": "string"}},
                            "indexes": _INDEXES_SCHEMA,
                            "properties": {
                                "type": "object",
                                "additionalProperties": _PROPERTY_MAPPING_SCHEMA,
                            },
                        },
                    },
                },
                "relationships": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "required": ["style"],
                        "not": {"required": ["collectionName"]},
                        "properties": {
                            "style": {"type": "string", "enum": ["DEDICATED_COLLECTION", "GENERIC_WITH_TYPE"]},
                            "edgeCollectionName": {"type": "string"},
                            "typeField": {"type": "string"},
                            "typeValue": {"type": "string"},
                            "indexes": _INDEXES_SCHEMA,
                            "properties": {
                                "type": "object",
                                "additionalProperties": _PROPERTY_MAPPING_SCHEMA,
                            },
                        },
                    },
                },
            },
        },
        "metadata": {
            "type": "object",
            "additionalProperties": True,
            "required": ["confidence", "timestamp", "analyzedCollectionCounts", "detectedPatterns"],
            "properties": {
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "timestamp": {"type": "string"},
                "analyzedCollectionCounts": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["documentCollections", "edgeCollections"],
                    "properties": {
                        "documentCollections": {"type": "number", "minimum": 0},
                        "edgeCollections": {"type": "number", "minimum": 0},
                    },
                },
                "detectedPatterns": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


_validator = Draft202012Validator(ANALYSIS_OUTPUT_SCHEMA)


def validate_analysis_output(data: dict[str, Any]) -> list[str]:
    errors = []
    for err in sorted(_validator.iter_errors(data), key=str):
        errors.append(err.message)
    return errors
