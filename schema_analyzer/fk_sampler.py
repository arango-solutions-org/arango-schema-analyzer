"""AQL value-containment sampler — the one genuinely ArangoDB-specific piece of FK inference.

``relational_schema_analyzer`` already abstracts the only DB-touching step of its FK engine
behind ``Sampler = Callable[[str, str, str, str], float | None]``, with five implementations
(PostgreSQL, MySQL, SQL Server, Databricks, CSV). This is the sixth, and the reason the rest
of the engine ports unchanged.

Kept in its own module so ``fk_inference`` stays free of any database import and can be
tested without one.
"""

from __future__ import annotations

import logging
from typing import Any

from ._arango import aql_first
from .defaults import FK_MAX_PROBES, FK_PROBE_SAMPLE_SIZE

logger = logging.getLogger(__name__)

# One round trip per candidate: sample distinct non-null referencing values, then count how
# many resolve in the referenced collection. LIMIT 1 inside the subquery makes each lookup an
# existence check rather than a scan, and `_key` lookups ride the primary index.
_PROBE_AQL = """
LET vals = (
  FOR d IN @@localCollection
    FILTER d[@localField] != null
    LIMIT @sampleSize
    RETURN DISTINCT {value}
)
LET hits = LENGTH(
  FOR v IN vals
    FILTER LENGTH(FOR t IN @@foreignCollection FILTER t[@foreignField] == v LIMIT 1 RETURN 1) > 0
    RETURN 1
)
RETURN LENGTH(vals) == 0 ? null : hits / LENGTH(vals)
"""

# Same probe, comparing on a normalized key. Ported from RSA 0.9.2 (issue #12): AQL `==`
# is case- and whitespace-sensitive, so a key written `CUST1` in one collection and
# `cust1` in another scores **zero** — and zero is the hard veto, so the real reference is
# not left unconfirmed, it is deleted. Exactly the failure the `_IDENTITY_FIELDS` cast
# above already guards against for numeric-vs-string `_key`s, in a different disguise.
#
# Kept as a *second* query rather than folded into the first, as in RSA. The normalized
# comparison cannot use an index — `LOWER(TRIM(...))` on the stored field forces a scan
# per sampled value — so it runs only when the strict result leaves the outcome open.
_NORMALIZED_PROBE_AQL = """
LET vals = (
  FOR d IN @@localCollection
    FILTER d[@localField] != null
    LIMIT @sampleSize
    RETURN DISTINCT LOWER(TRIM(TO_STRING({value})))
)
LET hits = LENGTH(
  FOR v IN vals
    FILTER LENGTH(
      FOR t IN @@foreignCollection
        FILTER LOWER(TRIM(TO_STRING(t[@foreignField]))) == v
        LIMIT 1
        RETURN 1
    ) > 0
    RETURN 1
)
RETURN LENGTH(vals) == 0 ? null : hits / LENGTH(vals)
"""

#: A strict score at or above this is conclusive, so the normalized probe is not run.
#: 0.9 is where ``_apply_sampler``'s confidence bump saturates: a higher number could not
#: change the candidate's fate, and the normalized probe costs an unindexed scan plus a
#: unit of probe budget.
_STRICT_IS_CONCLUSIVE = 0.9


class Overlap(float):
    """A containment ratio that remembers whether it needed a normalized comparison.

    A plain ``float`` subclass on purpose: every existing consumer — comparisons,
    arithmetic, ``float()`` — keeps working unchanged, while ``_apply_sampler`` can read
    ``normalized`` to label the evidence. Samplers may still return a bare ``float``; it
    reads as not normalized.
    """

    normalized: bool

    def __new__(cls, value: float, *, normalized: bool = False) -> Overlap:
        self = super().__new__(cls, value)
        self.normalized = normalized
        return self


# ARANGO: `_key` and `_id` are always strings, but a reference imported from a relational
# source is typically numeric. Comparing them raw makes every probe return 0.0, which — with
# `overlap_veto_on_zero` — silently vetoes every genuine candidate (measured against Chinook:
# recall dropped from 0.818 to 0.0). Casting the *sampled* value once keeps `t._key == v` an
# indexed primary lookup rather than forcing a scan with a per-document cast.
_IDENTITY_FIELDS = ("_key", "_id")


class ArangoValueSampler:
    """Measures what fraction of a field's sampled values exist in a target collection.

    Returns ``None`` for "could not evaluate" — an empty collection, a missing collection,
    or a failed query — which makes the engine skip the overlap signal rather than treat it
    as a veto. That distinction matters: a zero ratio is evidence against a candidate, while
    no data is no evidence at all.

    Enforces its own probe budget. On exhaustion it returns ``None`` and records the count in
    :attr:`skipped`, so the caller can surface ``foreignKeyStatus = "degraded"`` instead of
    silently reporting fewer relationships (PRD §3.4 transparency rule).
    """

    def __init__(
        self,
        db: Any,
        *,
        sample_size: int = FK_PROBE_SAMPLE_SIZE,
        max_probes: int = FK_MAX_PROBES,
    ) -> None:
        self._db = db
        self._sample_size = sample_size
        self._max_probes = max_probes
        self.probes = 0
        self.skipped = 0

    @property
    def budget_exhausted(self) -> bool:
        return self.probes >= self._max_probes

    def __call__(
        self,
        local_collection: str,
        local_field: str,
        foreign_collection: str,
        foreign_field: str,
    ) -> float | None:
        if self.budget_exhausted:
            self.skipped += 1
            return None

        bind_vars = {
            "@localCollection": local_collection,
            "@foreignCollection": foreign_collection,
            "localField": local_field,
            "foreignField": foreign_field,
            "sampleSize": self._sample_size,
        }
        value_expr = "TO_STRING(d[@localField])" if foreign_field in _IDENTITY_FIELDS else "d[@localField]"

        what = f"fk containment probe for {local_collection}.{local_field} -> {foreign_collection}.{foreign_field}"

        def probe(aql: str) -> float | None:
            """Run one containment probe, spending one unit of budget."""
            self.probes += 1
            row = aql_first(
                self._db,
                aql.format(value=value_expr),
                bind_vars,
                log=logger,
                what=what,
                level=logging.WARNING,
            )
            if row is None:
                return None
            try:
                return max(0.0, min(1.0, float(row)))
            except (TypeError, ValueError):
                return None

        exact = probe(_PROBE_AQL)
        if exact is None:
            return None
        if exact >= _STRICT_IS_CONCLUSIVE:
            return Overlap(exact, normalized=False)

        # Issue #12 (ported from RSA 0.9.2): values that differ only in case or padding
        # are the same key. Strict comparison scores them zero, which is the hard veto —
        # the reference is deleted rather than left unproven. Costs a second probe, so it
        # runs only here, where the strict answer left the outcome open.
        if self.budget_exhausted:
            self.skipped += 1
            return Overlap(exact, normalized=False)
        norm = probe(_NORMALIZED_PROBE_AQL)
        if norm is not None and norm > exact:
            return Overlap(norm, normalized=True)
        return Overlap(exact, normalized=False)

    def status(self) -> dict[str, Any]:
        """Probe accounting for ``metadata.foreignKeyStatus``."""
        degraded = self.skipped > 0
        return {
            "status": "degraded" if degraded else "ok",
            "probes": self.probes,
            "maxProbes": self._max_probes,
            "unprobedCandidates": self.skipped,
            "reason": (
                f"probe budget of {self._max_probes} exhausted; {self.skipped} candidate(s) "
                "were not measured and are reported on name evidence alone"
                if degraded
                else None
            ),
        }
