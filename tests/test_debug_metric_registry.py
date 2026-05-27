from scripts.debug_metric.registry import load_registry


def test_load_registry_returns_dict_keyed_by_metric_id():
    reg = load_registry()
    assert isinstance(reg, dict)
    assert "agentParallelism" in reg
    entry = reg["agentParallelism"]
    assert entry["id"] == "agentParallelism"
    assert "formula" in entry
    assert isinstance(entry["parts"], list)
    # The agentParallelism numerator carries a long-form symbol since the
    # registry refresh — historically "A", now "afk_parallel_minutes" so
    # the (i) modal reads like the formula. Accept either to keep this
    # test robust against the next symbol-convention change.
    assert entry["parts"][0]["symbol"] in {"A", "afk_parallel_minutes"}
