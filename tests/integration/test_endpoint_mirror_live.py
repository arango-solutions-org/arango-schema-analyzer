"""An edge field that repeats its endpoint collections, against a real ArangoDB.

The unit test (``test_endpoint_mirror_fields_leave_edge_collections_dedicated``) hands
the baseline a snapshot with ``edge_endpoints`` already filled in. A real snapshot
picks each edge's type field itself: before 0.21 it picked before it had read the
endpoint collections, so ``destination_sub_type`` (the IAM demo's copy of the target
collection name) became the snapshot's relation field while the baseline rejected it.
"""

from __future__ import annotations

import pytest

from schema_analyzer.baseline import infer_baseline_from_snapshot
from schema_analyzer.snapshot import snapshot_physical_schema

from ..conftest import env

pytestmark = pytest.mark.integration


def test_snapshot_and_baseline_agree_on_an_endpoint_mirror(fresh_database):
    db = fresh_database(f"{env('ARANGO_DB', 'schema_analyzer_it')}_endpoint_mirror")
    users = db.create_collection("users")
    instances = db.create_collection("instances")
    buckets = db.create_collection("buckets")
    users.insert_many([{"_key": f"u{i}", "name": f"user {i}"} for i in range(10)])
    instances.insert_many([{"_key": f"i{i}", "size": "large"} for i in range(10)])
    buckets.insert_many([{"_key": f"b{i}", "region": "eu"} for i in range(10)])
    can_access = db.create_collection("CAN_ACCESS", edge=True)
    can_access.insert_many(
        [
            {"_from": f"users/u{i}", "_to": f"{target}/{target[0]}{i}", "destination_sub_type": target}
            for i in range(10)
            for target in ("instances", "buckets")
        ]
    )

    snapshot = snapshot_physical_schema(db)
    edge = next(c for c in snapshot["collections"] if c["name"] == "CAN_ACCESS")

    # No relation field: the endpoints are reported by collection, not split by
    # destination_sub_type's values.
    assert edge["edge_endpoints"] == {"from_collections": ["users"], "to_collections": ["buckets", "instances"]}
    # destination_sub_type is an ordinary edge field, not a type field taken out of it.
    assert "destination_sub_type" in edge["observed_fields"]["fields"]

    out = infer_baseline_from_snapshot(snapshot)
    pm = out["physicalMapping"]["relationships"]
    assert list(pm) == ["CAN_ACCESS"]
    assert pm["CAN_ACCESS"]["style"] == "DEDICATED_COLLECTION"
