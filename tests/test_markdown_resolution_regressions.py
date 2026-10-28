"""Regressions for qualified built-ins and selected Markdown source identities."""
from pathlib import Path

import pytest

from graphify.extract import extract
from graphify.markdown_resolution import resolve_markdown_mentions
from graphify.source_io import SourceIO


def _write_files(root, files):
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def _mentions(result):
    return [edge for edge in result["edges"] if edge.get("_markdown_mention")]


def _snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


def _assert_private_markers_stripped(result):
    assert all("_callable" not in node and "_callable_class" not in node
               for node in result["nodes"])


@pytest.mark.parametrize("ambiguous", [False, True])
def test_qualified_builtin_method_uses_owner_evidence(tmp_path, ambiguous):
    files = {
        "printer.py": ("class Printer:\n"
                       "    def print(self):\n        pass\n"
                       "    def render(self):\n        pass\n"),
        "guide.md": ("# Qualified\n`Printer.print()`\n"
                     "# Control\n`Printer.render()`\n"
                     "# Bare\n`print()`\n"
                     "# Wrong\n`Other.print()`\n"
                     "# Pinned\n`printer.py::Printer::print`\n"),
    }
    if ambiguous:
        files["other.py"] = "class Printer:\n    def print(self):\n        pass\n"
    _write_files(tmp_path, files)
    result = extract([tmp_path / name for name in files], source_root=tmp_path,
                     parallel=False, ambient_output=False, quiet=True)
    expected = {"Printer.render()", "printer.py::Printer::print"}
    if not ambiguous:
        expected.add("Printer.print()")
    assert {edge["context"] for edge in _mentions(result)} == expected
    assert len(_mentions(result)) == len(expected)
    assert all(edge["confidence"] == ("EXTRACTED" if "::" in edge["context"]
                                     else "INFERRED") for edge in _mentions(result))
    _assert_private_markers_stripped(result)


@pytest.mark.parametrize("scoped", [False, True], ids=["public", "scoped"])
@pytest.mark.parametrize("input_form", ["relative", "absolute", "mixed"])
def test_selected_path_forms_resolve_the_same_mentions(tmp_path, monkeypatch,
                                                     scoped, input_form):
    files = {
        "src/widget.py": "class Widget:\n    pass\n",
        "docs/guide.md": ("# Suffix\n`widget.py::Widget`\n"
                          "# Relative\n`../src/widget.py::Widget`\n"
                          "# Missing\n`./src/widget.py::Widget`\n"
                          "# Escape\n`../../src/widget.py::Widget`\n"),
    }
    _write_files(tmp_path, files)
    before = _snapshot(tmp_path)
    monkeypatch.chdir(tmp_path)
    paths = [Path(name) for name in files]
    if input_form == "absolute":
        paths = [tmp_path / path for path in paths]
    elif input_form == "mixed":
        paths[0] = tmp_path / paths[0]
    if scoped:
        with SourceIO(tmp_path) as inputs:
            result = extract(paths, source_io=inputs, source_root=tmp_path,
                             parallel=False, ambient_output=False, quiet=True)
            assert {item["path"] for item in inputs.evidence
                    if item["operation"] == "read"} >= set(files)
            assert len(result["outcomes"]) == 2
    else:
        result = extract(paths, source_root=tmp_path, parallel=False,
                         ambient_output=False, quiet=True)
    assert {edge["context"] for edge in _mentions(result)} == {
        "widget.py::Widget", "../src/widget.py::Widget",
    }
    assert len(_mentions(result)) == 2
    assert all(edge["confidence"] == "EXTRACTED" for edge in _mentions(result))
    assert _snapshot(tmp_path) == before
    _assert_private_markers_stripped(result)


@pytest.mark.parametrize("empty_status", [{}, {"error": "parse failed"}])
def test_selected_empty_file_preserves_real_suffix_ambiguity(empty_status, monkeypatch):
    # The resolver must use admitted identities, never query the host filesystem.
    monkeypatch.setattr(Path, "resolve", lambda *_a, **_k: pytest.fail("host resolve"))
    nodes = [
        {"id": "guide", "label": "Guide", "file_type": "document",
         "source_file": "docs/guide.md"},
        {"id": "widget", "label": "Widget", "file_type": "code",
         "source_file": "src/widget.py", "_callable": True},
    ]
    per_file = [
        {"_markdown_source_file": "/repo/src/widget.py",
         "_markdown_source_alias": "src/widget.py"},
        {"_markdown_source_file": "/repo/vendor/widget.py",
         "_markdown_source_alias": "vendor/widget.py", **empty_status},
        {"_markdown_source_file": "/repo/docs/guide.md",
         "_markdown_source_alias": "docs/guide.md", "raw_calls": [
             {"caller_nid": "guide", "callee": "Widget", "language": "markdown",
              "path": "widget.py", "source_file": "docs/guide.md"},
             {"caller_nid": "guide", "callee": "Widget", "language": "markdown",
              "path": "../src/widget.py", "source_file": "docs/guide.md",
              "context": "../src/widget.py::Widget"},
         ]},
    ]
    edges = []
    resolve_markdown_mentions(per_file, nodes, edges)
    assert [edge["context"] for edge in _mentions({"edges": edges})] == [
        "../src/widget.py::Widget",
    ]


def test_selected_alias_does_not_reassign_foreign_nodes():
    nodes = [
        {"id": "guide", "label": "Guide", "file_type": "document",
         "source_file": "docs/guide.md"},
        {"id": "foreign", "label": "Foreign", "file_type": "code",
         "source_file": "/foreign/widget.py", "_callable": True},
    ]
    per_file = [{
        "nodes": [nodes[1]], "_markdown_source_file": "/repo/src/widget.py",
        "_markdown_source_alias": "src/widget.py", "raw_calls": [
            {"caller_nid": "guide", "callee": "Foreign", "language": "markdown",
             "path": "src/widget.py", "source_file": "docs/guide.md"},
        ],
    }]
    edges = []
    resolve_markdown_mentions(per_file, nodes, edges)
    assert edges == []
