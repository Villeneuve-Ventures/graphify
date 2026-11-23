"""Bounded replay admission and retained-object reporting on fixture roots."""
import json
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case, modeled as modeled


@pytest.mark.parametrize("sealed", [[], None, 17, "seal", True])
def test_nonobject_seal_is_structured_preview_refusal(case, tmp_path, capsys, sealed):
    request, _ = case
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    Path(request["stage"]).write_text(json.dumps(sealed))
    before = replacement._tree(request["output"])

    assert replacement.main(["preview", "--request", str(request_path)]) == 1

    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "blocked"
    assert "seal" in response["error"]
    assert replacement._tree(request["output"]) == before
    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()
    assert Path(request["stage"]).read_text() == json.dumps(sealed)


def test_coordinator_lock_refuses_registered_archive_parent(case, tmp_path):
    request, _ = case
    archive = tmp_path / "prior-archive"
    archive.mkdir()
    (archive / "preserved").write_bytes(b"retained")
    prior_journal = tmp_path / "prior-journal.json"
    prior_journal.write_text(json.dumps({"request_digest": "prior", "state": "COMMITTED"}))
    replacement._register(archive, prior_journal, "prior", "archive")
    request["journal"] = str(archive / "next-journal.json")
    del request["parent_identities"]
    request = replacement.bind_request(request)
    before = replacement._tree(archive)

    with pytest.raises(tx.PendingTransactionError, match="archive is read-only"):
        with replacement._coordinator_lock(request):
            pytest.fail("archive parent admitted a mutating lock")

    assert replacement._tree(archive) == before
    assert not list(archive.glob(".replacement-lock-*"))


@pytest.mark.parametrize("boundary", ["OLD_ARCHIVED", "INSTALL_INTENT"])
@pytest.mark.parametrize("interrupt_rollback", [False, True])
def test_preinstall_rollback_reports_persisted_container(case, modeled, boundary, interrupt_rollback):
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
    retained = Path(journal["container"])
    retained_tree = replacement._tree(retained)
    quarantine = Path(request["journal"]).parent / "unused-quarantine"

    def stop_rollback(point):
        if point == "after_rollback_restore":
            raise RuntimeError("rollback interruption")

    if interrupt_rollback:
        with pytest.raises(RuntimeError, match="rollback interruption"):
            replacement.rollback(request, expected, quarantine, failpoint=stop_rollback)
    result = replacement.rollback(request, expected, quarantine)

    assert result == {"status": "rolled-back-unrecovered", "new_graph_retained": str(retained)}
    journal = replacement._load(request["journal"])
    assert journal["rollback_retained"] == str(retained)
    assert replacement._tree(retained) == retained_tree
    assert not quarantine.exists()
    assert (Path(request["output"]) / "graph.json").read_bytes() == b'{"old":true}'


def test_preview_refuses_json_escaped_inventory_over_budget(case, monkeypatch):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    record = {"device": 1, "inode": 100000, "mode": 33188, "sha256": "0" * 64}
    tree = {"\x01" * 240 + f"/f{number:05d}": dict(record, inode=100000 + number)
            for number in range(16)}
    monkeypatch.setattr(replacement, "_tree", lambda _: tree)
    monkeypatch.setattr(replacement, "MAX_BYTES", 16 * 1024)
    monkeypatch.setattr(replacement, "_FORWARD_JOURNAL_RESERVE", 128)

    with pytest.raises(tx.PendingTransactionError, match="bound"):
        replacement.preview(request, sealed)

    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()
    assert (Path(request["output"]) / "graph.json").read_bytes() == b'{"old":true}'


def test_exact_serialized_persist_limit_is_readable(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    value = {"state": "\x01" * 20}
    data = replacement._bytes(value)
    monkeypatch.setattr(replacement, "MAX_BYTES", len(data))

    replacement._persist(path, value)

    assert path.read_bytes() == data
    assert replacement._load(path) == value


@pytest.mark.parametrize("existing", [False, True])
def test_oversized_persist_preserves_existing_state(tmp_path, monkeypatch, existing):
    path = tmp_path / "state.json"
    before = b'{"state":"held"}\n'
    if existing:
        path.write_bytes(before)
    monkeypatch.setattr(replacement, "MAX_BYTES", 128)

    def unexpected_write(*args):
        pytest.fail("oversized metadata reached the state writer")

    monkeypatch.setattr(tx, "_replace_bytes", unexpected_write)

    with pytest.raises(tx.PendingTransactionError, match="bound"):
        replacement._persist(path, {"state": "\x01" * 128})

    assert path.read_bytes() == before if existing else not path.exists()


@pytest.mark.parametrize("remaining", [0, -1])
def test_preview_complete_journal_budget_exact_boundary(case, monkeypatch, remaining):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)
    initial = {**plan, "request_digest": replacement._sha(replacement._bytes(request)),
               "state": "PREPARED", "new_identity": None, "container": None, "owner": None}
    limit = len(replacement._bytes(initial)) + replacement._FORWARD_JOURNAL_RESERVE + remaining
    monkeypatch.setattr(replacement, "MAX_BYTES", limit)

    if remaining == 0:
        assert replacement.preview(request, sealed) == plan
    else:
        with pytest.raises(tx.PendingTransactionError, match="bound"):
            replacement.preview(request, sealed)
    assert not Path(request["journal"]).exists()


def test_initial_journal_growth_refuses_before_create(case, modeled, monkeypatch):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)
    monkeypatch.setattr(replacement, "preview", lambda *_: plan)
    monkeypatch.setattr(replacement, "MAX_BYTES", len(replacement._bytes(plan)))

    def unexpected_create(*args):
        pytest.fail("oversized initial journal reached the state writer")

    monkeypatch.setattr(tx, "_create_bytes", unexpected_create)
    with pytest.raises(tx.PendingTransactionError, match="bound"):
        replacement.execute(request, sealed, plan["preview_sha256"])

    assert not Path(request["journal"]).exists()
    assert not Path(request["archive"]).exists()
    assert (Path(request["output"]) / "graph.json").read_bytes() == b'{"old":true}'


@pytest.mark.parametrize("boundary", ["ALLOCATED", "OLD_ARCHIVED", "NEW_INSTALLED", "PUBLISHING"])
def test_forward_resume_reserves_remaining_growth_before_effects(case, modeled, monkeypatch, boundary):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)

    def stop(point):
        if point == boundary:
            raise RuntimeError("execution interruption")

    with pytest.raises(RuntimeError, match="execution interruption"):
        replacement.execute(request, sealed, plan["preview_sha256"], failpoint=stop)
    journal_path = Path(request["journal"])
    before = journal_path.read_bytes()
    journal = replacement._load(journal_path)
    identities = {field: replacement._identity_at(request[field]) for field in ("output", "archive")}
    monkeypatch.setattr(replacement, "MAX_BYTES", len(before))

    def unexpected_effect(*args):
        pytest.fail("underfunded replay reached a registration or namespace effect")

    monkeypatch.setattr(replacement, "_register", unexpected_effect)
    monkeypatch.setattr(replacement, "_rename", unexpected_effect)
    with pytest.raises(tx.PendingTransactionError, match="bound"):
        replacement.execute(request, sealed, plan["preview_sha256"])

    assert journal_path.read_bytes() == before
    assert replacement._load(journal_path) == journal
    assert {field: replacement._identity_at(request[field]) for field in identities} == identities


def test_guard_growth_refuses_without_rewriting_held_guard(tmp_path, monkeypatch):
    slot = tmp_path / "archive"
    journal = tmp_path / "journal.json"
    replacement._register(slot, journal, "digest", "archive")
    marker = tmp_path / replacement.GUARD
    before = marker.read_bytes()
    monkeypatch.setattr(replacement, "MAX_BYTES", len(before))

    with pytest.raises(tx.PendingTransactionError, match="bound"):
        replacement._register(tmp_path / "second", journal, "digest", "archive")

    assert marker.read_bytes() == before


def test_unreadable_guard_is_not_rewritten(tmp_path, monkeypatch):
    marker = tmp_path / replacement.GUARD
    before = replacement._bytes({"schema": 1, "entries": {"held": {"note": "x" * 256}}})
    marker.write_bytes(before)
    monkeypatch.setattr(replacement, "MAX_BYTES", 128)

    with pytest.raises(tx.PendingTransactionError, match="oversized"):
        replacement._register(tmp_path / "next", tmp_path / "journal.json", "digest", "archive")

    assert marker.read_bytes() == before


def test_oversized_stage_refuses_before_exclusive_creation(case, monkeypatch):
    request, _ = case
    monkeypatch.setattr(replacement, "MAX_BYTES", 128)

    with pytest.raises(tx.PendingTransactionError, match="bound"):
        replacement._seal_data(request, {"payloads": "x" * 128})

    assert not Path(request["stage"]).exists()


def test_rollback_metadata_growth_refuses_before_namespace_change(case, modeled, monkeypatch):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)
    replacement.execute(request, sealed, plan["preview_sha256"])
    journal_path = Path(request["journal"])
    before = journal_path.read_bytes()
    journal = replacement._load(journal_path)
    current = replacement._tree(request["output"])
    quarantine = journal_path.parent / "retained-new"
    monkeypatch.setattr(replacement, "MAX_BYTES", len(before))

    def unexpected_effect(*args):
        pytest.fail("oversized rollback reached registration or rename")

    monkeypatch.setattr(replacement, "_register", unexpected_effect)
    monkeypatch.setattr(replacement, "_rename", unexpected_effect)

    with pytest.raises(tx.PendingTransactionError, match="bound"):
        replacement.rollback(request, replacement._sha(before), quarantine,
                             expected_new_tree=replacement._sha(replacement._bytes(current)))

    assert journal_path.read_bytes() == before
    assert replacement._tree(request["output"]) == current
    assert replacement._tree(request["archive"]) == journal["old_tree"]
    assert not quarantine.exists()


def test_rollback_resume_reserves_longer_terminal_state(case, modeled, monkeypatch):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)
    replacement.execute(request, sealed, plan["preview_sha256"])
    journal_path = Path(request["journal"])
    journal = replacement._load(journal_path)
    expected = replacement._sha(replacement._bytes(journal))
    quarantine = journal_path.parent / "retained-new"
    current = replacement._tree(request["output"])

    def stop(point):
        if point == "ROLLBACK_INTENT":
            raise RuntimeError("rollback interruption")

    with pytest.raises(RuntimeError, match="rollback interruption"):
        replacement.rollback(request, expected, quarantine, failpoint=stop,
                             expected_new_tree=replacement._sha(replacement._bytes(current)))
    before = journal_path.read_bytes()
    monkeypatch.setattr(replacement, "MAX_BYTES", len(before))

    def unexpected_effect(*args):
        pytest.fail("underfunded rollback resume reached registration or rename")

    monkeypatch.setattr(replacement, "_register", unexpected_effect)
    monkeypatch.setattr(replacement, "_rename", unexpected_effect)
    with pytest.raises(tx.PendingTransactionError, match="bound"):
        replacement.rollback(request, expected, quarantine)

    assert journal_path.read_bytes() == before
    with replacement._operation(journal_path, journal["request_digest"]):
        assert replacement._tree(request["output"]) == current
    assert not quarantine.exists()


@pytest.mark.parametrize("command", ["status", "execute", "rollback"])
def test_unreadable_journal_is_not_rewritten(case, modeled, monkeypatch, command):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)
    replacement.execute(request, sealed, plan["preview_sha256"])
    journal_path = Path(request["journal"])
    before = journal_path.read_bytes()
    monkeypatch.setattr(replacement, "MAX_BYTES", len(before) - 1)

    with pytest.raises(tx.PendingTransactionError, match="oversized"):
        if command == "status":
            replacement.status(request)
        elif command == "execute":
            replacement.execute(request, sealed, plan["preview_sha256"])
        else:
            replacement.rollback(request, replacement._sha(before), journal_path.parent / "retained-new")

    assert journal_path.read_bytes() == before


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("changed", ["missing", "replaced"])
def test_preinstall_rollback_holds_changed_retained_allocation(case, modeled, monkeypatch, resume, changed):
    request, fragments = case
    sealed = replacement.build_data(request, fragments)
    plan = replacement.preview(request, sealed)

    def stop_execution(point):
        if point == "OLD_ARCHIVED":
            raise RuntimeError("execution interruption")

    with pytest.raises(RuntimeError, match="execution interruption"):
        replacement.execute(request, sealed, plan["preview_sha256"], failpoint=stop_execution)
    journal_path = Path(request["journal"])
    journal = replacement._load(journal_path)
    expected = replacement._sha(replacement._bytes(journal))
    quarantine = journal_path.parent / "unused-quarantine"

    def stop_rollback(point):
        if point == "after_rollback_restore":
            raise RuntimeError("rollback interruption")

    if resume:
        with pytest.raises(RuntimeError, match="rollback interruption"):
            replacement.rollback(request, expected, quarantine, failpoint=stop_rollback)
    retained = Path(journal["container"])
    retained.rename(retained.with_name("held-allocation"))
    if changed == "replaced":
        retained.mkdir()
    before = journal_path.read_bytes()
    identities = {field: replacement._identity_at(request[field]) for field in ("output", "archive")}

    def unexpected_effect(*args):
        pytest.fail("changed retained allocation reached a registration or rename")

    monkeypatch.setattr(replacement, "_register", unexpected_effect)
    monkeypatch.setattr(replacement, "_rename", unexpected_effect)
    with pytest.raises(tx.PendingTransactionError, match="retained replacement allocation changed"):
        replacement.rollback(request, expected, quarantine)

    assert journal_path.read_bytes() == before
    assert {field: replacement._identity_at(request[field]) for field in identities} == identities
