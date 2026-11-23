"""Focused admission controls for canonical replacement authority."""
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx


@pytest.fixture
def registration(tmp_path):
    import json

    parent = tmp_path / 'registered'
    parent.mkdir()
    journal = tmp_path / 'journal.json'
    digest = 'a' * 64
    journal.write_text(json.dumps({'request_digest': digest, 'state': 'COMMITTED'}))
    marker = parent / replacement.GUARD
    marker.write_text(json.dumps({'schema': 1, 'entries': {
        'graphify-out': {'journal': str(journal), 'digest': digest, 'role': 'target'},
    }}))
    return parent, marker, journal


@pytest.mark.parametrize('change', ['missing-role', 'unknown-role', 'non-string-role',
                                    'null-entry', 'list-entry', 'non-object-journal'])
@pytest.mark.parametrize('authority', [False, True])
def test_malformed_selected_registration_refuses_before_creation(registration, change, authority):
    import json

    parent, marker, journal = registration
    record = json.loads(marker.read_text())
    entry = record['entries']['graphify-out']
    if change == 'missing-role':
        del entry['role']
    elif change == 'unknown-role':
        entry['role'] = 'canonical'
    elif change == 'non-string-role':
        entry['role'] = ['archive']
    elif change == 'null-entry':
        record['entries']['graphify-out'] = None
    elif change == 'list-entry':
        record['entries']['graphify-out'] = []
    else:
        journal.write_text('[]')
    marker.write_text(json.dumps(record))
    before = marker.read_bytes(), journal.read_bytes()
    target = parent / 'graphify-out' / 'new-child'
    token = replacement._AUTHORITY.set((str(journal), entry['digest'])) if authority else None
    try:
        with pytest.raises(tx.PendingTransactionError):
            with tx.pin_output(target, create=True):
                pass
    finally:
        if token is not None:
            replacement._AUTHORITY.reset(token)
    assert (marker.read_bytes(), journal.read_bytes()) == before
    assert not target.exists()


@pytest.mark.parametrize('failure', [tx.PendingTransactionError, KeyboardInterrupt])
def test_pin_output_closes_directory_after_late_admission_failure(tmp_path, monkeypatch, failure):
    import contextlib
    import os

    target = tmp_path / 'output'
    target.mkdir()
    opened = []
    real_open = os.open
    calls = 0

    def observe(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path) == target:
            opened.append(fd)
        return fd

    def admit(_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure('late replacement registration')

    monkeypatch.setattr(os, 'open', observe)
    monkeypatch.setattr(tx, 'require_ordinary_output', admit)
    with pytest.raises(failure, match='late replacement registration'):
        tx.pin_output(target)
    assert len(opened) == 1
    try:
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        with contextlib.suppress(OSError):
            os.close(opened[0])


def test_guard_marker_reads_are_relative_to_the_pinned_parent(registration, monkeypatch):
    import os

    parent, marker, _journal = registration
    identity = parent.stat()
    opened = []
    real_open = os.open

    def observe(path, flags, *args, **kwargs):
        if path == replacement.GUARD:
            info = os.fstat(kwargs['dir_fd'])
            assert (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)
            assert flags & os.O_NOFOLLOW
            opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', observe)
    monkeypatch.setattr(os.path, 'lexists', lambda *_: pytest.fail('unpinned pathname probe'))
    before = marker.read_bytes()
    replacement.guard_output(parent / 'graphify-out' / 'absent' / 'graph.json')
    assert opened
    assert marker.read_bytes() == before
    assert not (parent / 'graphify-out').exists()


def test_guard_parent_retarget_during_marker_lookup_refuses(registration, monkeypatch):
    import os

    parent, marker, _journal = registration
    identity = parent.stat()
    retained = parent.with_name('retained-parent')
    real_stat = os.stat
    swapped = False

    def retarget(path, *args, **kwargs):
        nonlocal swapped
        if path == replacement.GUARD and kwargs.get('dir_fd') is not None and not swapped:
            info = os.fstat(kwargs['dir_fd'])
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                parent.rename(retained)
                parent.mkdir()
                (parent / replacement.GUARD).write_text('{"schema":1,"entries":{}}')
                swapped = True
        return real_stat(path, *args, **kwargs)

    before = marker.read_bytes()
    monkeypatch.setattr(os, 'stat', retarget)
    with pytest.raises(tx.PendingTransactionError, match='directory identity changed'):
        replacement.guard_output(parent / 'graphify-out')
    assert swapped
    assert (retained / replacement.GUARD).read_bytes() == before
    assert not (parent / 'graphify-out').exists()


@pytest.mark.parametrize('change', ['replace', 'unlink', 'rewrite'])
def test_guard_marker_change_after_read_refuses(registration, monkeypatch, change):
    import os

    parent, marker, _journal = registration
    real_load = replacement._load
    changed = False

    def alter(path, *, parent=None):
        nonlocal changed
        value = real_load(path, parent=parent)
        if path == replacement.GUARD and parent is not None and not changed:
            if change == 'replace':
                candidate = marker.with_name('replacement-marker')
                candidate.write_bytes(marker.read_bytes())
                os.replace(candidate, marker)
            elif change == 'unlink':
                marker.unlink()
            else:
                marker.write_text('{"schema":1,"entries":{}}')
            changed = True
        return value

    monkeypatch.setattr(replacement, '_load', alter)
    with pytest.raises(tx.PendingTransactionError, match='replacement guard changed'):
        replacement.guard_output(parent / 'graphify-out')
    assert changed
    assert not (parent / 'graphify-out').exists()


def test_guard_alias_mapping_cannot_change_before_ancestor_admission(registration, tmp_path, monkeypatch):
    parent, _marker, _journal = registration
    alternate = tmp_path / 'alternate'
    alternate.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(parent, target_is_directory=True)
    real_guard = replacement._guard_path
    changed = False

    def retarget(path, **kwargs):
        nonlocal changed
        if not changed:
            alias.unlink()
            alias.symlink_to(alternate, target_is_directory=True)
            changed = True
        return real_guard(path, **kwargs)

    monkeypatch.setattr(replacement, '_guard_path', retarget)
    with pytest.raises(tx.PendingTransactionError, match='replacement guard path changed'):
        replacement.guard_output(alias / 'graphify-out')
    assert changed


@pytest.mark.parametrize('kind', ['directory', 'symlink', 'hardlink'])
def test_unsafe_guard_entries_refuse(registration, kind):
    import os

    parent, marker, _journal = registration
    original = marker.read_bytes()
    marker.unlink()
    if kind == 'directory':
        marker.mkdir()
    else:
        target = marker.with_name('other-marker')
        target.write_bytes(original)
        if kind == 'symlink':
            marker.symlink_to(target)
        else:
            os.link(target, marker)
    with pytest.raises(tx.PendingTransactionError):
        replacement.guard_output(parent / 'graphify-out')
    assert not (parent / 'graphify-out').exists()


@pytest.mark.parametrize('mutation', [False, True])
def test_pin_guard_runs_before_creation(tmp_path, monkeypatch, mutation):
    output = tmp_path / 'graphify-out'
    def refuse(path, *, mutation=True):
        raise tx.PendingTransactionError('replacement is pending')
    monkeypatch.setattr(replacement, 'guard_output', refuse)
    with pytest.raises(tx.PendingTransactionError, match='replacement is pending'):
        tx.pin_output(output, create=mutation, mutation=mutation)
    assert not output.exists()


def test_ordinary_writer_obeys_replacement_guard(tmp_path, monkeypatch):
    from graphify.storage_guard import require_ordinary_output
    def refuse(path, *, mutation=True):
        raise tx.PendingTransactionError('replacement is pending')
    monkeypatch.setattr(replacement, 'guard_output', refuse)
    with pytest.raises(tx.PendingTransactionError, match='replacement is pending'):
        require_ordinary_output(tmp_path / 'graphify-out' / 'cache')


def test_absent_snapshot_guard(tmp_path, monkeypatch):
    def refuse(path, *, mutation=True):
        if Path(path).name == 'graphify-out':
            raise tx.PendingTransactionError('replacement is pending')
    monkeypatch.setattr(replacement, 'guard_output', refuse)
    with pytest.raises(tx.PendingTransactionError, match='replacement is pending'):
        tx.open_graph_snapshot(tmp_path / 'graphify-out' / 'graph.json', purpose='test', allow_absent=True)
    assert not (tmp_path / 'graphify-out').exists()


from tests.test_canonical_replacement import case, modeled  # noqa: E402,F401


def test_guard_is_rechecked_after_lock_wait(tmp_path, monkeypatch):
    output = tmp_path / 'graphify-out'
    output.mkdir()
    with tx.pin_output(output) as cap:
        def refuse(path, *, mutation=True):
            raise tx.PendingTransactionError('replacement is pending')
        monkeypatch.setattr(replacement, 'guard_output', refuse)
        with pytest.raises(tx.PendingTransactionError, match='replacement is pending'):
            with tx._locked(cap):
                pytest.fail('guard must run under acquired lock')


def test_absent_creation_rechecks_guard_under_parent_lock(tmp_path, monkeypatch):
    output = tmp_path / 'graphify-out'
    checked = 0
    def guard(path, *, mutation=True):
        nonlocal checked
        if Path(path) == output:
            checked += 1
            if checked >= 2:
                raise tx.PendingTransactionError('replacement began while waiting')
    monkeypatch.setattr(replacement, 'guard_output', guard)
    with pytest.raises(tx.PendingTransactionError, match='replacement began while waiting'):
        tx._create_output_chain(output)
    assert not output.exists()
