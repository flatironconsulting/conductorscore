from scripts.plan_signals import is_plan_shaped_path


def test_is_plan_shaped_path_windows_separators():
    assert is_plan_shaped_path(r"C:\repo\plans\foo.md") is True
    assert is_plan_shaped_path(r"C:\repo\docs\design\auth.md") is True
    assert is_plan_shaped_path(r"C:\repo\db-design.md") is True
    assert is_plan_shaped_path(r"C:\repo\node_modules\plans\foo.md") is False
    assert is_plan_shaped_path(r"C:\repo\README.md") is False
    assert is_plan_shaped_path(r"C:\repo\notes.md") is False
