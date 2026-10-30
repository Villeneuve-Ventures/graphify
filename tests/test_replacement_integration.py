"""Focused admission controls for canonical replacement authority."""
from pathlib import Path

import pytest

from graphify import canonical_replacement as replacement
from graphify import transaction as tx


def test_distinct_replacement_binding():
    value = 'canonical-replacement-v1:' + 'a' * 64
    assert tx._validate_replacement_binding(value) == value
    with pytest.raises(tx.PendingTransactionError):
        tx._validate_replacement_binding('checkpoint-restore-v1:' + 'a' * 64)


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


def test_exact_guard_namespace_excluded():
    from graphify.detect import _SKIP_FILES
    assert '.graphify_replacement_guard' in _SKIP_FILES
    assert '.graphify_replacement_guard_notes.md' not in _SKIP_FILES


def test_absent_snapshot_guard(tmp_path, monkeypatch):
    def refuse(path, *, mutation=True):
        if Path(path).name == 'graphify-out':
            raise tx.PendingTransactionError('replacement is pending')
    monkeypatch.setattr(replacement, 'guard_output', refuse)
    with pytest.raises(tx.PendingTransactionError, match='replacement is pending'):
        tx.open_graph_snapshot(tmp_path / 'graphify-out' / 'graph.json', purpose='test', allow_absent=True)
    assert not (tmp_path / 'graphify-out').exists()


def test_direct_manifest_guard(tmp_path, monkeypatch):
    from graphify.detect import save_manifest
    def refuse(path, *, mutation=True):
        raise tx.PendingTransactionError('replacement is pending')
    monkeypatch.setattr(replacement, 'guard_output', refuse)
    with pytest.raises(tx.PendingTransactionError, match='replacement is pending'):
        save_manifest({}, manifest_path=str(tmp_path / 'out' / 'manifest.json'))
    assert not (tmp_path / 'out').exists()


from tests.test_canonical_replacement import case, modeled  # noqa: E402,F401


def test_replacement_origin_survives_next_generation(case, modeled):
    import json
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    plan = replacement.preview(request, seal)
    replacement.execute(request, seal, plan['preview_sha256'])
    output = Path(request['output'])
    origin = (output / replacement.PROVENANCE).read_bytes()
    owner = tx.begin_transaction('full', request['root'], output=output)
    assert owner.generation == 2
    assert owner.replacement_binding == tx.REPLACEMENT_BINDING_PREFIX + __import__('hashlib').sha256(origin).hexdigest()
    context = tx._AUTHORITY.set(tx._authority_for(owner))
    try:
        graph = {'directed': True, 'multigraph': False,
                 'graph': {tx.GRAPH_WATERMARK_KEY: {'schema': 1, 'protocol_epoch': 1, 'generation': 2, 'state': 'active'}},
                 'nodes': [{'id': 'next'}], 'links': []}
        tx.commit_publication_plan(owner, tx.PublicationPlan({'graph.json': json.dumps(graph).encode(), 'manifest.json': b'{}'}))
        tx.finish_transaction(owner)
    finally:
        tx._AUTHORITY.reset(context)
    assert (output / replacement.PROVENANCE).read_bytes() == origin
    assert tx.open_graph_snapshot(output / 'graph.json', purpose='test').generation == 2


def test_old_protocol_parser_refuses_replacement(case, modeled):
    import ast
    import json
    import subprocess
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    replacement.execute(request, seal, replacement.preview(request, seal)['preview_sha256'])
    source = subprocess.check_output(['git', 'show', 'HEAD:graphify/transaction.py'], text=True)
    function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == '_protocol_from_json')
    namespace = vars(tx).copy()
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<base-protocol-parser>', 'exec'), namespace)
    with tx.pin_output(request['output'], mutation=False) as cap:
        protocol = json.loads((Path(request['output']) / tx.PROTOCOL_FILE).read_bytes())
        with pytest.raises(tx.PendingTransactionError, match='malformed protocol'):
            namespace['_protocol_from_json'](cap, protocol)


def test_archive_alias_remains_mutation_guarded(case, modeled, tmp_path):
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    replacement.execute(request, seal, replacement.preview(request, seal)['preview_sha256'])
    alias = tmp_path / 'archive-alias'
    alias.symlink_to(request['archive'], target_is_directory=True)
    with pytest.raises(tx.PendingTransactionError, match='archive'):
        replacement.guard_output(alias / 'new-cache.json', mutation=True)


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


def test_checkpoint_capture_refuses_replacement_lineage(case, modeled, monkeypatch):
    import contextlib
    from graphify import checkpoint_restore as recovery
    request, fragments = case
    seal = replacement.build_data(request, fragments)
    replacement.execute(request, seal, replacement.preview(request, seal)['preview_sha256'])
    before = replacement._tree(request['output'])

    @contextlib.contextmanager
    def source_only(output):
        with tx.pin_output(output, mutation=False) as cap:
            yield cap
        pytest.fail('capture accepted replacement lineage before authority-store access')

    monkeypatch.setattr(recovery, '_capture_source', source_only)
    with pytest.raises(tx.PendingTransactionError, match='closed ordinary output'):
        recovery.capture('a' * 64, request['root'], request['output'], 'absent')
    assert replacement._tree(request['output']) == before
