"""Local transcription admits its output before cache use and transcript writes."""
import socket
import subprocess
from types import SimpleNamespace

import pytest

from graphify import canonical_replacement as replacement
from graphify import transcribe
from graphify import transaction as tx


@pytest.fixture
def whisper(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("transcript guard tests must not use downloads, network or subprocesses")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(transcribe, "download_audio", refuse)
    state = SimpleNamespace(models=[], calls=[], during_segments=lambda: None)

    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            state.models.append((model, kwargs))

        def transcribe(self, path, **kwargs):
            state.calls.append((path, kwargs))

            def segments():
                yield SimpleNamespace(text="  New transcript.  ")
                state.during_segments()
                yield SimpleNamespace(text=" ")
                yield SimpleNamespace(text="Second line.")

            return segments(), SimpleNamespace(language="en")

    monkeypatch.setattr(transcribe, "_get_whisper", lambda: FakeWhisperModel)
    return state


@pytest.fixture
def archive_registration(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "old.txt").write_bytes(b"preserved transcript")
    journal = tmp_path / "journal.json"
    journal.write_bytes(replacement._bytes({
        "request_digest": "prior-operation", "state": "COMMITTED",
    }))

    def register(path):
        replacement._register(path, journal, "prior-operation", "archive")

    register(archive)
    return archive, register


@pytest.mark.parametrize("slot", ["archive", "descendant", "alias", "absent-archive"])
def test_transcribe_refuses_protected_output_before_creation_or_model(
    tmp_path, whisper, archive_registration, slot,
):
    archive, register = archive_registration
    absent = tmp_path / "absent-archive"
    register(absent)
    alias = tmp_path / "alias"
    alias.symlink_to(archive, target_is_directory=True)
    target = {
        "archive": archive, "descendant": archive / "new" / "transcripts",
        "alias": alias, "absent-archive": absent,
    }[slot]
    before = replacement._tree(archive)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        transcribe.transcribe(tmp_path / "clip.mp4", output_dir=target)

    assert whisper.models == []
    assert replacement._tree(archive) == before
    assert not absent.exists()
    assert not (archive / "new").exists()


@pytest.mark.parametrize("cached,force", [(False, False), (True, False), (True, True)])
def test_transcribe_refuses_registered_selected_transcript(
    tmp_path, whisper, archive_registration, cached, force,
):
    _, register = archive_registration
    target = tmp_path / "transcripts"
    target.mkdir()
    selected = target / "clip.txt"
    if cached:
        selected.write_bytes(b"cached transcript")
    register(selected)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        transcribe.transcribe(tmp_path / "clip.mp4", output_dir=target, force=force)

    assert whisper.models == []
    if cached:
        assert selected.read_bytes() == b"cached transcript"
    else:
        assert not selected.exists()


@pytest.mark.parametrize("cached", [False, True])
def test_transcribe_refuses_selected_transcript_alias(
    tmp_path, whisper, archive_registration, cached,
):
    archive, _ = archive_registration
    target = tmp_path / "transcripts"
    target.mkdir()
    selected = target / "clip.txt"
    selected.symlink_to(archive / ("old.txt" if cached else "new.txt"))
    before = replacement._tree(archive)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        transcribe.transcribe(tmp_path / "clip.mp4", output_dir=target, force=True)

    assert whisper.models == []
    assert selected.is_symlink()
    assert replacement._tree(archive) == before


@pytest.mark.parametrize("cached", [False, True])
def test_transcribe_rechecks_registration_after_consuming_segments(
    tmp_path, whisper, archive_registration, cached,
):
    _, register = archive_registration
    target = tmp_path / "transcripts"
    target.mkdir()
    selected = target / "clip.txt"
    if cached:
        selected.write_bytes(b"cached transcript")
    whisper.during_segments = lambda: register(selected)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        transcribe.transcribe(tmp_path / "clip.mp4", output_dir=target, force=True)

    assert len(whisper.calls) == 1
    if cached:
        assert selected.read_bytes() == b"cached transcript"
    else:
        assert not selected.exists()


@pytest.mark.parametrize("cached,force", [(False, False), (True, False), (True, True)])
def test_ordinary_transcription_preserves_cache_and_force(tmp_path, whisper, cached, force):
    target = tmp_path / "nested" / "transcripts"
    selected = target / "clip.txt"
    if cached:
        target.mkdir(parents=True)
        selected.write_bytes(b"cached transcript")

    assert transcribe.transcribe(tmp_path / "clip.mp4", output_dir=target, force=force) == selected

    uses_cache = cached and not force
    assert selected.read_text() == ("cached transcript" if uses_cache else "New transcript.\nSecond line.")
    assert len(whisper.calls) == (0 if uses_cache else 1)


def test_transcribe_all_warns_and_skips_protected_output(
    tmp_path, whisper, archive_registration, capsys,
):
    archive, _ = archive_registration
    before = replacement._tree(archive)

    assert transcribe.transcribe_all([str(tmp_path / "clip.mp4")], output_dir=archive) == []

    assert "archive is read-only" in capsys.readouterr().out
    assert whisper.models == []
    assert replacement._tree(archive) == before
