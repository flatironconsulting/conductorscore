import io

from scripts.progress import ProgressBar


def test_emits_first_and_final_line_for_small_input(monkeypatch):
    buf = io.StringIO()
    bar = ProgressBar(total=2, out=buf, min_pct_delta=0.05, min_interval_s=10.0)
    bar.update(1)
    bar.update(2)
    bar.done()
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) >= 1
    assert "2/2" in lines[-1]
    assert "100%" in lines[-1]


def test_throttles_dense_updates(monkeypatch):
    buf = io.StringIO()
    # 100 items, only emit every 5%
    bar = ProgressBar(total=100, out=buf, min_pct_delta=0.05, min_interval_s=1000.0)
    for i in range(1, 101):
        bar.update(i)
    bar.done()
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    # ~20 emissions (every 5%) + final; not 100
    assert 15 <= len(lines) <= 25


def test_zero_total_is_safe():
    buf = io.StringIO()
    bar = ProgressBar(total=0, out=buf)
    bar.done()
    # No exception; final line may print "0/0 100%" or be skipped
    assert buf.getvalue() == "" or "0/0" in buf.getvalue()
