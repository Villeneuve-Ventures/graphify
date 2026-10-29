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


def _reference_case(root: Path, files: dict[str, str], mode: str) -> dict:
    _write_files(root, files)
    paths = [root / name for name in files]
    if mode == "scoped":
        before = _snapshot(root)
        with SourceIO(root) as inputs:
            result = extract(paths, source_root=root, source_io=inputs,
                             parallel=False, quiet=True, ambient_output=False)
        assert _snapshot(root) == before
    elif mode == "cached":
        extract(paths, source_root=root, cache_root=root / "cache", parallel=False, quiet=True)
        result = extract(paths, source_root=root, cache_root=root / "cache",
                         parallel=False, quiet=True)
    else:
        result = extract(paths, source_root=root, parallel=False,
                         quiet=True, ambient_output=False)
    if mode == "watch":
        from graphify.watch import _refresh_markdown_mentions

        docs = [path for path in paths if path.suffix == ".md"]
        result = _refresh_markdown_mentions(result, paths, docs, root=root)
    _assert_private_markers_stripped(result)
    return result


@pytest.mark.parametrize('mode', ['public', 'cached', 'scoped', 'watch'])
@pytest.mark.parametrize('fence', ['~~~md\n```\n~~~', '````md\n```\n````'])
def test_unbound_heading_does_not_publish_prior_owner_reference(tmp_path, mode, fence):
    result = _reference_case(tmp_path, {
        'model.py': 'class Visible:\n    pass\nclass Hidden:\n    pass\n',
        'guide.md': '# Before\n`Visible`\n\n' + fence
                    + '\n\n## Lost\nUse `Hidden` and `model.py::Hidden`.\n',
    }, mode)
    mentions = _mentions(result)
    assert [edge['context'] for edge in mentions] == ['Visible']
    before = next(node['id'] for node in result['nodes'] if node['label'] == 'Before')
    assert mentions[0]['source'] == before
    assert all(node['label'] != 'Lost' for node in result['nodes'])


def test_native_watch_withholds_unbound_heading_references(tmp_path):
    from tests.test_markdown_code_spans import _native_mentions, _native_rebuild

    corpus = tmp_path / 'corpus'
    _write_files(corpus, {
        'model.py': 'class Visible:\n    pass\nclass Hidden:\n    pass\n',
        'guide.md': '# Before\n`Visible`\n\n## After\n`Hidden`\n',
    })
    initial = _native_rebuild(corpus)
    assert {edge['context'] for edge in _native_mentions(initial)} == {'Visible', 'Hidden'}
    doc = corpus / 'guide.md'
    doc.write_text('# Before\n`Visible`\n\n~~~md\n```\n~~~\n\n## After\n`Hidden`\n')
    refreshed = _native_rebuild(corpus, [doc])
    assert [edge['context'] for edge in _native_mentions(refreshed)] == ['Visible']
    assert all(node['label'] != 'After' for node in refreshed['nodes'])


@pytest.mark.parametrize("mode", ["public", "cached", "scoped", "watch"])
def test_dotted_qualifiers_preserve_path_and_owner_order(tmp_path, mode):
    valid = ["acme.tools.Widget", "tools.Widget", "Widget.render()",
             "acme.tools.Widget.render()",
             "worker.Worker.run()", "pkg.worker.Worker.run()",
             "repeated.repeated.tools.Repeated"]
    invalid = ["tools.acme.Widget", "acme.acme.tools.Widget",
               "Widget.acme.tools.render()", "tools.Widget.acme.render()",
               "repeated.tools.repeated.Repeated", "Wrong.tools.Widget", "pkg.Worker.run()"]
    files = {
        "acme/tools.py": "class Widget:\n    def render(self):\n        pass\n",
        "pkg/worker.py": "class Worker:\n    def run(self):\n        pass\n",
        "repeated/repeated/tools.py": "class Repeated:\n    pass\n",
        "guide.md": "\n\n".join(f"# Case{index}\n`{span}`"
                                   for index, span in enumerate(valid + invalid)),
    }
    result = _reference_case(tmp_path, files, mode)
    assert {edge["context"] for edge in _mentions(result)} == set(valid)


@pytest.mark.parametrize("mode", ["public", "cached", "scoped", "watch"])
def test_dotted_qualifiers_do_not_splice_namespace_and_path(tmp_path, mode):
    valid = ["Acme.Tools.Widget", "Acme.Tools.Widget.Run()", "Widget.Run()"]
    invalid = ["Tools.Acme.Widget", "Acme.Acme.Tools.Widget", "Acme.src.Widget",
               "Widget.Acme.Tools.Run()", "Tools.Widget", "Widget"]
    files = {
        "src/Widget.cs": "namespace Acme.Tools { public class Widget { public void Run() {} } }",
        "other/Widget.cs": "namespace Other.Tools { public class Widget {} }",
        "guide.md": "\n\n".join(f"# Case{index}\n`{span}`"
                                   for index, span in enumerate(valid + invalid)),
    }
    result = _reference_case(tmp_path, files, mode)
    assert {edge["context"] for edge in _mentions(result)} == set(valid)


@pytest.mark.parametrize("mode", ["public", "cached", "scoped", "watch"])
@pytest.mark.parametrize("explicit_first", [False, True])
def test_duplicate_mention_retains_strongest_provenance(tmp_path, mode, explicit_first):
    weak = "Widget.render()"
    strong = "src/widget.py::Widget::render"
    spans = [strong, weak] if explicit_first else [weak, strong]
    files = {
        "src/widget.py": "class Widget:\n    def render(self):\n        pass\n",
        "guide.md": "# Guide\n\n" + "\n\n".join(f"`{span}`" for span in spans) + "\n",
    }
    result = _reference_case(tmp_path, files, mode)
    edges = _mentions(result)
    assert len(edges) == 1
    assert (edges[0]["confidence"], edges[0]["confidence_score"]) == ("EXTRACTED", 1.0)
    assert edges[0]["context"] == strong
    assert edges[0]["source_location"] == ("L3" if explicit_first else "L5")


@pytest.mark.parametrize("origin, marker", [("semantic", None), ("ast", None),
                                             ("semantic", 1), ("ast", 1)])
def test_stronger_mention_only_promotes_owned_references(origin, marker):
    nodes = [
        {"id": "guide", "label": "Guide", "file_type": "document", "source_file": "guide.md"},
        {"id": "widget", "label": "Widget", "file_type": "code", "source_file": "src/widget.py",
         "_callable": True, "_callable_class": True},
        {"id": "render", "label": ".render()", "file_type": "code", "source_file": "src/widget.py",
         "_callable": True},
    ]
    previous = {"source": "guide", "target": "render", "relation": "references",
                "confidence": "INFERRED", "confidence_score": 0.61, "context": "retained",
                "source_file": "guide.md", "source_location": "L1", "_origin": origin}
    if marker is not None:
        previous["_markdown_mention"] = marker
    edges = [{"source": "widget", "target": "render", "relation": "method"}, dict(previous)]
    calls = [{"caller_nid": "guide", "callee": "render", "qualifiers": ["Widget"],
              "path": "src/widget.py", "language": "markdown",
              "context": "src/widget.py::Widget::render", "source_file": "guide.md",
              "source_location": "L7"}]
    resolve_markdown_mentions([{"raw_calls": calls}], nodes, edges)
    assert len(edges) == 2
    if origin == "ast" and marker == 1:
        assert edges[1] == {**previous, "confidence": "EXTRACTED", "confidence_score": 1.0,
                            "context": "src/widget.py::Widget::render", "source_location": "L7"}
    else:
        assert edges[1] == previous


_WATCH_OWNER_DOC = ("# Old\n`A.run()`\n# New\n`B.run()`\n"
                    "# Pinned old\n`model.js::A::run`\n"
                    "# Pinned new\n`model.js::B::run`\n")


def test_watch_owner_move_withholds_unpublished_current_method(tmp_path):
    from graphify.watch import _refresh_markdown_mentions

    files = {"model.js": "class A { run() {} } class B {}\n",
             "guide.md": _WATCH_OWNER_DOC}
    _write_files(tmp_path, files)
    paths = [tmp_path / name for name in files]
    result = extract(paths, source_root=tmp_path, parallel=False,
                     ambient_output=False, quiet=True)
    assert {edge["context"] for edge in _mentions(result)} == {
        "A.run()", "model.js::A::run",
    }
    (tmp_path / "model.js").write_text("class A {} class B { run() {} }\n")
    before = _snapshot(tmp_path)
    refreshed = _refresh_markdown_mentions(result, paths, [tmp_path / "guide.md"], root=tmp_path)
    assert _mentions(refreshed) == []
    assert _snapshot(tmp_path) == before
    _assert_private_markers_stripped(refreshed)
    control = extract(paths, source_root=tmp_path, parallel=False,
                      ambient_output=False, quiet=True)
    assert {edge["context"] for edge in _mentions(control)} == {
        "B.run()", "model.js::B::run",
    }


@pytest.mark.parametrize("origin", ["semantic", "ast"])
@pytest.mark.parametrize("relation", ["contains", "method"])
def test_watch_retained_ownership_is_not_current_ast_evidence(tmp_path, origin, relation):
    import json

    from graphify.watch import _refresh_markdown_mentions

    files = {"model.js": "class A {} class B { run() {} }\n",
             "guide.md": _WATCH_OWNER_DOC}
    _write_files(tmp_path, files)
    paths = [tmp_path / name for name in files]
    result = extract(paths, source_root=tmp_path, parallel=False,
                     ambient_output=False, quiet=True)
    owner = next(node["id"] for node in result["nodes"] if node["label"] == "A")
    method = next(node["id"] for node in result["nodes"] if node["label"] == ".run()")
    retained = {"source": owner, "target": method, "relation": relation,
                "_origin": origin, "confidence": "INFERRED", "confidence_score": 0.61,
                "source_file": "notes.md", "context": "Retained ownership"}
    result["edges"].append(retained)
    retained_bytes = json.dumps(retained, sort_keys=True).encode()
    refreshed = _refresh_markdown_mentions(result, paths, [tmp_path / "guide.md"], root=tmp_path)
    assert retained in refreshed["edges"]
    assert json.dumps(retained, sort_keys=True).encode() == retained_bytes
    contexts = {edge["context"] for edge in _mentions(refreshed)}
    assert not contexts & {"A.run()", "model.js::A::run"}
    if origin == "semantic":
        assert contexts == {"B.run()", "model.js::B::run"}
    # Contradictory retained AST ancestry cannot prove a published declaration.
    else:
        assert contexts == set()


def test_watch_namespace_move_withholds_stale_same_line_endpoint(tmp_path):
    from graphify.watch import _refresh_markdown_mentions

    files = {
        "Widget.cs": "namespace Acme { public class Widget { public void Run() {} } }",
        "guide.md": ("# Old\n`Acme.Widget.Run()`\n# New\n`Other.Widget.Run()`\n"
                     "# Pinned\n`Widget.cs::Widget::Run`\n"),
    }
    _write_files(tmp_path, files)
    paths = [tmp_path / name for name in files]
    result = extract(paths, source_root=tmp_path, parallel=False,
                     ambient_output=False, quiet=True)
    assert len(_mentions(result)) == 2
    (tmp_path / "Widget.cs").write_text(files["Widget.cs"].replace("Acme", "Other"))
    refreshed = _refresh_markdown_mentions(result, paths, [tmp_path / "guide.md"], root=tmp_path)
    assert _mentions(refreshed) == []


def test_watch_endpoint_matching_does_not_require_scratch_ids(tmp_path):
    from graphify.watch import _refresh_markdown_mentions

    files = {"model.js": "class A { run() {} } class B {}\n",
             "guide.md": _WATCH_OWNER_DOC}
    _write_files(tmp_path, files)
    paths = [tmp_path / name for name in files]
    result = extract(paths, source_root=tmp_path, parallel=False,
                     ambient_output=False, quiet=True)
    expected = {(edge["context"], edge["confidence"], edge["confidence_score"])
                for edge in _mentions(result)}
    remap = {node["id"]: node["id"] + "_collision_salt" for node in result["nodes"]}
    for node in result["nodes"]:
        node["id"] = remap[node["id"]]
    for edge in result["edges"]:
        edge["source"], edge["target"] = remap[edge["source"]], remap[edge["target"]]
    refreshed = _refresh_markdown_mentions(result, paths, [tmp_path / "guide.md"], root=tmp_path)
    assert {(edge["context"], edge["confidence"], edge["confidence_score"])
            for edge in _mentions(refreshed)} == expected
    assert all(edge["source"] in remap.values() and edge["target"] in remap.values()
               for edge in _mentions(refreshed))
    _assert_private_markers_stripped(refreshed)


def test_native_watch_owner_move_doc_event_then_code_event(tmp_path):
    from tests.test_markdown_code_spans import _native_mentions, _native_rebuild

    corpus = tmp_path / "corpus"
    _write_files(corpus, {"model.js": "class A { run() {} } class B {}\n",
                          "guide.md": _WATCH_OWNER_DOC})
    initial = _native_rebuild(corpus)
    assert {edge["context"] for edge in _native_mentions(initial)} == {
        "A.run()", "model.js::A::run",
    }
    source, doc = corpus / "model.js", corpus / "guide.md"
    source.write_text("class A {} class B { run() {} }\n")
    doc.write_text(_WATCH_OWNER_DOC + "\nAn ordinary document event.\n")
    doc_only = _native_rebuild(corpus, [doc])
    assert _native_mentions(doc_only) == []
    assert any(node["id"] == "model_a_run" for node in doc_only["nodes"])
    code_followup = _native_rebuild(corpus, [source])
    mentions = _native_mentions(code_followup)
    assert {edge["context"] for edge in mentions} == {"B.run()", "model.js::B::run"}
    assert {edge["target"] for edge in mentions} == {"model_b_run"}
    assert {edge["confidence"] for edge in mentions} == {"INFERRED", "EXTRACTED"}


@pytest.mark.parametrize("mode", ["public", "scoped", "watch"])
def test_empty_selected_extraction_cannot_prove_inferred_uniqueness(tmp_path, monkeypatch, mode):
    import importlib

    from graphify.watch import _refresh_markdown_mentions

    extraction = importlib.import_module("graphify.extract")
    files = {"good.py": "class Widget:\n    pass\n",
             "omitted.py": "class Widget:\n    pass\n",
             "guide.md": "# Bare\n`Widget`\n# Pinned\n`good.py::Widget`\n"}
    _write_files(tmp_path, files)
    paths = [tmp_path / name for name in files]
    baseline = extract(paths, source_root=tmp_path, parallel=False,
                       ambient_output=False, quiet=True)
    real_extract = extraction._safe_extract_with_xaml_root

    def anomalous_empty(extractor, path, root, **kwargs):
        result = real_extract(extractor, path, root, **kwargs)
        if path.name == "omitted.py":
            assert any(node["label"] == "Widget" for node in result["nodes"])
            return {"nodes": [], "edges": [], "raw_calls": []}
        return result

    monkeypatch.setattr(extraction, "_safe_extract_with_xaml_root", anomalous_empty)
    before = _snapshot(tmp_path)
    if mode == "scoped":
        with SourceIO(tmp_path) as inputs:
            result = extract(paths, source_root=tmp_path, source_io=inputs,
                             parallel=False, ambient_output=False, quiet=True)
            assert {item["path"] for item in inputs.evidence
                    if item["operation"] == "read"} >= set(files)
    elif mode == "watch":
        result = _refresh_markdown_mentions(baseline, paths, [tmp_path / "guide.md"], root=tmp_path)
    else:
        result = extract(paths, source_root=tmp_path, parallel=False, strict=True,
                         ambient_output=False, report_outcomes=True, quiet=True)
    assert {edge["context"] for edge in _mentions(result)} == {"good.py::Widget"}
    assert [(edge["confidence"], edge["confidence_score"]) for edge in _mentions(result)] == [
        ("EXTRACTED", 1.0),
    ]
    if mode != "watch":
        assert next(item for item in result["outcomes"]
                    if Path(item["path"]).name == "omitted.py")["status"] == "empty"
    assert _snapshot(tmp_path) == before
    _assert_private_markers_stripped(result)


@pytest.mark.parametrize("payload, inferred", [
    ({}, True), ({"nodes": []}, False), ({"_unsupported": True}, False),
])
def test_inference_distinguishes_empty_results_from_metadata_only_payloads(payload, inferred):
    nodes = [
        {"id": "guide", "label": "Guide", "file_type": "document", "source_file": "guide.md"},
        {"id": "widget", "label": "Widget", "file_type": "code", "source_file": "model.py",
         "_callable": True},
    ]
    per_file = [{"raw_calls": [{"caller_nid": "guide", "callee": "Widget",
                                "context": "Widget", "language": "markdown",
                                "source_file": "guide.md"}]},
                {"_markdown_source_file": "other.py", **payload}]
    edges = []
    resolve_markdown_mentions(per_file, nodes, edges)
    assert bool(_mentions({"edges": edges})) is inferred


@pytest.mark.parametrize("mode", ["public", "scoped", "watch"])
def test_empty_source_bytes_with_file_node_preserve_inference(tmp_path, mode):
    files = {"model.py": "class Widget:\n    pass\n", "empty.py": "",
             "guide.md": "# Guide\n`Widget`\n"}
    result = _reference_case(tmp_path, files, mode)
    assert [edge["context"] for edge in _mentions(result)] == ["Widget"]
    assert any(node["source_file"] == "empty.py" for node in result["nodes"])


@pytest.mark.parametrize("mode", ["public", "cached", "scoped", "watch"])
def test_prefixed_method_qualifiers_require_complete_actual_owners(tmp_path, mode):
    valid = ["Acme.Tools.Widget.Render()", "Widget.Render()", "Acme.Tools.Widget",
             "Gizmo.Widget.Render()"]
    invalid = ["Acme.Tools.Render()", "Tools.Render()", "Gizmo.Render()", "src.Render()",
               "src.Widget.Render()"]
    files = {
        "src/Gizmo.cs": "namespace Acme.Tools { public class Widget { public void Render() {} } }",
        "guide.md": "\n".join(f"# Case{i}\n`{span}`" for i, span in enumerate(valid + invalid)),
    }
    result = _reference_case(tmp_path, files, mode)
    assert {edge["context"] for edge in _mentions(result)} == set(valid)


@pytest.mark.parametrize("namespace_on", ["method", "class", "both"])
def test_namespace_evidence_includes_owners_wherever_metadata_is_recorded(namespace_on):
    from graphify.markdown_resolution import _evidence

    nodes: dict[str, dict] = {
        "widget": {"id": "widget", "label": "Widget", "file_type": "code",
                   "_callable": True, "_callable_class": True},
        "render": {"id": "render", "label": ".Render()", "file_type": "code",
                   "_callable": True},
    }
    for node_id in ({"method": ["render"], "class": ["widget"],
                     "both": ["render", "widget"]}[namespace_on]):
        nodes[node_id]["metadata"] = {"namespace": "Acme.Tools"}
    evidence = _evidence("render", nodes, {"render": {"widget"}}, source_path="src/Gizmo.cs")
    assert ("Acme", "Tools", "Widget") in evidence
    assert ("Widget",) in evidence
    assert ("Acme", "Tools") not in evidence
    assert ("src",) not in evidence
    assert ("src", "Gizmo") not in evidence


def test_unowned_namespace_callable_keeps_namespace_and_module_qualifiers():
    from graphify.markdown_resolution import _evidence

    node = {"id": "render", "label": "Render()", "file_type": "code", "_callable": True,
            "metadata": {"namespace": "Acme.Tools"}}
    assert _evidence("render", {"render": node}, {}, source_path="src/Gizmo.cs") == {
        ("Acme", "Tools"), ("src", "Gizmo"),
    }


def test_native_empty_watch_context_withholds_inference_but_keeps_explicit_cite(tmp_path,
                                                                              monkeypatch):
    import importlib

    from tests.test_markdown_code_spans import _native_mentions, _native_rebuild

    extraction = importlib.import_module("graphify.extract")
    corpus = tmp_path / "corpus"
    _write_files(corpus, {"good.py": "class Widget:\n    pass\n",
                          "omitted.py": "class Widget:\n    pass\n",
                          "guide.md": "# Bare\n`Widget`\n# Pinned\n`good.py::Widget`\n"})
    initial = _native_rebuild(corpus)
    assert {edge["context"] for edge in _native_mentions(initial)} == {"good.py::Widget"}
    real_extract = extraction._safe_extract_with_xaml_root

    def anomalous_empty(extractor, path, root, **kwargs):
        result = real_extract(extractor, path, root, **kwargs)
        if path.name == "omitted.py":
            assert any(node["label"] == "Widget" for node in result["nodes"])
            return {"nodes": [], "edges": [], "raw_calls": []}
        return result

    monkeypatch.setattr(extraction, "_safe_extract_with_xaml_root", anomalous_empty)
    doc = corpus / "guide.md"
    doc.write_text(doc.read_text() + "\nAn ordinary document event.\n")
    refreshed = _native_rebuild(corpus, [doc])
    mentions = _native_mentions(refreshed)
    assert {edge["context"] for edge in mentions} == {"good.py::Widget"}
    assert {(edge["confidence"], edge["confidence_score"]) for edge in mentions} == {
        ("EXTRACTED", 1.0),
    }


@pytest.mark.parametrize("namespace_on", ["method", "outer", "inner"])
def test_prefixed_evidence_preserves_complete_recorded_nested_owner_route(namespace_on):
    from graphify.markdown_resolution import _evidence

    # Exercise recorded ownership. The current C# extractor does not record
    # Outer -> Inner for a nested class, so the helper must not invent it.
    nodes: dict[str, dict] = {
        "outer": {"id": "outer", "label": "Outer", "file_type": "code", "_callable": True},
        "inner": {"id": "inner", "label": "Inner", "file_type": "code", "_callable": True},
        "method": {"id": "method", "label": ".Draw()", "file_type": "code", "_callable": True},
    }
    nodes[namespace_on]["metadata"] = {"namespace": "Acme.Tools"}
    parents = {"method": {"inner"}, "inner": {"outer"}}
    evidence = _evidence("method", nodes, parents, source_path="src/Gizmo.cs")
    assert evidence == {("Outer", "Inner"), ("Acme", "Tools", "Outer", "Inner"),
                        ("src", "Gizmo", "Outer", "Inner")}


@pytest.mark.parametrize("mode", ["public", "cached", "scoped", "watch"])
@pytest.mark.parametrize("closer", ["---", "...", "--- \t", "... \t"])
def test_frontmatter_delimiters_preserve_column_zero(tmp_path, mode, closer):
    files = {
        "model.py": "class Hidden:\n    pass\nclass Visible:\n    pass\n",
        "guide.md": ("--- \t\nnested:\n  ---\napi: \"`Hidden`\"\n"
                     + closer + "\n# Body\n`Visible`\n"),
    }
    result = _reference_case(tmp_path, files, mode)
    assert {edge["context"] for edge in _mentions(result)} == {"Visible"}


@pytest.mark.parametrize("mode", ["public", "cached", "scoped", "watch"])
@pytest.mark.parametrize("indent", ["  ", "\t"])
def test_indented_frontmatter_opener_keeps_visible_mentions(tmp_path, mode, indent):
    files = {
        "model.py": "class Widget:\n    pass\n",
        "guide.md": indent + "---\n# Visible\n`Widget`\n---\n",
    }
    result = _reference_case(tmp_path, files, mode)
    assert {edge["context"] for edge in _mentions(result)} == {"Widget"}


@pytest.mark.parametrize("mode", ["public", "cached", "scoped", "watch"])
@pytest.mark.parametrize("stem", ["renderer", "render"])
def test_free_callable_qualifiers_retain_the_defining_module(tmp_path, mode, stem):
    valid = [f"{stem}.render()", f"src.{stem}.render()", "render()",
             f"src/{stem}.py::render"]
    files = {
        f"src/{stem}.py": "def render():\n    return 1\n",
        "guide.md": "\n".join(f"# Case{i}\n`{span}`" for i, span in
                               enumerate(valid + ["src.render()"])),
    }
    result = _reference_case(tmp_path, files, mode)
    assert {edge["context"] for edge in _mentions(result)} == set(valid)


@pytest.mark.parametrize("mode", ["public", "cached", "scoped", "watch"])
@pytest.mark.parametrize("stem", ["Widget", "widget"])
def test_filename_type_qualifiers_require_the_recorded_type_name(tmp_path, mode, stem):
    valid = [f"src.{stem}.Widget", f"{stem}.Widget", "Widget", "Widget.render()",
             f"src.{stem}.Widget.render()"]
    if stem == "Widget":
        valid += ["src.Widget", "src.Widget.render()"]
    invalid = [] if stem == "Widget" else ["src.Widget", "src.Widget.render()"]
    files = {
        f"src/{stem}.py": "class Widget:\n    def render(self):\n        pass\n",
        "guide.md": "\n".join(f"# Case{i}\n`{span}`" for i, span in enumerate(valid + invalid)),
    }
    result = _reference_case(tmp_path, files, mode)
    assert {edge["context"] for edge in _mentions(result)} == set(valid)


@pytest.mark.parametrize("failure", ["missing", "stat", "read"])
@pytest.mark.parametrize("unavailable", ["unrelated.md", "other.py", "guide.md"])
def test_watch_unavailable_observations_exclude_unbound_evidence(tmp_path, monkeypatch,
                                                               failure, unavailable):
    import importlib

    from graphify.watch import _refresh_markdown_mentions

    extraction = importlib.import_module("graphify.extract")
    files = {
        "good.py": "class Widget:\n    pass\n",
        "other.py": "class Widget:\n    pass\n",
        "guide.md": "# Bare\n`Widget`\n# Good\n`good.py::Widget`\n# Other\n`other.py::Widget`\n",
        "unrelated.md": "# Notes\n`good.py::Widget`\n",
    }
    _write_files(tmp_path, files)
    paths = [tmp_path / name for name in files]
    result = extract(paths, source_root=tmp_path, parallel=False,
                     ambient_output=False, quiet=True)
    target = tmp_path / unavailable
    if failure == "missing":
        target.unlink()
    elif failure == "stat":
        real_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == target:
                raise PermissionError("controlled observation stat failure")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
    else:
        real_read = extraction.source_read_bytes

        def read(path):
            if path == target:
                raise PermissionError("controlled observation read failure")
            return real_read(path)

        monkeypatch.setattr(extraction, "source_read_bytes", read)
    context = extraction._extract_markdown_context(paths, root=tmp_path)
    outcome = next(item for item in context["outcomes"] if item["path"] == str(target))
    assert outcome["status"] == "failed_observation"
    assert all(node.get("source_file") != str(target) for node in context["nodes"])
    assert all(call.get("source_file") != str(target) for call in context["raw_calls"])
    refreshed = _refresh_markdown_mentions(
        result, paths, [tmp_path / "guide.md", tmp_path / "unrelated.md"], root=tmp_path,
    )
    expected = {(name, span) for name, span in [
        ("guide.md", "good.py::Widget"), ("guide.md", "other.py::Widget"),
        ("unrelated.md", "good.py::Widget"),
    ] if name != unavailable and not (unavailable == "other.py" and span.startswith("other.py"))}
    assert {(Path(edge["source_file"]).name, edge["context"])
            for edge in _mentions(refreshed)} == expected
    assert all(edge["confidence"] == "EXTRACTED" for edge in _mentions(refreshed))
    _assert_private_markers_stripped(refreshed)


@pytest.mark.parametrize("phase", ["extraction", "resolution"])
def test_watch_changed_observed_source_still_rejects_refresh(tmp_path, monkeypatch, phase):
    import copy
    import importlib

    from graphify.watch import _refresh_markdown_mentions

    extraction = importlib.import_module("graphify.extract")
    resolution = importlib.import_module("graphify.markdown_resolution")
    files = {"model.py": "class Widget:\n    pass\n",
             "guide.md": "# Pinned\n`model.py::Widget`\n"}
    _write_files(tmp_path, files)
    paths = [tmp_path / name for name in files]
    result = extract(paths, source_root=tmp_path, parallel=False,
                     ambient_output=False, quiet=True)
    before = copy.deepcopy(result)
    owner, function = ((extraction, "_extract_impl") if phase == "extraction"
                       else (resolution, "resolve_markdown_mentions"))
    original = getattr(owner, function)

    def change(*args, **kwargs):
        returned = original(*args, **kwargs)
        (tmp_path / "model.py").write_text("class Gadget:\n    pass\n")
        return returned

    monkeypatch.setattr(owner, function, change)
    with pytest.raises(RuntimeError, match="Markdown context inputs changed"):
        _refresh_markdown_mentions(result, paths, [tmp_path / "guide.md"], root=tmp_path)
    assert result == before


def test_native_watch_unavailable_context_publishes_stable_explicit_mentions(tmp_path, monkeypatch):
    import importlib

    from tests.test_markdown_code_spans import _native_mentions, _native_rebuild

    extraction = importlib.import_module("graphify.extract")
    corpus = tmp_path / "corpus"
    _write_files(corpus, {"good.py": "class Widget:\n    pass\n",
                          "guide.md": "# Bare\n`Widget`\n# Pinned\n`good.py::Widget`\n",
                          "unrelated.md": "# Notes\nOrdinary prose.\n"})
    initial = _native_rebuild(corpus)
    assert {edge["context"] for edge in _native_mentions(initial)} == {
        "Widget", "good.py::Widget",
    }
    real_read = extraction.source_read_bytes

    def read(path):
        if path == corpus / "unrelated.md":
            raise PermissionError("controlled unreadable context document")
        return real_read(path)

    monkeypatch.setattr(extraction, "source_read_bytes", read)
    doc = corpus / "guide.md"
    doc.write_text(doc.read_text() + "\nAn ordinary document event.\n")
    refreshed = _native_rebuild(corpus, [doc])
    assert {edge["context"] for edge in _native_mentions(refreshed)} == {"good.py::Widget"}
    assert all(edge["confidence"] == "EXTRACTED" for edge in _native_mentions(refreshed))


@pytest.mark.parametrize("initially_unavailable", [False, True])
def test_watch_context_availability_changes_still_reject(tmp_path, monkeypatch,
                                                        initially_unavailable):
    import importlib

    extraction = importlib.import_module("graphify.extract")
    _write_files(tmp_path, {"model.py": "class Widget:\n    pass\n",
                            "guide.md": "# Guide\n`model.py::Widget`\n"})
    paths = [tmp_path / "model.py", tmp_path / "guide.md"]
    real_read = extraction.source_read_bytes
    real_extract = extraction._extract_impl
    unavailable = initially_unavailable

    def read(path):
        if path == paths[0] and unavailable:
            raise PermissionError("controlled availability transition")
        return real_read(path)

    def change(*args, **kwargs):
        nonlocal unavailable
        result = real_extract(*args, **kwargs)
        unavailable = not unavailable
        return result

    monkeypatch.setattr(extraction, "source_read_bytes", read)
    monkeypatch.setattr(extraction, "_extract_impl", change)
    with pytest.raises(RuntimeError, match="Markdown context inputs changed"):
        extraction._extract_markdown_context(paths, root=tmp_path)


def test_watch_unavailable_source_remains_cited_file_ambiguity(tmp_path, monkeypatch):
    import importlib

    from graphify.watch import _refresh_markdown_mentions

    extraction = importlib.import_module("graphify.extract")
    files = {"a/widget.py": "class Widget:\n    pass\n",
             "b/widget.py": "class Other:\n    pass\n",
             "guide.md": "# Suffix\n`widget.py::Widget`\n# Exact\n`a/widget.py::Widget`\n"}
    _write_files(tmp_path, files)
    paths = [tmp_path / name for name in files]
    result = extract(paths, source_root=tmp_path, parallel=False,
                     ambient_output=False, quiet=True)
    real_read = extraction.source_read_bytes

    def read(path):
        if path == tmp_path / "b/widget.py":
            raise PermissionError("controlled unavailable competing file")
        return real_read(path)

    monkeypatch.setattr(extraction, "source_read_bytes", read)
    refreshed = _refresh_markdown_mentions(result, paths, [tmp_path / "guide.md"], root=tmp_path)
    assert {edge["context"] for edge in _mentions(refreshed)} == {"a/widget.py::Widget"}
