"""Regression checks for replacement refusal before coordinator effects."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx
from tests.test_canonical_replacement import case as case

pytestmark = pytest.mark.skipif(os.name != "posix", reason="descriptor-relative replacement requires POSIX")


def test_cli_reports_json_decoder_recursion(tmp_path, monkeypatch, capsys):
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")

    def overflow(*args, **kwargs):
        raise RecursionError("simulated JSON decoder stack exhaustion")

    with monkeypatch.context() as patch:
        patch.setattr(replacement.json, "loads", overflow)
        result = replacement.main(["bind-request", "--request", str(request)])

    assert result == 1
    captured = capsys.readouterr()
    assert not captured.err
    assert json.loads(captured.out) == {
        "status": "blocked", "error": "replacement JSON nesting exceeds decoder bound",
    }


def test_guard_journal_reports_json_decoder_recursion(tmp_path, monkeypatch):
    journal = tmp_path.resolve() / "journal.json"
    journal.write_text("{}", encoding="utf-8")

    def overflow(*args, **kwargs):
        raise RecursionError("simulated JSON decoder stack exhaustion")

    with monkeypatch.context() as patch:
        patch.setattr(replacement.json, "loads", overflow)
        with pytest.raises(tx.PendingTransactionError, match="JSON nesting exceeds decoder bound"):
            with replacement._guard_journal(str(journal), frozenset({os.getuid()})):
                pytest.fail("the guard must refuse before yielding a decoded journal")


@pytest.mark.parametrize("late", [False, True], ids=["present", "during-admission"])
def test_register_honors_recovery_backup_inside_held_parent(tmp_path, monkeypatch, late):
    from graphify import replacement_guard_issuers as issuers

    parent = tmp_path.resolve()
    slot = parent / "archive"
    backup = parent / (f".{replacement.GUARD}.graphify-merge-backup." + "a" * 32)

    def write_backup():
        backup.write_bytes(b"displaced predecessor")

    if not late:
        write_backup()
    policy = SimpleNamespace(
        trusted_issuers=lambda *args, **kwargs: frozenset({os.getuid()}),
        validate=write_backup if late else lambda: None,
    )
    monkeypatch.setattr(issuers, "issuer_snapshot", lambda: contextlib.nullcontext(policy))

    with pytest.raises(tx.PendingTransactionError, match="explicit recovery"):
        replacement._register(slot, parent / "journal.json", "digest", "archive")

    assert backup.read_bytes() == b"displaced predecessor"
    assert not (parent / replacement.GUARD).exists()
    assert not slot.exists()


@pytest.mark.parametrize("field", ["archive", "archive-upper", "stage", "quarantine"])
def test_refuse_evidence_path_equal_to_coordinator_lock(case, field):
    request, _ = case
    journal = Path(request["journal"])
    lock = journal.with_name(".replacement-lock-" + hashlib.sha256(os.fsencode(journal.name)).hexdigest())

    with pytest.raises(tx.PendingTransactionError, match="coordinator lock"):
        if field == "quarantine":
            replacement.rollback(request, "0" * 64, lock)
        elif field == "archive-upper":
            replacement.bind_request({**request, "archive": str(lock.with_name(lock.name.upper()))})
        else:
            replacement.bind_request({**request, field: str(lock)})

    assert not lock.exists()
    assert not journal.exists()
