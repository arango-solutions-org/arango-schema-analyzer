from schema_analyzer.utils import (
    analysis_cache_storage_key,
    extract_first_json_object,
    graph_collections,
    index_edge_definitions_by_collection,
    iter_edge_definitions,
    normalize_analysis_dict,
    split_domain_tokens,
)


def test_extract_first_json_object_simple():
    txt = '{"a": 1}'
    assert extract_first_json_object(txt) == txt


def test_extract_first_json_object_with_preamble():
    txt = 'hello\n\n{ "a": {"b": 2}, "c": 3 }\nbye'
    assert extract_first_json_object(txt).strip().startswith("{")
    assert extract_first_json_object(txt).strip().endswith("}")


def test_extract_first_json_object_handles_strings():
    txt = 'preamble {"a": "x}y", "b": 1} post'
    assert extract_first_json_object(txt) == '{"a": "x}y", "b": 1}'


def test_analysis_cache_storage_key_baseline_unchanged():
    fp = "deadbeef" * 8
    assert analysis_cache_storage_key(fp, llm_cache_segment=None) == fp


def test_analysis_cache_storage_key_llm_segment_changes_key():
    fp = "abc"
    k1 = analysis_cache_storage_key(fp, llm_cache_segment="v1\x00sysA")
    k2 = analysis_cache_storage_key(fp, llm_cache_segment="v1\x00sysB")
    assert k1 != k2
    assert len(k1) == 64


def test_split_domain_tokens_basic():
    assert split_domain_tokens("Customer_Order") == ["customer", "order"]
    assert split_domain_tokens("PRODUCT-CATALOG") == ["product", "catalog"]
    assert split_domain_tokens("simple") == ["simple"]


def test_split_domain_tokens_handles_empty_and_non_string():
    assert split_domain_tokens("") == []
    assert split_domain_tokens(None) == []  # type: ignore[arg-type]
    assert split_domain_tokens("___") == []


def test_iter_edge_definitions_drops_malformed():
    graph = {
        "edge_definitions": [
            {"collection": "knows", "from": ["users"], "to": ["users"]},
            "not-a-dict",
            {"collection": "", "from": [], "to": []},
            {"from": ["x"], "to": ["y"]},
            {"collection": "follows", "from": ["users"], "to": ["users"]},
        ]
    }
    out = iter_edge_definitions(graph)
    assert [ed["collection"] for ed in out] == ["knows", "follows"]


def test_index_edge_definitions_deterministic():
    graphs = [
        {"name": "g_z", "edge_definitions": [{"collection": "shared", "from": [], "to": []}]},
        {"name": "g_a", "edge_definitions": [{"collection": "shared", "from": [], "to": []}]},
        {"name": "g_a", "edge_definitions": [{"collection": "only_a", "from": [], "to": []}]},
    ]
    idx = index_edge_definitions_by_collection(graphs)
    assert set(idx.keys()) == {"shared", "only_a"}


def test_index_edge_definitions_first_graph_wins_and_skips_non_dicts():
    graphs = [
        "not-a-graph",
        {"name": "g_z", "edge_definitions": [{"collection": "shared", "from": ["z"], "to": []}]},
        {"name": "g_a", "edge_definitions": [{"collection": "shared", "from": ["a"], "to": []}]},
    ]
    assert index_edge_definitions_by_collection(graphs)["shared"]["from"] == ["a"]


def test_graph_collections_drops_empty_and_malformed_names():
    graph = {
        "edge_definitions": [
            {"collection": "knows", "from": ["users", ""], "to": "posts"},
            {"collection": "", "from": ["ghost"], "to": []},
        ],
        "orphan_collections": ["badges", "", 7],
    }
    assert graph_collections(graph) == ({"users", "badges"}, {"knows"})


def test_index_edge_definitions_handles_none_and_empty():
    assert index_edge_definitions_by_collection(None) == {}
    assert index_edge_definitions_by_collection([]) == {}


def test_normalize_analysis_dict_hoists_snake_case_keys():
    raw = {
        "conceptual_schema": {"entities": [{"name": "User"}]},
        "physical_mapping": {"entities": {"User": {"collectionName": "users"}}},
        "metadata": {"confidence": 0.9},
    }
    out = normalize_analysis_dict(raw)
    assert "conceptualSchema" in out
    assert "physicalMapping" in out
    assert "conceptual_schema" not in out
    assert "physical_mapping" not in out
    assert out["metadata"] == {"confidence": 0.9}


def test_normalize_analysis_dict_camel_case_passthrough():
    raw = {
        "conceptualSchema": {"entities": []},
        "physicalMapping": {"entities": {}},
        "metadata": {},
    }
    out = normalize_analysis_dict(raw)
    assert out["conceptualSchema"] == {"entities": []}
    assert out["physicalMapping"] == {"entities": {}}


def test_normalize_analysis_dict_camel_wins_over_snake():
    raw = {
        "conceptualSchema": {"entities": [{"name": "Camel"}]},
        "conceptual_schema": {"entities": [{"name": "Snake"}]},
    }
    out = normalize_analysis_dict(raw)
    assert out["conceptualSchema"] == {"entities": [{"name": "Camel"}]}
    assert "conceptual_schema" not in out
