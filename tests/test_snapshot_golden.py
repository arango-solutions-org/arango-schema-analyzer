import json
from pathlib import Path

from schema_analyzer.list_values import classify_list
from schema_analyzer.snapshot import snapshot_physical_schema


class FakeCollection:
    def __init__(self, *, col_type: int, count: int, indexes: list[dict], properties: dict):
        self._type = col_type
        self._count = count
        self._indexes = indexes
        self._props = properties

    def properties(self):
        return dict(self._props)

    def count(self):
        return self._count

    def indexes(self):
        return list(self._indexes)


class FakeGraph:
    def __init__(self, name: str, props: dict):
        self._name = name
        self._props = props

    def properties(self):
        return dict(self._props)


class FakeDB:
    def __init__(
        self, *, collections: dict, graphs: list[dict], graph_props: dict[str, dict], samples: dict[str, list[dict]]
    ):
        self._collections = collections
        self._graphs = graphs
        self._graph_props = graph_props
        self._samples = samples

        class AQL:
            def __init__(self, outer):
                self._outer = outer

            def execute(self, query, bind_vars=None):
                bind_vars = bind_vars or {}
                name = bind_vars.get("@c") or bind_vars.get("@ec")
                samples = self._outer._samples.get(name) or []

                if "LIMIT 1 RETURN d" in query:
                    return iter(samples[:1])

                if "COLLECT val = " in query and "WITH COUNT INTO cnt" in query:
                    field = bind_vars.get("field", "")
                    agg: dict[str, dict] = {}
                    for s in samples:
                        val = s.get(field)
                        if isinstance(val, list):  # primary label of a label array (issue #28)
                            val = val[0] if val else None
                        if val is None:
                            continue
                        k = str(val)
                        if k not in agg:
                            agg[k] = {"value": val, "count": 0}
                        agg[k]["count"] += 1
                    items = sorted(agg.values(), key=lambda x: (-x["count"], str(x["value"])))
                    top = bind_vars.get("top", 20)
                    overflow_len = bind_vars.get("overflowLen", 50)
                    return iter(
                        [
                            {
                                "distinct": len(items),
                                "top": items[:top],
                                "overflow": items[top : top + overflow_len],
                            }
                        ]
                    )

                if "LET a = ATTRIBUTES(d)" in query:
                    # One row per sampled document: its attribute names, and which
                    # of them hold a list (of scalars only, or not).
                    field = bind_vars.get("field")
                    val = bind_vars.get("val")
                    result = []
                    for s in samples:
                        if field and val is not None and s.get(field) != val:
                            continue
                        lists = [
                            {"name": k, "scalars": classify_list(v) == "scalars"}
                            for k, v in s.items()
                            if isinstance(v, list)
                        ]
                        result.append({"fields": list(s.keys()), "lists": lists})
                    return iter(result[: bind_vars.get("lim", 10)])

                if "PARSE_IDENTIFIER" in query:
                    seen: set[tuple[str, str]] = set()
                    result = []
                    for s in samples:
                        fr = str(s.get("_from", ""))
                        to = str(s.get("_to", ""))
                        if "/" in fr and "/" in to:
                            pair = (fr.split("/")[0], to.split("/")[0])
                            if pair not in seen:
                                seen.add(pair)
                                result.append({"fromCollection": pair[0], "toCollection": pair[1]})
                    return iter(result)

                if "DOCUMENT(e._from)" in query:
                    return iter([])

                limit = bind_vars.get("limit", 0)
                return iter(samples[:limit])

        self.aql = AQL(self)

    def collections(self):
        return dict(self._collections)

    def graphs(self):
        return list(self._graphs)

    def graph(self, name: str):
        return FakeGraph(name, self._graph_props[name])


def _load_fixture(name: str) -> dict:
    p = Path(__file__).parent / "fixtures" / name
    return json.loads(p.read_text("utf-8"))


def test_snapshot_matches_graphrag_fixture():
    fx = _load_fixture("graphrag_snapshot.json")

    collections = {
        # Intentionally out-of-order insertion to validate deterministic sorting.
        "mentions": FakeCollection(
            col_type=3, count=3_000_000, indexes=fx["collections"][3]["indexes"], properties={"type": 3}
        ),
        "entities": FakeCollection(
            col_type=2, count=500_000, indexes=fx["collections"][2]["indexes"], properties={"type": 2}
        ),
        "documents": FakeCollection(
            col_type=2, count=10_000, indexes=fx["collections"][1]["indexes"], properties={"type": 2}
        ),
        "chunks": FakeCollection(
            col_type=2, count=120_000, indexes=fx["collections"][0]["indexes"], properties={"type": 2}
        ),
        "_system": FakeCollection(col_type=2, count=0, indexes=[], properties={"type": 2}),
    }

    graph_props = {
        "graphrag": {
            "name": "graphrag",
            "edgeDefinitions": [{"collection": "mentions", "from": ["chunks"], "to": ["entities"]}],
            "orphanCollections": ["documents"],
        }
    }

    samples = {
        "entities": [{"type": "Person", "labels": ["Entity"], "kind": "person"}],
        "chunks": [{"type": "chunk"}],
        "mentions": [{"relation": "mentions", "relType": "MENTIONS"}],
    }

    db = FakeDB(collections=collections, graphs=fx["graphs"], graph_props=graph_props, samples=samples)
    snap = snapshot_physical_schema(db, sample_limit_per_collection=1, include_samples_in_snapshot=False)
    assert snap == fx


def test_snapshot_matches_high_cardinality_fixture():
    fx = _load_fixture("high_cardinality_snapshot.json")

    collections = {
        "relationships": FakeCollection(
            col_type=3, count=250_000_000, indexes=fx["collections"][1]["indexes"], properties={"type": 3}
        ),
        "entities": FakeCollection(
            col_type=2, count=20_000_000, indexes=fx["collections"][0]["indexes"], properties={"type": 2}
        ),
    }

    graph_props = {
        "hybrid": {
            "name": "hybrid",
            "edgeDefinitions": [{"collection": "relationships", "from": ["entities"], "to": ["entities"]}],
            "orphanCollections": [],
        }
    }

    samples = {
        "entities": [{"type": "Account", "entityType": "account", "kind": "account"}],
        "relationships": [{"relation": "owns", "relType": "OWNS", "type": "edge"}],
    }

    db = FakeDB(collections=collections, graphs=fx["graphs"], graph_props=graph_props, samples=samples)
    snap = snapshot_physical_schema(db, sample_limit_per_collection=1, include_samples_in_snapshot=False)
    assert snap == fx
