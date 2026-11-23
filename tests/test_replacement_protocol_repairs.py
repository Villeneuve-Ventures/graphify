"""Preservation regressions use disposable data and modeled maintenance only."""
import json
import os
from pathlib import Path
import stat

import pytest

from graphify import canonical_replacement as replacement
from graphify import hook_installation
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


def _prepared(request, plan):
    return {**plan, "request_digest": replacement._sha(replacement._bytes(request)),
            "state": "PREPARED", "container": None, "new_identity": None, "owner": None}


def _deny_acl(monkeypatch, path):
    """Model one unsafe ACL while leaving lock and other directory ACLs real."""
    identity = path.stat().st_dev, path.stat().st_ino
    real_acl = hook_installation._acl_safe

    def safe(fd):
        info = os.fstat(fd)
        return False if (info.st_dev, info.st_ino) == identity else real_acl(fd)

    monkeypatch.setattr(hook_installation, "_acl_safe", safe)


@pytest.mark.parametrize("boundary", ["ALLOCATED", "ARCHIVE_INTENT"])
@pytest.mark.parametrize("damage", ["missing", "substituted", "claim", "extra-entry"])
def test_invalid_recorded_allocation_refuses_before_archival(case, modeled, boundary, damage):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    output, archive, journal_path = map(Path, (request["output"], request["archive"], request["journal"]))
    original = replacement._tree(output)

    def stop(point):
        if point == boundary:
            raise RuntimeError("pre-archive interruption")

    with pytest.raises(RuntimeError, match="pre-archive interruption"):
        replacement.execute(request, seal, plan["preview_sha256"], failpoint=stop)
    journal_bytes = journal_path.read_bytes()
    container = Path(json.loads(journal_bytes)["container"])
    claim = container / ".graphify_replacement_allocation.json"
    retained = container.with_name("retained-allocation")
    if damage in {"missing", "substituted"}:
        container.rename(retained)
        if damage == "substituted":
            container.mkdir()
            claim.write_bytes((retained / claim.name).read_bytes())
    elif damage == "claim":
        claim.write_bytes(b"{}")
    else:
        (container / "unclaimed").write_bytes(b"retain this foreign entry")
    selected_before = replacement._tree(container) if container.exists() else None
    retained_before = replacement._tree(retained) if retained.exists() else None

    with pytest.raises(tx.PendingTransactionError):
        replacement.execute(request, seal, plan["preview_sha256"])

    assert output.is_dir()
    with replacement._operation(journal_path, replacement._sha(replacement._bytes(request))):
        assert replacement._tree(output) == original
    assert not os.path.lexists(archive)
    assert journal_path.read_bytes() == journal_bytes
    assert (replacement._tree(container) if container.exists() else None) == selected_before
    assert (replacement._tree(retained) if retained.exists() else None) == retained_before


@pytest.mark.parametrize("damage", ["mode", "acl"])
@pytest.mark.parametrize("existing_journal", [False, True])
def test_unsafe_control_custody_refuses_before_journal_and_guards(
        case, modeled, monkeypatch, damage, existing_journal):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    output, archive, journal = map(Path, (request["output"], request["archive"], request["journal"]))
    if existing_journal:
        journal.write_bytes(replacement._bytes(_prepared(request, plan)))
    before_journal = journal.read_bytes() if existing_journal else None
    if damage == "mode":
        journal.parent.chmod(0o775)
    else:
        _deny_acl(monkeypatch, journal.parent)
    before_mode = stat.S_IMODE(journal.parent.stat().st_mode)
    original = replacement._tree(output)

    with pytest.raises(tx.PendingTransactionError):
        replacement.execute(request, seal, plan["preview_sha256"])

    assert (journal.read_bytes() if journal.exists() else None) == before_journal
    assert not (output.parent / replacement.GUARD).exists()
    assert not (archive.parent / replacement.GUARD).exists()
    assert not os.path.lexists(archive)
    assert replacement._tree(output) == original
    assert stat.S_IMODE(journal.parent.stat().st_mode) == before_mode


@pytest.mark.parametrize("damage", ["mode", "acl"])
def test_existing_journal_custody_is_authenticated_before_guards(case, modeled, monkeypatch, damage):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    output, archive, journal = map(Path, (request["output"], request["archive"], request["journal"]))
    journal.write_bytes(replacement._bytes(_prepared(request, plan)))
    if damage == "mode":
        journal.chmod(0o666)
    else:
        _deny_acl(monkeypatch, journal)
    before = journal.read_bytes(), stat.S_IMODE(journal.stat().st_mode), replacement._tree(output)

    with pytest.raises(tx.PendingTransactionError):
        replacement.execute(request, seal, plan["preview_sha256"])

    assert not (output.parent / replacement.GUARD).exists()
    assert not (archive.parent / replacement.GUARD).exists()
    assert not os.path.lexists(archive)
    assert (journal.read_bytes(), stat.S_IMODE(journal.stat().st_mode), replacement._tree(output)) == before
