"""Focused replacement repairs on disposable fixtures and modeled maintenance."""
from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


def test_registration_refuses_mutation_inside_retained_archive(tmp_path):
    archive = tmp_path / "prior-archive"
    archive.mkdir()
    (archive / "preserved").write_bytes(b"retained")
    journal = tmp_path / "prior-journal.json"
    journal.write_text(json.dumps({"request_digest": "prior", "state": "COMMITTED"}))
    replacement._register(archive, journal, "prior", "archive")
    before = replacement._tree(archive)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        replacement._register(archive / "next", tmp_path / "next-journal.json", "next", "archive")

    assert replacement._tree(archive) == before
    assert not (archive / replacement.GUARD).exists()


def test_registration_rechecks_mutation_after_waiting_for_parent_lock(tmp_path, monkeypatch):
    archive = tmp_path / "prior-archive"
    archive.mkdir()
    (archive / "preserved").write_bytes(b"retained")
    journal = tmp_path / "prior-journal.json"
    journal.write_text(json.dumps({"request_digest": "prior", "state": "COMMITTED"}))
    before = replacement._tree(archive)
    real_locks = replacement._fd_locks
    registered = []

    @contextmanager
    def register_while_waiting(paths, deadline):
        with real_locks(paths, deadline) as caps:
            if list(paths) == [archive]:
                replacement._register(archive, journal, "prior", "archive")
                registered.append(True)
            yield caps

    monkeypatch.setattr(replacement, "_fd_locks", register_while_waiting)
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        replacement._register(archive / "next", tmp_path / "next-journal.json", "next", "archive")

    assert registered == [True]
    assert replacement._tree(archive) == before
    assert not (archive / replacement.GUARD).exists()


@pytest.mark.parametrize("boundary", ["ALLOCATED", "OLD_ARCHIVED", "INSTALL_INTENT"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_preinstall_rollback_leaves_unused_quarantine_unregistered(case, modeled, boundary, interrupted):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)

    def stop_execution(point):
        if point == boundary:
            raise RuntimeError("execution interruption")

    with pytest.raises(RuntimeError, match="execution interruption"):
        replacement.execute(request, sealed, plan["preview_sha256"], failpoint=stop_execution)
    journal = replacement._load(request["journal"])
    expected = replacement._sha(replacement._bytes(journal))
    quarantine = Path(request["journal"]).parent / "unused-quarantine"

    def stop_rollback(point):
        if point == "after_rollback_restore":
            raise RuntimeError("rollback interruption")

    if interrupted and boundary != "ALLOCATED":
        with pytest.raises(RuntimeError, match="rollback interruption"):
            replacement.rollback(request, expected, quarantine, failpoint=stop_rollback)
    result = replacement.rollback(request, expected, quarantine)

    assert result["new_graph_retained"] == journal["container"]
    assert not quarantine.exists()
    guard = replacement._load(quarantine.parent / replacement.GUARD)
    assert quarantine.name not in guard["entries"]
    replacement.guard_output(quarantine / "new.json", mutation=True)
    with tx.pin_output(quarantine, create=True):
        pass
    assert quarantine.is_dir()


@pytest.mark.parametrize("boundary", [None, "after_rollback_quarantine", "after_rollback_restore"])
def test_installed_rollback_keeps_actual_quarantine_readonly(case, modeled, boundary):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)
    replacement.execute(request, sealed, plan["preview_sha256"])
    journal = replacement._load(request["journal"])
    expected = replacement._sha(replacement._bytes(journal))
    tree = replacement._tree(request["output"])
    quarantine = Path(request["journal"]).parent / "retained-new"

    def stop(point):
        if point == boundary:
            raise RuntimeError("rollback interruption")

    if boundary is not None:
        with pytest.raises(RuntimeError, match="rollback interruption"):
            replacement.rollback(request, expected, quarantine,
                                 expected_new_tree=replacement._sha(replacement._bytes(tree)), failpoint=stop)
    result = replacement.rollback(request, expected, quarantine,
                                  expected_new_tree=replacement._sha(replacement._bytes(tree)))

    assert result["new_graph_retained"] == str(quarantine)
    assert replacement._tree(quarantine) == tree
    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        replacement.guard_output(quarantine / "new.json", mutation=True)


@pytest.mark.parametrize("field", ["edges", "hyperedges"])
@pytest.mark.parametrize("value", [None, 3, "record", []])
def test_malformed_semantic_relationship_returns_structured_cli_refusal(case, tmp_path, capsys, field, value):
    request, fragments = case
    fragments[0][field] = [value]
    request_path, fragments_path = tmp_path / "request.json", tmp_path / "fragments.json"
    request_path.write_text(json.dumps(request))
    fragments_path.write_text(json.dumps(fragments))
    before = replacement._tree(request["output"])

    assert replacement.main(["build-data", "--request", str(request_path),
                             "--fragments", str(fragments_path)]) == 1

    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert "semantic relationship must be an object" in response["error"]
    assert replacement._tree(request["output"]) == before
    assert not Path(request["stage"]).exists()
    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()


@pytest.mark.parametrize("depth", [128, 129])
def test_preservation_depth_boundary_closes_opened_directories(tmp_path, monkeypatch, depth):
    root = tmp_path / "inventory"
    root.mkdir()
    path = root
    for _ in range(depth):
        path = path / "d"
        path.mkdir()
    (path / "held").write_bytes(b"retained")
    real_open, real_close = replacement.os.open, replacement.os.close
    opened = set()

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.add(fd)
        return fd

    def tracked_close(fd):
        opened.discard(fd)
        return real_close(fd)

    monkeypatch.setattr(replacement.os, "open", tracked_open)
    monkeypatch.setattr(replacement.os, "close", tracked_close)
    if depth == 128:
        inventory = replacement._tree(root)
        assert len(inventory) == depth + 2  # Root metadata plus child directories and file.
        assert inventory["/".join(["d"] * depth + ["held"])]["sha256"] == replacement._sha(b"retained")
    else:
        with pytest.raises(tx.PendingTransactionError, match="preservation directory depth bound exceeded"):
            replacement._tree(root)
    assert not opened


def test_cli_resume_without_journal_refuses_under_coordinator_lock(case, modeled, tmp_path, monkeypatch, capsys):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)
    Path(request["stage"]).write_text(json.dumps(sealed))
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    before = replacement._tree(request["output"])
    entered = []
    real_lock = replacement._coordinator_lock

    @contextmanager
    def tracked_lock(selected):
        with real_lock(selected):
            entered.append(True)
            yield

    monkeypatch.setattr(replacement, "_coordinator_lock", tracked_lock)
    assert replacement.main(["resume", "--request", str(request_path),
                             "--preview-sha256", plan["preview_sha256"]]) == 1
    response = json.loads(capsys.readouterr().out)
    assert entered == [True]
    assert response["status"] == "blocked"
    assert "existing replacement journal required" in response["error"]
    assert replacement._tree(request["output"]) == before
    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()
    assert not list(Path(request["journal"]).parent.glob(".replacement-container-*"))


def test_cli_resume_replays_existing_prepared_operation(case, modeled, tmp_path, capsys):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)
    journal = {**plan, "request_digest": replacement._sha(replacement._bytes(request)),
               "state": "PREPARED", "new_identity": None, "container": None, "owner": None}
    replacement._persist(Path(request["journal"]), journal)
    Path(request["stage"]).write_text(json.dumps(sealed))
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))

    assert replacement.main(["resume", "--request", str(request_path),
                             "--preview-sha256", plan["preview_sha256"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "complete"
    assert replacement.execute(request, sealed, plan["preview_sha256"]) == result
