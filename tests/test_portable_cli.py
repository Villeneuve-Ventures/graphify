"""The explicit portable query must remain read-only even with query logging enabled."""
import subprocess

import pytest

from graphify import __main__ as mainmod, portable


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *args],
        check=True, capture_output=True, text=True,
    ).stdout


def bundle_repo(root):
    git(root, "init", "-q")
    git(root, "config", "user.name", "Portable test")
    git(root, "config", "user.email", "portable@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / "hello.py").write_text("def hello():\n    return 1\n")
    git(root, "add", "hello.py")
    git(root, "commit", "-qm", "source")
    _, sources = portable.source_records_from_tree(root, "graphify-out")
    graph = {
        "graph": {}, "directed": False, "multigraph": False,
        "nodes": [{"id": "hello", "label": "hello", "source_file": "hello.py"}],
        "links": [],
    }
    output = root / "graphify-out"
    output.mkdir()
    for name, payload in portable.make_bundle(graph, sources, "graphify-out").items():
        (output / name).write_bytes(payload)
    git(root, "add", "graphify-out")
    git(root, "commit", "-qm", "bundle")
    return output


def test_portable_query_uses_retained_bytes_without_logging(tmp_path, monkeypatch, capsys):
    bundle_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRAPHIFY_QUERY_LOG", "1")
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    from graphify import querylog
    monkeypatch.setattr(querylog, "log_query", lambda **_: pytest.fail("portable query logged"))
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", "query", "hello", "--portable"])
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    mainmod.main()
    out = capsys.readouterr().out
    assert "Portable bundle" in out and "hello" in out
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_portable_query_requires_complete_source_projection(tmp_path, monkeypatch, capsys):
    bundle_repo(tmp_path)
    (tmp_path / "new.py").write_text("def new(): pass\n")
    git(tmp_path, "add", "new.py")
    git(tmp_path, "commit", "-qm", "changed source")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", "query", "hello", "--portable"])
    with pytest.raises(SystemExit) as exc:
        mainmod.main()
    assert exc.value.code == 1
    assert "source" in capsys.readouterr().err


def test_normal_query_does_not_fall_back_to_portable(tmp_path, monkeypatch, capsys):
    bundle_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", "query", "hello"])
    with pytest.raises(SystemExit):
        mainmod.main()
    assert "portable" in capsys.readouterr().err
