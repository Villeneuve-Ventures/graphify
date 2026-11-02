"""Audio delegation downloads privately and admits its actual publication path."""
import hashlib
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from graphify import canonical_replacement as replacement
from graphify import security, transcribe
from graphify import transaction as tx


URL = "https://youtube.com/watch?v=fixture"
STEM = "yt_" + hashlib.sha1(URL.encode(), usedforsecurity=False).hexdigest()[:12]


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("audio guard tests must not use network or subprocesses")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(security, "validate_url", lambda url: None)
    private_root = tmp_path / "private-downloads"
    private_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private_root))
    state = SimpleNamespace(options=[], paths=[], ext="m4a", reported_ext="m4a",
                            before_write=lambda: None, error=None, private_root=private_root)

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options
            state.options.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            assert url == URL
            assert download is True
            out = Path(self.options["outtmpl"].replace("%(ext)s", state.ext))
            state.paths.append(out)
            state.before_write()
            if state.error is not None:
                raise state.error
            out.write_bytes(b"local audio bytes")
            return {"ext": state.reported_ext}

    monkeypatch.setattr(transcribe, "_get_yt_dlp", lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(transcribe, "_get_whisper", refuse)
    return state


@pytest.fixture
def archive_registration(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "old.m4a").write_bytes(b"preserved audio")
    journal = tmp_path / "journal.json"
    journal.write_bytes(replacement._bytes({
        "request_digest": "prior-operation", "state": "COMMITTED",
    }))

    def register(path):
        replacement._register(path, journal, "prior-operation", "archive")

    register(archive)
    return archive, register


@pytest.mark.parametrize("slot", ["archive", "descendant", "alias", "absent-archive"])
def test_audio_refuses_protected_directory_before_download(
    tmp_path, downloader, archive_registration, slot,
):
    archive, register = archive_registration
    absent = tmp_path / "absent-archive"
    register(absent)
    alias = tmp_path / "alias"
    alias.symlink_to(archive, target_is_directory=True)
    target = {
        "archive": archive, "descendant": archive / "new" / "raw",
        "alias": alias, "absent-archive": absent,
    }[slot]
    before = replacement._tree(archive)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        transcribe.download_audio(URL, target)

    assert downloader.options == []
    assert replacement._tree(archive) == before
    assert not absent.exists()
    assert not (archive / "new").exists()


@pytest.mark.parametrize("cached", [False, True])
def test_audio_selected_child_alias_is_guarded(
    tmp_path, downloader, archive_registration, cached,
):
    archive, _ = archive_registration
    target = tmp_path / "raw"
    target.mkdir()
    selected = target / f"{STEM}.m4a"
    selected.symlink_to(archive / ("old.m4a" if cached else "new.m4a"))
    before = replacement._tree(archive)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        transcribe.download_audio(URL, target)

    assert replacement._tree(archive) == before
    assert selected.is_symlink()
    assert len(downloader.options) == (0 if cached else 1)
    assert list(downloader.private_root.iterdir()) == []


@pytest.mark.parametrize("late", [False, True])
def test_audio_selected_absent_slot_is_guarded(
    tmp_path, downloader, archive_registration, late,
):
    _, register = archive_registration
    target = tmp_path / "raw"
    target.mkdir()
    selected = target / f"{STEM}.m4a"
    if late:
        downloader.before_write = lambda: register(selected)
    else:
        register(selected)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        transcribe.download_audio(URL, target)

    assert not selected.exists()
    assert sorted(path.name for path in target.iterdir()) == [replacement.GUARD]
    assert list(downloader.private_root.iterdir()) == []


@pytest.mark.parametrize("ext", ["m4a", "opus"])
def test_audio_normal_download_is_private_then_published(tmp_path, downloader, ext):
    target = tmp_path / "nested" / "raw"
    downloader.ext = ext
    downloader.reported_ext = "m4a"  # exercises existing fallback for actual opus output

    out = transcribe.download_audio(URL, target)

    assert out == target / f"{STEM}.{ext}"
    assert out.read_bytes() == b"local audio bytes"
    downloaded = downloader.paths[0]
    assert downloaded.is_relative_to(downloader.private_root)
    assert not downloaded.exists()
    options = dict(downloader.options[0])
    assert options.pop("outtmpl") == str(downloaded.parent / f"{STEM}.%(ext)s")
    assert options == {
        "format": "bestaudio[ext=m4a]/bestaudio/best", "quiet": True,
        "no_warnings": True, "noplaylist": True, "postprocessors": [],
    }
    assert list(downloader.private_root.iterdir()) == []


@pytest.mark.parametrize("ext", ["m4a", "opus", "mp3", "ogg", "wav", "webm"])
def test_audio_ordinary_cache_is_unchanged(tmp_path, downloader, ext):
    cached = tmp_path / f"{STEM}.{ext}"
    cached.write_bytes(b"cached audio")

    assert transcribe.download_audio(URL, tmp_path) == cached
    assert cached.read_bytes() == b"cached audio"
    assert downloader.options == []
    assert list(downloader.private_root.iterdir()) == []


def test_audio_downloader_error_propagates_and_private_files_are_removed(tmp_path, downloader):
    failure = RuntimeError("intercepted downloader error")
    downloader.error = failure
    target = tmp_path / "raw"

    with pytest.raises(RuntimeError) as caught:
        transcribe.download_audio(URL, target)

    assert caught.value is failure
    assert list(target.iterdir()) == []
    assert list(downloader.private_root.iterdir()) == []
