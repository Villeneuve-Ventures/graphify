"""Saved-root decoding through the consumers that use it."""

import json
import pytest


@pytest.fixture(params=[("utf-8", ""), ("utf-8", "\n"), ("utf-8-sig", "\r\n")])
def saved_root(tmp_path, request):
    root = tmp_path / "corpus-é "
    root.mkdir()
    out = tmp_path / "elsewhere"
    out.mkdir()
    encoding, ending = request.param
    (out / ".graphify_root").write_bytes((str(root) + ending).encode(encoding))
    return root, out


def test_build_merge_prunes_using_saved_root(saved_root):
    from graphify.build import build_merge

    root, out = saved_root
    graph = out / "graph.json"
    graph.write_text(json.dumps({"nodes": [
        {"id": "old", "label": "old", "source_file": "gone.py"},
        {"id": "keep", "label": "keep", "source_file": "keep.py"},
    ], "links": []}))
    merged = build_merge([], graph, prune_sources=[str(root / "gone.py")], dedup=False)
    assert set(merged.nodes) == {"keep"}


def test_reflect_resolves_saved_root(saved_root):
    from graphify.reflect import _resolve_source_path

    root, out = saved_root
    source = root / "code.py"
    source.write_text("x = 1\n")
    assert _resolve_source_path("code.py", out / "graph.json") == source


def test_watch_retains_saved_source_identity(saved_root):
    from graphify.build import _norm_source_file
    from graphify.watch import _StoredSourcePaths

    root, out = saved_root
    paths = _StoredSourcePaths(
        {}, out=out, project_root=out.parent, watch_root=root,
        normalize_source=_norm_source_file,
    )
    assert paths.identity("code.py") == (root / "code.py").as_posix()
    assert paths.in_watch_root("code.py")


def test_update_cli_uses_saved_root(saved_root, monkeypatch):
    import graphify.__main__ as main
    import graphify.cli as cli
    import graphify.watch as watch

    root, out = saved_root
    monkeypatch.chdir(out.parent)
    monkeypatch.setenv("GRAPHIFY_OUT", str(out))
    monkeypatch.setattr(cli, "_GRAPHIFY_OUT", str(out))
    monkeypatch.setattr("sys.argv", ["graphify", "update"])
    calls = []
    monkeypatch.setattr(watch, "_rebuild_code", lambda path, **kw: calls.append(path) or True)
    main.main()
    assert calls == [root]


@pytest.mark.parametrize("kind", ["COMMIT", "CHECKOUT"])
def test_rebuild_hook_uses_saved_root(saved_root, monkeypatch, kind):
    from graphify import hooks, watch

    root, out = saved_root
    monkeypatch.chdir(out.parent)
    monkeypatch.setenv("GRAPHIFY_OUT", str(out))
    monkeypatch.setenv("GRAPHIFY_CHANGED", "code.py")
    monkeypatch.setenv("GRAPHIFY_REBUILD_TIMEOUT", "0")
    monkeypatch.delenv("_GFY_REBUILD_CURRENT_ROOT", raising=False)
    monkeypatch.setattr(watch, "_apply_resource_limits", lambda: None)
    calls = []
    monkeypatch.setattr(watch, "_rebuild_code", lambda path, **kw: calls.append(path) or True)
    exec(getattr(hooks, f"_REBUILD_BODY_{kind}"), {})
    assert calls == [root]


@pytest.mark.parametrize("value", ["", "   ", "\tpath\t ", "relative/path", "a\nb"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_decoder_preserves_path_characters(tmp_path, value, encoding):
    from graphify.paths import read_graphify_root

    marker = tmp_path / ".graphify_root"
    marker.write_bytes((value + "\r\n").encode(encoding))
    assert read_graphify_root(marker) == value


def test_decoder_propagates_read_and_decode_errors(tmp_path):
    from graphify.paths import read_graphify_root

    marker = tmp_path / ".graphify_root"
    with pytest.raises(FileNotFoundError):
        read_graphify_root(marker)
    marker.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        read_graphify_root(marker)
