"""Stubbed conversion writes must preserve registered replacement archives."""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import unicodedata

import pytest

from graphify import canonical_replacement as replacement
from graphify import detect as detection
from graphify import google_workspace as workspace
from graphify import replacement_guard_issuers as issuers
from graphify import transaction as tx


KINDS = [".docx", ".xlsx", ".gdoc", ".gslides", ".gsheet"]
_RUN_GWS_EXPORT = workspace._run_gws_export


@pytest.fixture(autouse=True)
def isolated_io(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("conversion guard tests must not use network or subprocesses")

    class LocalIssuers:
        def trusted_issuers(self, *args, **kwargs):
            return frozenset({os.getuid()})

    monkeypatch.setattr(issuers, "issuer_snapshot", lambda: nullcontext(LocalIssuers()))
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(workspace, "_run_gws_export", refuse)
    monkeypatch.setattr(detection, "docx_to_markdown", refuse)
    monkeypatch.setattr(detection, "xlsx_to_markdown", refuse)


@pytest.fixture
def archives(tmp_path):
    journal = tmp_path / "journal.json"
    journal.write_text(json.dumps({"request_digest": "fixture", "state": "COMMITTED"}))

    def register(path):
        marker = path.parent / replacement.GUARD
        record = json.loads(marker.read_text()) if marker.exists() else {"schema": 1, "entries": {}}
        record["entries"][path.name] = {
            "journal": str(journal), "digest": "fixture", "role": "archive",
        }
        marker.write_text(json.dumps(record))
        marker.chmod(0o600)

    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "old.md").write_text("retained graph content")
    register(archive)
    return archive, register


def _source(tmp_path, kind):
    source = tmp_path / ("source" + kind)
    source.write_text('{"doc_id":"fixture-id"}')
    return source


def _sidecar(source, target):
    key = str(source.resolve())
    if source.suffix in {".docx", ".xlsx"}:
        key = unicodedata.normalize("NFC", key)
    digest = hashlib.sha256(key.encode()).hexdigest()[:8]
    return target / f"{source.stem}_{digest}.md"


def _stub_conversion(monkeypatch, calls, *, after=None):
    def office(source):
        calls.append(source)
        if after is not None:
            after(source)
        return "# Converted fixture"

    def export(file_id, mime_type, output, resource_key=None):
        calls.append(output)
        output.write_text("# Converted fixture")
        if after is not None:
            after(output)

    monkeypatch.setattr(detection, "docx_to_markdown", office)
    monkeypatch.setattr(detection, "xlsx_to_markdown", office)
    monkeypatch.setattr(workspace, "_run_gws_export", export)


def _convert(source, target):
    if source.suffix in {".docx", ".xlsx"}:
        return detection.convert_office_file(source, target)
    return workspace.convert_google_workspace_file(
        source, target, xlsx_to_markdown=lambda path: "# Converted fixture",
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("slot", ["archive", "descendant", "alias", "alias-descendant", "absent"])
def test_conversion_refuses_archive_before_directory_or_export(
    tmp_path, monkeypatch, archives, kind, slot,
):
    archive, register = archives
    absent = tmp_path / "absent"
    register(absent)
    alias = tmp_path / "alias"
    alias.symlink_to(archive, target_is_directory=True)
    target = {
        "archive": archive, "descendant": archive / "new" / "converted",
        "alias": alias, "alias-descendant": alias / "new" / "converted",
        "absent": absent / "converted",
    }[slot]
    source = _source(tmp_path, kind)
    before = replacement._tree(archive)
    calls = []
    _stub_conversion(monkeypatch, calls)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        _convert(source, target)

    assert calls == []
    assert replacement._tree(archive) == before
    assert not (archive / "new").exists()
    assert not absent.exists()


@pytest.mark.parametrize("kind", KINDS)
def test_conversion_refuses_selected_sidecar_alias(tmp_path, monkeypatch, archives, kind):
    archive, _ = archives
    source = _source(tmp_path, kind)
    target = tmp_path / "converted"
    target.mkdir()
    selected = _sidecar(source, target)
    selected.symlink_to(archive / "old.md")
    # Force Office's rewrite branch; the guard must also protect cached aliases.
    os.utime(archive / "old.md", ns=(1, 1))
    before = replacement._tree(archive)
    _stub_conversion(monkeypatch, [])

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        _convert(source, target)

    assert selected.is_symlink()
    assert replacement._tree(archive) == before


@pytest.mark.parametrize("kind", KINDS)
def test_conversion_rechecks_final_sidecar_registration(tmp_path, monkeypatch, archives, kind):
    _, register = archives
    source = _source(tmp_path, kind)
    target = tmp_path / "converted"
    target.mkdir()
    selected = _sidecar(source, target)
    _stub_conversion(monkeypatch, [], after=lambda _: register(selected))

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        _convert(source, target)

    assert not selected.exists()
    assert [path.name for path in target.iterdir()] == [replacement.GUARD]


@pytest.mark.parametrize("kind", [".gdoc", ".gslides", ".gsheet"])
def test_conversion_rechecks_directory_before_temporary_creation(
    tmp_path, monkeypatch, archives, kind,
):
    _, register = archives
    source = _source(tmp_path, kind)
    target = tmp_path / "converted"
    original = workspace._sidecar_path

    def select_after_registration(path, out_dir):
        register(out_dir)
        return original(path, out_dir)

    monkeypatch.setattr(workspace, "_sidecar_path", select_after_registration)
    calls = []
    _stub_conversion(monkeypatch, calls)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        _convert(source, target)

    assert calls == []
    assert not target.exists() or list(target.iterdir()) == []


@pytest.mark.parametrize("kind", [".gdoc", ".gslides", ".gsheet"])
def test_conversion_preserves_temporary_file_registered_during_export(
    tmp_path, monkeypatch, archives, kind,
):
    _, register = archives
    source = _source(tmp_path, kind)
    target = tmp_path / "converted"
    calls = []
    _stub_conversion(monkeypatch, calls, after=register)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        _convert(source, target)

    assert len(calls) == 1
    assert calls[0].read_text() == "# Converted fixture"
    assert not _sidecar(source, target).exists()


@pytest.mark.parametrize("kind", [".gdoc", ".gslides", ".gsheet"])
def test_conversion_refuses_temporary_alias_before_export(tmp_path, monkeypatch, archives, kind):
    archive, _ = archives
    source = _source(tmp_path, kind)
    target = tmp_path / "converted"
    original = workspace.tempfile.NamedTemporaryFile
    aliases = []

    @contextmanager
    def aliased_temporary(*args, **kwargs):
        with original(*args, **kwargs) as temporary:
            yield temporary
        alias = Path(temporary.name)
        alias.unlink()
        alias.symlink_to(archive / "old.md")
        aliases.append(alias)

    monkeypatch.setattr(workspace.tempfile, "NamedTemporaryFile", aliased_temporary)
    calls = []
    _stub_conversion(monkeypatch, calls)
    before = replacement._tree(archive)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        _convert(source, target)

    assert calls == []
    assert len(aliases) == 1 and aliases[0].is_symlink()
    assert replacement._tree(archive) == before


@pytest.mark.parametrize("kind", [".gdoc", ".gslides", ".gsheet"])
def test_conversion_rechecks_after_frontmatter_formatting(tmp_path, monkeypatch, archives, kind):
    _, register = archives
    source = _source(tmp_path, kind)
    target = tmp_path / "converted"
    original = workspace._with_frontmatter

    def format_then_register(*args):
        content = original(*args)
        register(_sidecar(source, target))
        return content

    _stub_conversion(monkeypatch, [])
    monkeypatch.setattr(workspace, "_with_frontmatter", format_then_register)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        _convert(source, target)

    assert not _sidecar(source, target).exists()
    assert [path.name for path in target.iterdir()] == [replacement.GUARD]


@pytest.mark.parametrize("kind", KINDS)
def test_ordinary_conversion_retains_sidecar_contract(tmp_path, monkeypatch, kind):
    source = _source(tmp_path, kind)
    target = tmp_path / "new" / "converted"
    calls = []
    _stub_conversion(monkeypatch, calls)

    output = _convert(source, target)

    assert output is not None
    assert output == _sidecar(source, target)
    assert "# Converted fixture" in output.read_text()
    assert list(target.iterdir()) == [output]
    assert len(calls) == 1


def test_detection_propagates_google_archive_refusal(tmp_path, monkeypatch):
    _source(tmp_path, ".gdoc")

    def refuse(*args, **kwargs):
        raise tx.PendingTransactionError("preserved graph archive is read-only")

    monkeypatch.setattr(detection, "convert_google_workspace_file", refuse)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        detection.detect(tmp_path, google_workspace=True)


def test_detection_keeps_ordinary_google_export_failure_note(tmp_path, monkeypatch):
    source = _source(tmp_path, ".gdoc")

    def fail_export(*args, **kwargs):
        raise RuntimeError("intercepted conversion failure")

    monkeypatch.setattr(detection, "convert_google_workspace_file", fail_export)
    result = detection.detect(tmp_path, google_workspace=True)

    assert result["files"]["document"] == []
    assert any(str(source) in entry and "intercepted conversion failure" in entry
               for entry in result["skipped_sensitive"])


@pytest.mark.parametrize("alias", [False, True])
def test_direct_export_refuses_archive_before_directory_or_subprocess(
    tmp_path, monkeypatch, archives, alias,
):
    archive, _ = archives
    target = archive / "new" / "export.md"
    if alias:
        target = tmp_path / "export.md"
        target.symlink_to(archive / "old.md")
    # Exercise the real export boundary while intercepting CLI discovery/launch.
    monkeypatch.setattr(workspace, "_run_gws_export", _RUN_GWS_EXPORT)
    calls = []
    monkeypatch.setattr(workspace.shutil, "which", lambda name: "fixture-gws")

    def refuse_launch(*args, **kwargs):
        calls.append("launch")
        pytest.fail("archive export reached subprocess launch")

    monkeypatch.setattr(workspace.subprocess, "run", refuse_launch)
    before = replacement._tree(archive)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        workspace._run_gws_export("fixture", "text/markdown", target)

    assert calls == []
    assert replacement._tree(archive) == before


def test_direct_export_rechecks_selected_alias_after_cli_lookup(tmp_path, monkeypatch, archives):
    _, register = archives
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    destination = ordinary / "export.md"
    destination.write_text("existing export")
    selected = tmp_path / "export-alias.md"
    selected.symlink_to(destination)
    monkeypatch.setattr(workspace, "_run_gws_export", _RUN_GWS_EXPORT)

    def lookup(name):
        register(selected)
        return "fixture-gws"

    monkeypatch.setattr(workspace.shutil, "which", lookup)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        workspace._run_gws_export("fixture", "text/markdown", selected)

    assert selected.is_symlink()
    assert destination.read_text() == "existing export"
