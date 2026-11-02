"""URL ingestion refuses retained archive mutations at library write boundaries."""
import socket
import subprocess
import sys
import urllib.error

import pytest

from graphify import canonical_replacement as replacement
from graphify import ingest as ingestion
from graphify import transaction as tx


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("ingest guard tests must not use network or subprocesses")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(ingestion, "validate_url", lambda url: None)
    monkeypatch.setattr(ingestion, "safe_fetch", refuse)
    monkeypatch.setattr(ingestion, "safe_fetch_text", refuse)
    monkeypatch.setattr(ingestion, "_fetch_html", refuse)


@pytest.fixture
def archive_registration(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "old.txt").write_bytes(b"retained old graph")
    journal = tmp_path / "journal.json"
    journal.write_bytes(replacement._bytes({
        "request_digest": "prior-operation", "state": "COMMITTED",
    }))

    def register(path):
        replacement._register(path, journal, "prior-operation", "archive")

    register(archive)
    return archive, register


@pytest.mark.parametrize("slot", [
    "archive", "descendant", "alias", "alias-descendant", "absent-archive",
])
@pytest.mark.parametrize("url_type", ["webpage", "tweet", "arxiv", "pdf", "image", "youtube"])
def test_ingest_refuses_archive_before_creation_or_fetch(
    tmp_path, monkeypatch, archive_registration, slot, url_type,
):
    archive, register = archive_registration
    absent = tmp_path / "absent-archive"
    register(absent)
    alias = tmp_path / "alias"
    alias.symlink_to(archive, target_is_directory=True)
    destinations = {
        "archive": archive,
        "descendant": archive / "new" / "raw",
        "alias": alias,
        "alias-descendant": alias / "new" / "raw",
        "absent-archive": absent / "raw",
    }
    before = replacement._tree(archive)
    calls = []

    def fetch(*args, **kwargs):
        calls.append("fetch")
        return "new content", "article.md"

    def download(*args, **kwargs):
        calls.append("download")
        return destinations[slot] / "download.bin"

    from graphify import transcribe

    monkeypatch.setattr(ingestion, "_detect_url_type", lambda url: url_type)
    for name in ("_fetch_webpage", "_fetch_tweet", "_fetch_arxiv"):
        monkeypatch.setattr(ingestion, name, fetch)
    monkeypatch.setattr(ingestion, "_download_binary", download)
    monkeypatch.setattr(transcribe, "download_audio", download)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        ingestion.ingest("https://example.com/page", destinations[slot])

    assert calls == []
    assert replacement._tree(archive) == before
    assert not (archive / "new").exists()
    assert not absent.exists()


def test_text_collision_selected_alias_is_guarded(tmp_path, monkeypatch, archive_registration):
    archive, _ = archive_registration
    target = tmp_path / "raw"
    target.mkdir()
    (target / "article.md").write_text("ordinary existing content")
    alias = target / "article_1.md"
    alias.symlink_to(archive / "new.md")
    before = replacement._tree(archive)
    monkeypatch.setattr(ingestion, "_fetch_webpage", lambda *args: ("new", "article.md"))

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        ingestion.ingest("https://example.com/page", target)

    assert replacement._tree(archive) == before
    assert alias.is_symlink()
    assert (target / "article.md").read_text() == "ordinary existing content"


@pytest.mark.parametrize("suffix", [".pdf", ".png"])
def test_binary_selected_alias_is_guarded(tmp_path, monkeypatch, archive_registration, suffix):
    archive, _ = archive_registration
    target = tmp_path / "raw"
    target.mkdir()
    url = f"https://example.com/file{suffix}"
    out = target / ingestion._safe_filename(url, suffix)
    out.symlink_to(archive / "old.txt")
    before = replacement._tree(archive)
    monkeypatch.setattr(ingestion, "safe_fetch", lambda url: b"replacement bytes")

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        ingestion.ingest(url, target)

    assert replacement._tree(archive) == before
    assert out.is_symlink()


@pytest.mark.parametrize("binary", [False, True])
def test_selected_file_rechecked_after_fetch(tmp_path, monkeypatch, archive_registration, binary):
    _, register = archive_registration
    target = tmp_path / "raw"
    url = "https://example.com/file.pdf" if binary else "https://example.com/page"
    filename = ingestion._safe_filename(url, ".pdf") if binary else "article.md"

    def fetch(*args):
        register(target / filename)
        return b"new bytes" if binary else ("new text", filename)

    monkeypatch.setattr(ingestion, "safe_fetch" if binary else "_fetch_webpage", fetch)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        ingestion.ingest(url, target)

    assert not (target / filename).exists()
    assert sorted(path.name for path in target.iterdir()) == [replacement.GUARD]


def test_youtube_rechecks_target_before_delegation(tmp_path, monkeypatch, archive_registration):
    _, register = archive_registration
    target = tmp_path / "raw"
    calls = []
    from graphify import transcribe

    def download(*args):
        calls.append(args)
        return target / "audio.m4a"

    monkeypatch.setattr(ingestion, "validate_url", lambda url: register(target))
    monkeypatch.setattr(transcribe, "download_audio", download)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        ingestion.ingest("https://youtube.com/watch?v=example", target)

    assert calls == []
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("url_type", ["webpage", "tweet", "arxiv", "pdf", "image", "youtube"])
def test_ordinary_ingest_still_saves(tmp_path, monkeypatch, url_type):
    target = tmp_path / "new" / "raw"
    monkeypatch.setattr(ingestion, "_detect_url_type", lambda url: url_type)
    for name in ("_fetch_webpage", "_fetch_tweet", "_fetch_arxiv"):
        monkeypatch.setattr(ingestion, name, lambda *args: ("new text", "article.md"))
    monkeypatch.setattr(ingestion, "safe_fetch", lambda url: b"new bytes")

    def download(url, destination):
        assert destination == target
        out = destination / "audio.m4a"
        out.write_bytes(b"local audio fixture")
        return out

    from graphify import transcribe

    monkeypatch.setattr(transcribe, "download_audio", download)
    out = ingestion.ingest("https://example.com/page.png", target)

    assert out.parent == target
    assert out.read_bytes() == (
        b"new bytes" if url_type in {"pdf", "image"}
        else b"local audio fixture" if url_type == "youtube"
        else b"new text"
    )


def test_ordinary_text_keeps_collision_suffix(tmp_path, monkeypatch):
    (tmp_path / "article.md").write_text("old content")
    monkeypatch.setattr(ingestion, "_fetch_webpage", lambda *args: ("new content", "article.md"))

    out = ingestion.ingest("https://example.com/page", tmp_path)

    assert out == tmp_path / "article_1.md"
    assert out.read_text() == "new content"
    assert (tmp_path / "article.md").read_text() == "old content"


def test_ordinary_fetch_error_keeps_existing_contract(tmp_path, monkeypatch):
    def fetch(*args):
        raise urllib.error.URLError("intercepted fetch failure")

    monkeypatch.setattr(ingestion, "_fetch_webpage", fetch)
    with pytest.raises(RuntimeError, match="ingest: failed to fetch") as caught:
        ingestion.ingest("https://example.com/page", tmp_path)
    assert isinstance(caught.value.__cause__, urllib.error.URLError)


def test_add_cli_reports_guard_refusal(tmp_path, monkeypatch, capsys, archive_registration):
    from graphify import cli

    archive, _ = archive_registration
    before = replacement._tree(archive)
    monkeypatch.setattr(sys, "argv", [
        "graphify", "add", "https://example.com/page", "--dir", str(archive),
    ])
    monkeypatch.setattr(ingestion, "_fetch_webpage", lambda *args: ("new text", "article.md"))

    with pytest.raises(SystemExit) as caught:
        cli.dispatch_command("add")

    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert "error: preserved graph archive is read-only" in captured.err
    assert captured.out == ""
    assert replacement._tree(archive) == before
