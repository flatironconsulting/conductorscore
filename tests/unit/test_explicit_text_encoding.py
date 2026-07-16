"""Repository guard: production text I/O must name its encoding explicitly."""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _literal_string(node: ast.expr | None, default: str) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return default


def _has_encoding(call: ast.Call) -> bool:
    return any(kw.arg == "encoding" for kw in call.keywords)


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _text_mode(call: ast.Call, index: int, default: str) -> bool:
    mode_node = call.args[index] if len(call.args) > index else _keyword(call, "mode")
    return "b" not in _literal_string(mode_node, default)


def test_production_text_io_specifies_encoding():
    violations: list[str] = []
    for path in sorted(_SCRIPTS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            name = _dotted_name(node.func)
            if not name:
                continue
            leaf = name.rsplit(".", 1)[-1]
            needs_encoding = False

            if leaf in {"read_text", "write_text"}:
                needs_encoding = True
            elif name in {"open", "io.open"}:
                needs_encoding = _text_mode(node, 1, "r")
            elif leaf == "open" and name not in {"_OPENER.open", "webbrowser.open"}:
                # Path.open() takes mode as its first positional argument.
                needs_encoding = _text_mode(node, 0, "r")
            elif name == "tempfile.NamedTemporaryFile":
                needs_encoding = _text_mode(node, 0, "w+b")
            elif name == "io.TextIOWrapper":
                needs_encoding = True
            elif name.startswith("subprocess."):
                needs_encoding = _is_true(_keyword(node, "text")) or _is_true(
                    _keyword(node, "universal_newlines")
                )

            if needs_encoding and not _has_encoding(node):
                relative = path.relative_to(_ROOT)
                violations.append(f"{relative}:{node.lineno}: {name}()")

    assert not violations, (
        "Text I/O must pass encoding= explicitly (binary I/O is exempt):\n"
        + "\n".join(violations)
    )
