"""W3C-community OWL naming for CSI conceptual models (CDF CC-12).

Every CSI-conforming conceptual model follows the OWL ecosystem convention:

* **entity (class) names** — singular ``PascalCase``: ``Account``,
  ``UsageMetric`` — never ``accounts`` or ``usage_metrics``;
* **property names** — ``lowerCamel``: ``accountId``, ``citableUrl`` — never
  ``account_id`` or ``HAS_NAME``;
* **relationship names** — ``lowerCamel`` verb phrases: ``hasPart``.

Only the *conceptual* layer is renamed. The physical mapping keeps raw
collection/field names, and every renamed property records its physical
realization under ``arangoPhysicalMapping.entities.<E>.properties.<name>.field``
so transpilers (arango-sparql-py ``phys:attributeName``, R2RML ``rr:column``)
resolve the conceptual name back to the stored one.

:func:`apply_owl_naming` is the shared producer transform (used by ``to_csi``
here and available to any CSI producer); :func:`naming_issues` is the
validation half wired into ``validate_csi``. Singularization is best-effort
English with a per-call ``overrides`` map — the curation "confirm" step is the
human backstop for the irregular cases.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

__all__ = [
    "lower_camel",
    "pascal_singular",
    "singularize",
    "apply_owl_naming",
    "naming_issues",
]

_ENTITY_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_PROPERTY_RE = re.compile(r"^[a-z][A-Za-z0-9]*$")
# Split camelCase / PascalCase / snake / kebab / space / acronym runs:
# "HTTPServer" → HTTP, Server; "HAS_NAME" → HAS, NAME; "accountId" → account, Id.
_SPLIT_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z0-9]+|[A-Z]+|[a-z0-9]+")


def _words(name: str) -> list[str]:
    return _SPLIT_RE.findall(str(name)) or [str(name)]


#: Same word in singular and plural; :func:`singularize` leaves it alone.
_INVARIANT = frozenset({"data", "metadata", "media", "news", "series", "species", "gps", "sms", "dns", "analytics"})

#: Singular words that already end in ``s`` (``status``, ``analysis``).
_SINGULAR_ENDING_IN_S = frozenset(
    {
        "status",
        "bus",
        "campus",
        "virus",
        "alias",
        "bonus",
        "census",
        "corpus",
        "lens",
        "gas",
        "canvas",
        "atlas",
        "chassis",
        "analysis",
        "diagnosis",
        "crisis",
        "thesis",
        "hypothesis",
        "synopsis",
        "synthesis",
        "parenthesis",
        "emphasis",
    }
)

#: Irregular plural -> singular.
_IRREGULAR = {
    "people": "person",
    "children": "child",
    "men": "man",
    "women": "woman",
    "heroes": "hero",
    "potatoes": "potato",
    "tomatoes": "tomato",
    "echoes": "echo",
    "quizzes": "quiz",
    "criteria": "criterion",
    "phenomena": "phenomenon",
    "matrices": "matrix",
    "oxen": "ox",
    # Data-modelling usage, not the dictionary's: a table of customer demographics
    # holds rows that are each one demographic (Northwind models CustomerDemographic).
    "demographics": "demographic",
    "vertices": "vertex",
}
#: Plural -> singular that does not reverse (``index`` pluralizes to ``indexes``).
_SINGULARIZE_ONLY = {"indices": "index"}

#: ``-ie`` singulars, whose plural ``-ies`` must not become ``-y``.
_IE_SINGULARS = frozenset(
    {
        "movie",
        "cookie",
        "calorie",
        "zombie",
        "rookie",
        "selfie",
        "hoodie",
        "pie",
        "tie",
        "lie",
        "brownie",
        "genie",
        "smoothie",
        "collie",
        "magpie",
        "birdie",
        "prairie",
        "reverie",
        "sortie",
        "lingerie",
    }
)
#: ``-che`` singulars, whose plural ``-ches`` must not lose its ``e``.
_CHE_SINGULARS = frozenset({"cache", "niche", "avalanche", "headache", "moustache", "cliche"})


def _same_case(target: str, like: str) -> str:
    """Spell ``target`` in the case of ``like`` (UPPER, Capitalized or lower)."""
    if like.isupper():
        return target.upper()
    if like[:1].isupper():
        return target[:1].upper() + target[1:]
    return target


def _split_tail(word: str) -> tuple[str, str]:
    """``(head, tail)`` where ``tail`` is the last word of the identifier, so a
    compound (``ORDER_STATUS``, ``usage_metrics``) inflects only its last word."""
    words = _SPLIT_RE.findall(word)
    if words and word.endswith(words[-1]):
        tail = words[-1]
        return word[: len(word) - len(tail)], tail
    return "", word


def _singular_by_rules(word: str) -> str:
    low = word.lower()
    if low in _INVARIANT or low in _SINGULAR_ENDING_IN_S:
        return word
    irregular = _IRREGULAR.get(low) or _SINGULARIZE_ONLY.get(low)
    if irregular is not None:
        return _same_case(irregular, word)
    if low.endswith("ies") and len(word) > 3:
        if low[:-1] in _IE_SINGULARS:
            return word[:-1]
        return word[:-3] + ("Y" if word[-1].isupper() else "y")
    if low.endswith("es") and len(word) > 3:
        stem = low[:-2]
        if stem + "is" in _SINGULAR_ENDING_IN_S:  # analyses -> analysis
            return word[:-2] + ("IS" if word[-1].isupper() else "is")
        if stem in _SINGULAR_ENDING_IN_S or stem.endswith(("ss", "x", "sh", "zz")):
            return word[:-2]
        if stem.endswith("ch") and stem + "e" not in _CHE_SINGULARS:
            return word[:-2]
        # Otherwise the singular ends in e (warehouses, sizes, caches): drop only the s.
    if low.endswith("s") and not low.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


@dataclass(frozen=True)
class NounLexicon:
    """The dictionary layer, vendored from relational-schema-analyzer (identical bytes).

    ``plural_to_singular`` maps an inflected form to its lemma(s), primary
    reading first; ``nouns`` is every noun the lexicon knows. Generated by RSA's
    ``tools/build_noun_lexicon.py`` from LemmInflect 0.2.3 (MIT; see
    ``data/NOTICE-noun-lexicon.md``). r2g's tests check that its SHA-256 equals RSA's.
    """

    plural_to_singular: dict[str, tuple[str, ...]]
    nouns: frozenset[str]
    sha256: str


@lru_cache(maxsize=1)
def noun_lexicon() -> NounLexicon:
    """Load the vendored noun lexicon once."""
    raw = resources.files("schema_analyzer.csi.data").joinpath("noun_lexicon.json.gz").read_bytes()
    table = json.loads(gzip.decompress(raw))
    p2s = {k: tuple(v) for k, v in table["plural_to_singular"].items()}
    nouns = (
        frozenset(table["singular_to_plural"])
        | frozenset(x for v in p2s.values() for x in v)
        | frozenset(table["lemmas_only"])
    )
    return NounLexicon(p2s, nouns, hashlib.sha256(raw).hexdigest())


def _singular_word(word: str) -> str:
    """Named lists, then the dictionary, then the rules (r2g-arango#30)."""
    low = word.lower()
    if (
        low in _INVARIANT
        or low in _SINGULAR_ENDING_IN_S
        or low in _IRREGULAR
        or low in _SINGULARIZE_ONLY
        or (low.endswith("ies") and low[:-1] in _IE_SINGULARS)
    ):
        return _singular_by_rules(word)
    lexicon = noun_lexicon()
    readings = lexicon.plural_to_singular.get(low)
    if readings:
        return _same_case(readings[0], word)
    if low in lexicon.nouns:  # a noun in its own right: cactus, logistics
        return word
    return _singular_by_rules(word)


def singularize(word: str) -> str:
    """Best-effort English singular, shared with relational-schema-analyzer and r2g.

    ``ies`` -> ``y`` (but ``movies`` -> ``movie``); ``-es`` is dropped only
    after a sibilant (``addresses``, ``boxes``, ``batches``) or a singular that
    already ends in ``s`` (``statuses`` -> ``status``); any other ``-es`` keeps
    its ``e`` (``warehouses`` -> ``warehouse``); a trailing ``s`` (not ``ss``)
    is dropped; ``status``, ``analysis``, ``news``, ``series`` and a short
    irregular table (``people`` -> ``person``) are handled by name. Only the
    last word of a compound is inflected. Case-insensitive; the replacement
    follows the word's case.

    Every copy of this function is tested against the same list,
    :mod:`schema_analyzer.csi.naming_known_answers` (vendored from
    relational-schema-analyzer; arango-solutions-org/r2g-arango#30). Pass
    ``overrides`` to :func:`apply_owl_naming` for anything it still gets wrong;
    the curation step reviews the result (CDF CC-12).
    """
    if not word:
        return word
    head, tail = _split_tail(word)
    return head + _singular_word(tail)


def pascal_singular(name: str, overrides: dict[str, str] | None = None) -> str:
    """``usage_metrics`` → ``UsageMetric``; ``tickets`` → ``Ticket``."""
    if overrides and name in overrides:
        return overrides[name]
    words = _words(name)
    if words:
        words[-1] = singularize(words[-1])
    return "".join(w[:1].upper() + w[1:].lower() for w in words if w) or str(name)


def lower_camel(name: str, overrides: dict[str, str] | None = None) -> str:
    """``account_id`` → ``accountId``; ``HAS_NAME`` → ``hasName``.

    Leading underscores (ArangoDB system fields like ``_uri``) are preserved
    so physical passthrough names stay recognizable.
    """
    if overrides and name in overrides:
        return overrides[name]
    prefix = ""
    core = str(name)
    while core.startswith("_"):
        prefix += "_"
        core = core[1:]
    words = _words(core)
    if not words:
        return str(name)
    head = words[0].lower()
    tail = "".join(w[:1].upper() + w[1:].lower() for w in words[1:])
    return prefix + head + tail


def apply_owl_naming(
    csi: dict[str, Any],
    *,
    entity_overrides: dict[str, str] | None = None,
    property_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a copy of a CSI document with OWL-conforming conceptual names.

    Renames conceptual entities/properties/relationships and the matching
    ``arangoPhysicalMapping`` keys; records each renamed property's physical
    field (``properties.<name>.field``) when not already recorded. Idempotent:
    conforming names pass through unchanged.
    """
    import copy

    doc = copy.deepcopy(csi)
    conceptual = doc.get("conceptualModel") or {}
    physical = doc.get("arangoPhysicalMapping") or {}
    phys_entities = physical.get("entities") or {}

    entity_renames: dict[str, str] = {}
    for ent in conceptual.get("entities") or []:
        if not isinstance(ent, dict) or not isinstance(ent.get("name"), str):
            continue
        old = ent["name"]
        new = pascal_singular(old, entity_overrides)
        entity_renames[old] = new
        ent["name"] = new
        if isinstance(ent.get("labels"), list):
            ent["labels"] = [entity_renames.get(lb, lb) if lb == old else lb for lb in ent["labels"]]

        # Rename conceptual property names, remembering physical fields.
        prop_renames: dict[str, str] = {}
        for prop in ent.get("properties") or []:
            if not isinstance(prop, dict) or not isinstance(prop.get("name"), str):
                continue
            p_old = prop["name"]
            p_new = lower_camel(p_old, property_overrides)
            prop_renames[p_old] = p_new
            prop["name"] = p_new

        # Mirror into the physical mapping: key rename + property map.
        spec = phys_entities.get(old)
        if isinstance(spec, dict):
            prop_map = spec.get("properties")
            if not isinstance(prop_map, dict):
                prop_map = {}
            new_prop_map: dict[str, Any] = {}
            for p_old, p_new in prop_renames.items():
                entry = prop_map.get(p_old) or prop_map.get(p_new) or {}
                if not isinstance(entry, dict):
                    entry = {}
                # A nested value already names its location with ``path``; adding
                # ``field`` too would make the mapping invalid (never both, CSI 1.1).
                if "path" not in entry:
                    entry.setdefault("field", p_old)
                new_prop_map[p_new] = entry
            # Preserve any pre-existing mapped properties we didn't rename.
            for k, v in prop_map.items():
                if k not in prop_renames and k not in new_prop_map:
                    new_prop_map[k] = v
            if new_prop_map:
                spec["properties"] = new_prop_map
            # Mapping ids (issue #42) are keyed like ``properties``; follow the rename.
            prop_ids = spec.get("propertyIds")
            if isinstance(prop_ids, dict):
                spec["propertyIds"] = {prop_renames.get(k, k): v for k, v in prop_ids.items()}
            # So do column references; the column's own name is the physical one and stays.
            columns = spec.get("columns")
            for column in columns if isinstance(columns, list) else []:
                if isinstance(column, dict) and isinstance(column.get("property"), str):
                    column["property"] = prop_renames.get(column["property"], column["property"])

    # Physical-mapping entity keys follow the conceptual rename; the physical
    # collection name stays what it is.
    if isinstance(phys_entities, dict) and entity_renames:
        physical["entities"] = {entity_renames.get(name, name): spec for name, spec in phys_entities.items()}

    # Relationships: lowerCamel the type; keys in the physical map follow.
    rel_renames: dict[str, str] = {}
    for rel in conceptual.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        rtype = rel.get("type")
        if isinstance(rtype, str) and rtype:
            new = lower_camel(rtype, property_overrides)
            rel_renames[rtype] = new
            rel["type"] = new
        for endpoint in ("fromEntity", "toEntity"):
            val = rel.get(endpoint)
            if isinstance(val, str) and val in entity_renames:
                rel[endpoint] = entity_renames[val]
    phys_rels = physical.get("relationships")
    if isinstance(phys_rels, dict) and rel_renames:
        physical["relationships"] = {rel_renames.get(name, name): spec for name, spec in phys_rels.items()}

    return doc


def naming_issues(csi: dict[str, Any]) -> list[str]:
    """CC-12 naming checks for ``validate_csi``.

    Pattern-level only (singularity is a convention reviewed in curation):
    entity names must be PascalCase identifiers, property/relationship names
    lowerCamel identifiers.
    """
    issues: list[str] = []
    conceptual = csi.get("conceptualModel") or {}
    for ent in conceptual.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        name = ent.get("name")
        if isinstance(name, str) and not _ENTITY_RE.match(name):
            issues.append(f"entity {name!r} violates CC-12 naming (expected singular PascalCase, e.g. 'UsageMetric')")
        for prop in ent.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            p = prop.get("name")
            if isinstance(p, str) and not p.startswith("_") and not _PROPERTY_RE.match(p):
                issues.append(f"property {name}.{p!r} violates CC-12 naming (expected lowerCamel, e.g. 'accountId')")
    for rel in conceptual.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        rtype = rel.get("type")
        if isinstance(rtype, str) and not _PROPERTY_RE.match(rtype):
            issues.append(f"relationship {rtype!r} violates CC-12 naming (expected lowerCamel, e.g. 'hasPart')")
    return issues
