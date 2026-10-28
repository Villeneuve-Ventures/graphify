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


_QUALIFIER_CODE = ("def render():\n    return 1\n\n"
                   "class Painter:\n    def paint(self):\n        pass\n")
_QUALIFIER_DOC = ("# Checkout\n`CheckoutName.render()`\n"
                  "# Host\n`HostAncestor.render()`\n"
                  "# Owner\n`Painter.paint()`\n"
                  "# Package\n`src.renderer.render()`\n"
                  "# Module\n`renderer.render()`\n"
                  "# Bare\n`render()`\n"
                  "# Pinned\n`src/renderer.py::render`\n")
_VALID_QUALIFIERS = {
    "Painter.paint()", "src.renderer.render()", "renderer.render()",
    "render()", "src/renderer.py::render",
}


@pytest.mark.parametrize("checkout", ["CheckoutName", "Other"])
@pytest.mark.parametrize("mode", ["public", "scoped", "cached"])
@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_dotted_qualifiers_ignore_checkout_ancestors(tmp_path, monkeypatch,
                                                   checkout, mode, relative):
    root = tmp_path / "HostAncestor" / checkout
    files = {"src/renderer.py": _QUALIFIER_CODE, "guide.md": _QUALIFIER_DOC}
    _write_files(root, files)
    before = _snapshot(root)
    monkeypatch.chdir(root)
    paths = [Path(name) if relative else root / name for name in files]
    kwargs = dict(source_root=root, parallel=False, quiet=True, ambient_output=False)
    if mode == "scoped":
        with SourceIO(root) as inputs:
            result = extract(paths, source_io=inputs, **kwargs)
    elif mode == "cached":
        from graphify.cache import load_cached

        cache_root = tmp_path / "cache"
        kwargs.update(ambient_output=True, cache_root=cache_root)
        extract(paths, **kwargs)
        result = extract(paths, **kwargs)
        for path in paths:
            payload = load_cached(path, root, cache_root=cache_root)
            assert payload is not None
            assert not any(key.startswith("_markdown_source_") for key in payload)
    else:
        result = extract(paths, **kwargs)
    assert {edge["context"] for edge in _mentions(result)} == _VALID_QUALIFIERS
    assert _snapshot(root) == before
    _assert_private_markers_stripped(result)


@pytest.mark.parametrize("checkout", ["CheckoutName", "Other"])
def test_watch_qualifiers_use_corpus_relative_context(tmp_path, checkout):
    from graphify.watch import _refresh_markdown_mentions

    root = tmp_path / "HostAncestor" / checkout
    files = {"src/renderer.py": _QUALIFIER_CODE, "guide.md": _QUALIFIER_DOC}
    _write_files(root, files)
    paths = [root / name for name in files]
    result = extract(paths, source_root=root, parallel=False,
                     ambient_output=False, quiet=True)
    refreshed = _refresh_markdown_mentions(result, paths, [root / "guide.md"], root=root)
    assert {edge["context"] for edge in _mentions(refreshed)} == _VALID_QUALIFIERS
    _assert_private_markers_stripped(refreshed)


def test_absolute_foreign_source_is_not_dotted_qualifier_evidence():
    nodes = [
        {"id": "guide", "label": "Guide", "file_type": "document",
         "source_file": "guide.md"},
        {"id": "foreign", "label": "render()", "file_type": "code",
         "source_file": "/HostAncestor/foreign.py", "_callable": True},
    ]
    per_file = [{"raw_calls": [{
        "caller_nid": "guide", "callee": "render", "language": "markdown",
        "qualifiers": ["HostAncestor"], "context": "HostAncestor.render()",
        "source_file": "guide.md",
    }]}]
    edges = []
    resolve_markdown_mentions(per_file, nodes, edges)
    assert edges == []


def test_selected_symlink_name_remains_module_evidence(tmp_path):
    source = tmp_path / "renderer.py"
    source.write_text("def render():\n    pass\n")
    alias = tmp_path / "alias.py"
    try:
        alias.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    doc = tmp_path / "guide.md"
    doc.write_text("# Guide\n`alias.render()`\n")
    result = extract([alias, doc], source_root=tmp_path, parallel=False,
                     ambient_output=False, quiet=True)
    assert {edge["context"] for edge in _mentions(result)} == {"alias.render()"}


@pytest.mark.parametrize("file_alias", [False, True])
@pytest.mark.parametrize("physical_inputs", [False, True])
def test_symlinked_checkout_keeps_corpus_qualifiers(tmp_path, file_alias, physical_inputs):
    physical = tmp_path / "PhysicalRoot"
    _write_files(physical, {"src/renderer.py": "def render():\n    pass\n"})
    logical = tmp_path / "CheckoutName"
    try:
        logical.symlink_to(physical, target_is_directory=True)
        if file_alias:
            (physical / "src/alias.py").symlink_to(physical / "src/renderer.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    module = "alias" if file_alias else "renderer"
    doc = physical / "guide.md"
    doc.write_text(f"# Module\n`src.{module}.render()`\n"
                   "# Physical\n`PhysicalRoot.render()`\n"
                   "# Logical\n`CheckoutName.render()`\n")
    input_root = physical if physical_inputs else logical
    paths = [input_root / f"src/{module}.py", input_root / "guide.md"]
    for _ in range(2):
        result = extract(paths, source_root=logical, cache_root=tmp_path / "cache",
                         parallel=False, quiet=True)
        assert {edge["context"] for edge in _mentions(result)} == {f"src.{module}.render()"}
        _assert_private_markers_stripped(result)


@pytest.mark.parametrize("file_alias", [False, True])
def test_watch_symlinked_checkout_keeps_corpus_qualifiers(tmp_path, file_alias):
    from graphify.watch import _refresh_markdown_mentions

    physical = tmp_path / "PhysicalRoot"
    _write_files(physical, {
        "src/renderer.py": "def render():\n    pass\n",
    })
    logical = tmp_path / "CheckoutName"
    try:
        logical.symlink_to(physical, target_is_directory=True)
        if file_alias:
            (physical / "src/alias.py").symlink_to(physical / "src/renderer.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    module = "alias" if file_alias else "renderer"
    (physical / "guide.md").write_text(
        f"# Module\n`src.{module}.render()`\n# Host\n`CheckoutName.render()`\n"
    )
    paths = [logical / f"src/{module}.py", logical / "guide.md"]
    result = extract(paths, source_root=physical, parallel=False,
                     ambient_output=False, quiet=True)
    refreshed = _refresh_markdown_mentions(result, paths, [paths[-1]], root=physical)
    assert {edge["context"] for edge in _mentions(refreshed)} == {f"src.{module}.render()"}


@pytest.mark.parametrize("body, owners", [
    ("Use `Widget`.\n", {"guide.md"}),
    ("Use `Widget`.\n\n# Guide\nUse `Widget`.\n", {"guide.md", "Guide"}),
    ("# Guide\nUse `Widget`.\n", {"Guide"}),
])
@pytest.mark.parametrize("mode", ["public", "cached", "scoped", "watch"])
def test_page_mentions_follow_canonical_file_ids(tmp_path, body, owners, mode):
    code = tmp_path / "widget.py"
    code.write_text("class Widget:\n    pass\n")
    doc = tmp_path / "guide.md"
    doc.write_text(body)
    paths = [code, doc]
    if mode == "scoped":
        with SourceIO(tmp_path) as inputs:
            result = extract(paths, source_root=tmp_path, source_io=inputs,
                             quiet=True, ambient_output=False)
    elif mode == "cached":
        extract(paths, source_root=tmp_path, cache_root=tmp_path / "cache",
                parallel=False, quiet=True)
        result = extract(paths, source_root=tmp_path, cache_root=tmp_path / "cache",
                         parallel=False, quiet=True)
    else:
        result = extract(paths, source_root=tmp_path, parallel=False,
                         quiet=True, ambient_output=False)
        if mode == "watch":
            from graphify.watch import _refresh_markdown_mentions

            result = _refresh_markdown_mentions(result, paths, [doc], root=tmp_path)
    nodes = {node["id"]: node for node in result["nodes"]}
    assert {nodes[edge["source"]]["label"] for edge in _mentions(result)} == owners
    assert len(_mentions(result)) == len(owners)
    assert all(edge["target"] in nodes for edge in _mentions(result))
    _assert_private_markers_stripped(result)


@pytest.mark.parametrize("mode", ["public", "cached", "watch"])
@pytest.mark.parametrize("relative", [False, True])
def test_symlink_document_cites_from_selected_location(tmp_path, monkeypatch, mode, relative):
    root = tmp_path / "repo"
    external = tmp_path / "external"
    external.mkdir()
    _write_files(root, {"src/widget.py": "class Widget:\n    pass\n"})
    (root / "docs").mkdir()
    body = ("# Relative\n`../src/widget.py::Widget`\n"
            "# Selected code alias\n`../src/alias.py::Widget`\n"
            "# Pinned\n`src/widget.py::Widget`\n"
            "# Escaped\n`../../src/widget.py::Widget`\n")
    target = external / "guide.md"
    target.write_text(body)
    doc = root / "docs/guide.md"
    alias = root / "src/alias.py"
    try:
        doc.symlink_to(target)
        alias.symlink_to(root / "src/widget.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    monkeypatch.chdir(root)
    paths = [Path("src/alias.py"), Path("docs/guide.md")]
    if not relative:
        paths = [root / path for path in paths]
    if mode == "cached":
        extract(paths, source_root=root, cache_root=tmp_path / "cache",
                parallel=False, quiet=True)
        result = extract(paths, source_root=root, cache_root=tmp_path / "cache",
                         parallel=False, quiet=True)
    else:
        result = extract(paths, source_root=root, parallel=False,
                         quiet=True, ambient_output=False)
    if mode == "watch":
        from graphify.watch import _refresh_markdown_mentions

        result = _refresh_markdown_mentions(result, paths, [doc], root=root)
    assert {edge["context"] for edge in _mentions(result)} == {
        "../src/widget.py::Widget", "../src/alias.py::Widget", "src/widget.py::Widget",
    }
    _assert_private_markers_stripped(result)


@pytest.mark.parametrize("declaration", [
    "namespace Acme.Tools { public class Widget { public void Run() {} } }",
    "namespace Acme.Tools; public class Widget { public void Run() {} }",
    "namespace Acme { namespace Tools { public class Widget { public void Run() {} } } }",
])
@pytest.mark.parametrize("mode", ["public", "cached", "scoped", "watch"])
def test_declared_namespace_qualifies_marked_targets(tmp_path, declaration, mode):
    files = {
        "src/Widget.cs": declaration,
        "other/Widget.cs": "namespace Other.Tools { public class Widget {} }",
        "guide.md": ("# Class\n`Acme.Tools.Widget`\n"
                     "# Method\n`Acme.Tools.Widget.Run()`\n"
                     "# Wrong\n`Wrong.Tools.Widget`\n"
                     "# Ambiguous\n`Widget`\n"),
    }
    _write_files(tmp_path, files)
    paths = [tmp_path / name for name in files]
    if mode == "scoped":
        before = _snapshot(tmp_path)
        with SourceIO(tmp_path) as inputs:
            result = extract(paths, source_root=tmp_path, source_io=inputs,
                             quiet=True, ambient_output=False)
        assert _snapshot(tmp_path) == before
    elif mode == "cached":
        extract(paths, source_root=tmp_path, cache_root=tmp_path / "cache",
                parallel=False, quiet=True)
        result = extract(paths, source_root=tmp_path, cache_root=tmp_path / "cache",
                         parallel=False, quiet=True)
    else:
        result = extract(paths, source_root=tmp_path, parallel=False,
                         quiet=True, ambient_output=False)
    if mode == "watch":
        from graphify.watch import _refresh_markdown_mentions

        result = _refresh_markdown_mentions(result, paths, [tmp_path / "guide.md"], root=tmp_path)
    assert {edge["context"] for edge in _mentions(result)} == {
        "Acme.Tools.Widget", "Acme.Tools.Widget.Run()",
    }
    _assert_private_markers_stripped(result)


@pytest.mark.parametrize("mode", ["public", "cached", "watch"])
def test_page_mentions_follow_salted_file_ids(tmp_path, mode):
    from graphify.ids import make_id

    assert make_id("a.b_guide") == make_id("a_b_guide")
    files = {"widget.py": "class Widget:\n    pass\n",
             "a.b/guide.md": "Use `Widget`.\n", "a_b/guide.md": "Use `Widget`.\n"}
    _write_files(tmp_path, files)
    paths = [tmp_path / name for name in files]
    if mode == "cached":
        extract(paths, source_root=tmp_path, cache_root=tmp_path / "cache",
                parallel=False, quiet=True)
        result = extract(paths, source_root=tmp_path, cache_root=tmp_path / "cache",
                         parallel=False, quiet=True)
    else:
        result = extract(paths, source_root=tmp_path, parallel=False,
                         quiet=True, ambient_output=False)
    if mode == "watch":
        from graphify.watch import _refresh_markdown_mentions

        result = _refresh_markdown_mentions(result, paths, paths[1:], root=tmp_path)
    nodes = {node["id"]: node for node in result["nodes"]}
    assert len(nodes) == len(result["nodes"])
    assert len(_mentions(result)) == 2
    assert {nodes[edge["source"]]["source_file"] for edge in _mentions(result)} == {
        "a.b/guide.md", "a_b/guide.md",
    }


@pytest.mark.parametrize("namespace", [None, [], {"name": "Acme.Tools"}, 7])
def test_namespace_evidence_requires_recorded_string(namespace):
    nodes = [
        {"id": "guide", "label": "Guide", "file_type": "document"},
        {"id": "widget", "label": "Widget", "file_type": "code", "_callable": True,
         "metadata": {"namespace": namespace}},
    ]
    per_file = [{"raw_calls": [{"caller_nid": "guide", "callee": "Widget",
                               "qualifiers": ["Acme", "Tools"], "language": "markdown",
                               "context": "Acme.Tools.Widget"}]}]
    edges = []
    resolve_markdown_mentions(per_file, nodes, edges)
    assert edges == []


@pytest.mark.parametrize("mode", ["public", "cached", "watch"])
def test_shared_document_target_does_not_share_owner_evidence(tmp_path, mode):
    root = tmp_path / "repo"
    shared = tmp_path / "shared.md"
    shared.write_text("# Guide\n`./model.py::Widget` and `redis`.\n")
    _write_files(root, {"a/model.py": "class Widget:\n    def redis(self):\n        pass\n"})
    (root / "b").mkdir()
    docs = [root / "a/guide.md", root / "b/guide.md"]
    try:
        for doc in docs:
            doc.symlink_to(shared)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    paths = [root / "a/model.py", *docs]
    if mode == "cached":
        extract(paths, source_root=root, cache_root=tmp_path / "cache",
                parallel=False, quiet=True)
        result = extract(paths, source_root=root, cache_root=tmp_path / "cache",
                         parallel=False, quiet=True)
    else:
        result = extract(paths, source_root=root, parallel=False,
                         quiet=True, ambient_output=False)
    if mode == "watch":
        from graphify.watch import _refresh_markdown_mentions

        result = _refresh_markdown_mentions(result, paths, docs, root=root)
    nodes = {node["id"]: node for node in result["nodes"]}
    assert {(nodes[edge["source"]]["source_file"], edge["context"])
            for edge in _mentions(result)} == {
        ("a/guide.md", "./model.py::Widget"), ("a/guide.md", "redis"),
    }
