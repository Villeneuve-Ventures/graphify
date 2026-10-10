"""Checkout transformations and interrupted preparation cannot publish bad state."""
from contextlib import contextmanager
import os
from pathlib import Path

import pytest

from graphify import merge_finalize as finalizer
from tests.test_merge_commit_lifecycle import git, isolated_authority  # noqa: F401
from tests.test_merge_finalize import pending_repo

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX manual publication")


def preserved(repo):
    output = repo / "graphify-out"
    return ((repo / ".git/index").read_bytes(),
            {str(p.relative_to(output)): p.read_bytes() for p in output.rglob("*") if p.is_file()})


@pytest.mark.parametrize("stage", ["prepare", "commit"])
@pytest.mark.parametrize("cached", [False, True])
def test_ident_conversion_refuses_without_publication(stage, cached):
    repo = pending_repo()
    (repo / "$Id$.py").write_text("def ident():\n    return 3\n")
    git(repo, "add", "$Id$.py")
    if stage == "commit":
        finalizer.finalize_merge(repo, "graphify-out")
    with (repo / ".gitattributes").open("a") as stream:
        stream.write("\ngraphify-out/* ident\n")
    if cached:
        git(repo, "add", ".gitattributes")
    before = preserved(repo)
    staged_entries = git(repo, "ls-files", "--stage", "-v").stdout
    head = git(repo, "rev-parse", "HEAD").stdout
    if stage == "prepare":
        with pytest.raises(finalizer.MergeFinalizeError, match="output.*unsupported"):
            finalizer.finalize_merge(repo, "graphify-out")
        assert not finalizer._record_path(repo, "graphify-out").exists()
    else:
        result = git(repo, "commit", "--no-edit", skip_hooks=False, check=False)
        assert result.returncode != 0
        assert "unsupported" in result.stderr
    assert git(repo, "rev-parse", "HEAD").stdout == head
    assert (repo / ".git/MERGE_HEAD").exists()
    assert git(repo, "ls-files", "--stage", "-v").stdout == staged_entries
    assert preserved(repo)[1] == before[1]
    if stage == "prepare":
        assert preserved(repo)[0] == before[0]


def test_partial_record_write_leaves_retry_usable(monkeypatch):
    repo = pending_repo()
    fdopen = os.fdopen

    @contextmanager
    def interrupted(fd, mode):
        with fdopen(fd, mode) as stream:
            class PartialWrite:
                def __getattr__(self, name):
                    return getattr(stream, name)

                def write(self, payload):
                    if isinstance(payload, bytes) and b'"prepared_entries"' in payload:
                        stream.write(payload[:23])
                        stream.flush()
                        raise OSError("injected partial record write")
                    return stream.write(payload)
            yield PartialWrite()

    before = preserved(repo)
    monkeypatch.setattr(os, "fdopen", interrupted)
    with pytest.raises(finalizer.MergeFinalizeError, match="partial record write"):
        finalizer.finalize_merge(repo, "graphify-out")
    monkeypatch.setattr(os, "fdopen", fdopen)
    assert preserved(repo) == before
    assert not finalizer._record_path(repo, "graphify-out").exists()
    assert not (repo / ".git/index.lock").exists()
    content = finalizer.finalize_merge(repo, "graphify-out")
    assert finalizer.validate_index_bundle(repo, "graphify-out").content_id == content
    finalizer.cancel_merge(repo, "graphify-out")
    assert preserved(repo)[1] == before[1]


def test_record_publication_does_not_replace_existing_target(tmp_path):
    path = tmp_path / "record.json"
    path.write_bytes(b"existing record\n")
    with pytest.raises(FileExistsError):
        finalizer._write_record(path, {"record": "replacement"})
    assert path.read_bytes() == b"existing record\n"
    assert list(tmp_path.iterdir()) == [path]


def test_real_extraction_refuses_before_decoding_oversized_output(monkeypatch):
    repo = Path.home() / "extract-repo"
    repo.mkdir()
    git(repo, "init")
    source = "".join(f"def f{i}():\n    return f{i+1}()\n" for i in range(100)).encode()
    assert len(source) < 8192
    monkeypatch.setenv("GRAPHIFY_MAX_GRAPH_BYTES", "8192")
    with pytest.raises(finalizer.MergeFinalizeError, match="extraction output exceeds"):
        finalizer._extract(repo, {"a.py": ("100644", source)})
    assert not (repo / ".git/index").exists()
    assert not list((repo / ".git").glob("graphify-finalize-*"))


@pytest.mark.parametrize("script", ["raise RuntimeError('extractor refused')", "print('not json')"])
def test_extractor_failure_remains_a_refusal(monkeypatch, script):
    repo = Path.home() / "extract-repo"
    repo.mkdir()
    git(repo, "init")
    monkeypatch.setattr(finalizer, "_EXTRACT_SCRIPT", script)
    with pytest.raises((finalizer.MergeFinalizeError, ValueError)):
        finalizer._extract(repo, {})
    assert not list((repo / ".git").glob("graphify-finalize-*"))
