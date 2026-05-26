import io

from scripts.progress import ProgressBar


def test_non_tty_emits_only_50_and_100(monkeypatch):
    """In captured-output environments (Claude Code Bash tool, CI), the bar
    must stay tidy: at most one mid-scan milestone (50%) plus the final
    100% line, regardless of how many updates land."""
    buf = io.StringIO()
    bar = ProgressBar(total=100, out=buf, force_tty=False)
    for i in range(1, 101):
        bar.update(i)
    bar.done()
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert "50/100  50%" in lines[0]
    assert "100/100 100%" in lines[1]


def test_non_tty_caps_emissions_regardless_of_total():
    """No matter how many updates fire, non-TTY mode never emits more than
    two bar lines (50% milestone + final 100%)."""
    for total in (2, 10, 100, 10_000):
        buf = io.StringIO()
        bar = ProgressBar(total=total, out=buf, force_tty=False)
        for i in range(1, total + 1):
            bar.update(i)
        bar.done()
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) <= 2, f"total={total}: {lines}"
        assert "100%" in lines[-1]


def test_tty_rewrites_one_line_with_carriage_return():
    """In a real terminal the bar stays on one line: each emission starts
    with \\r and the line is finalized with a single \\n on done()."""
    buf = io.StringIO()
    bar = ProgressBar(
        total=100, out=buf, force_tty=True, min_pct_delta=0.05, min_interval_s=1000.0
    )
    for i in range(1, 101):
        bar.update(i)
    bar.done()
    raw = buf.getvalue()
    # All in-progress updates use \r, exactly one trailing \n (from done()).
    assert raw.count("\n") == 1
    assert raw.endswith("\n")
    assert raw.count("\r") >= 15  # throttled to ~5% steps + final
    assert "100/100 100%" in raw


def test_zero_total_is_safe():
    buf = io.StringIO()
    bar = ProgressBar(total=0, out=buf, force_tty=False)
    bar.done()
    assert buf.getvalue() == ""
