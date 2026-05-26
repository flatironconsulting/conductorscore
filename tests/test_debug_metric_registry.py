from scripts.debug_metric.registry import load_registry


def test_load_registry_returns_dict_keyed_by_metric_id():
    reg = load_registry()
    assert isinstance(reg, dict)
    assert "agentParallelism" in reg
    entry = reg["agentParallelism"]
    assert entry["id"] == "agentParallelism"
    assert "formula" in entry
    assert isinstance(entry["parts"], list)
    assert entry["parts"][0]["symbol"] == "A"
