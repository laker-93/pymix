from pymix.utils.beets_query import or_query


def test_or_query_glues_a_bare_comma_between_terms():
    assert or_query("id", [1, 2, 3]) == ["id:1", ",", "id:2", ",", "id:3"]


def test_or_query_single_value_has_no_comma():
    assert or_query("id", [1]) == ["id:1"]


def test_or_query_empty_values_is_empty():
    assert or_query("id", []) == []


def test_or_query_exact_uses_double_colon():
    assert or_query("subbox_id", ["a", "b"], exact=True) == ["subbox_id::a", ",", "subbox_id::b"]
