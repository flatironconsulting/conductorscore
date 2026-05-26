from scripts.debug_metric.diff import first_divergence, format_grid


def test_first_divergence_finds_arrow():
    rows = [
        ("2", "local re-extract", 4.0, "extractor.py just now"),
        ("3", "uploads body", 4.0, "most recent upload"),
        ("4", "aggregate row", 0.0, "from profile_aggregates"),
        ("4b", "/api/profile", 0.0, "GET /api/profile/x"),
        ("5", "UI tile", 0.0, "data-tile=..."),
    ]
    div = first_divergence(rows)
    assert div is not None
    assert div[0] == "4"


def test_no_divergence_returns_none():
    rows = [
        ("2", "wire", 4.0, ""),
        ("3", "uploads", 4.0, ""),
        ("4", "aggregate", 4.000001, ""),
    ]
    assert first_divergence(rows) is None


def test_none_values_are_skipped():
    rows = [
        ("2", "wire", 4.0, ""),
        ("3", "uploads", None, "skipped"),
        ("4", "aggregate", 4.0, ""),
    ]
    assert first_divergence(rows) is None


def test_format_grid_shows_arrow_on_divergence():
    rows = [
        ("2", "local re-extract", 4.0, "just now"),
        ("3", "uploads body", 0.0, "17h ago"),
    ]
    out = format_grid(rows)
    assert "FIRST DIVERGENCE" in out
    assert "← FIRST DIVERGENCE" in out


def test_format_grid_shows_all_agree_when_consistent():
    rows = [
        ("2", "wire", 2.0, ""),
        ("3", "uploads", 2.0, ""),
    ]
    out = format_grid(rows)
    assert "All available layers agree" in out
