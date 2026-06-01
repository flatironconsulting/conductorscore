from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_install_md_refreshes_skill_by_version():
    md = (ROOT / "install.md").read_text()
    assert 'SERVER_VER=$(curl -fsSL "$BASE/VERSION"' in md
    assert 'LOCAL_VER=$(cat "$SKILL_DIR/VERSION"' in md
    assert 'echo "current $LOCAL_VER"' in md
    assert 'echo "updating $LOCAL_VER -> $SERVER_VER"' in md
    assert 'printf \'%s\\n\' "$SERVER_VER" > "$SKILL_DIR/VERSION"' in md


def test_install_md_uses_p_pairing_parameter():
    md = (ROOT / "install.md").read_text()
    assert "`p=` parameter" in md
    assert "cs_pair_" in md


def test_install_md_points_pairing_failures_to_install():
    md = (ROOT / "install.md").read_text()
    assert "was not recognized" in md
    assert "https://conductorscore.com/install" in md
    assert "https://conductorscore.com/pair" not in md
