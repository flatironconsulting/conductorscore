import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pair.py"


def test_pair_410_message_points_to_install_and_mentions_wrong_server(tmp_path, httpserver):
    httpserver.expect_request(
        "/api/pair/exchange", method="POST"
    ).respond_with_json({"error": "code_expired_or_not_found"}, status=410)

    env = {
        **os.environ,
        "CONDUCTORSCORE_API_BASE": httpserver.url_for(""),
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "cs_pair_" + "A" * 12],
        capture_output=True,
        text=True,
        env=env,
    )

    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "expired" in out.lower()
    assert "not recognized" in out.lower()
    assert "conductorscore.com/install" in out
    assert "conductorscore.com/pair" not in out
