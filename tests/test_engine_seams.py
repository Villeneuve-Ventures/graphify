"""Opt-in engine observation, output isolation, and ordinary parity."""
import io
import json
from pathlib import Path

import networkx as nx
import pytest


def test_scoped_reads_detect_drift_and_negative_probe(tmp_path):
    from graphify.source_io import SourceIO, SourceChanged
    source = tmp_path / 'main.py'
    source.write_text('x = 1\n')
    with SourceIO(tmp_path) as inputs:
        assert inputs.read_bytes(source) == b'x = 1\n'
        assert inputs.probe(tmp_path / 'tsconfig.json') is None
        source.write_text('x = 2\n')
        with pytest.raises(SourceChanged):
            inputs.read_bytes(source)


def test_scoped_extract_relative_ids_and_outcomes(tmp_path):
    from graphify.source_io import SourceIO
    from graphify.extract import extract
    path = tmp_path / 'main.py'
    path.write_text('def run():\n    pass\n')
    ordinary = extract([path], cache_root=tmp_path, strict=True, parallel=False)
    with SourceIO(tmp_path) as inputs:
        result = extract([path], source_root=tmp_path, source_io=inputs, quiet=True)
        assert result['nodes'] == ordinary['nodes']
        assert result['edges'] == ordinary['edges']
        assert result['outcomes'][0]['status'] == 'success'
        assert any(e['path'] == 'main.py' and e['operation'] == 'read' for e in inputs.evidence)
    assert not (tmp_path / 'graphify-out').exists()


def test_stream_serializer_parity(tmp_path):
    from graphify.export import to_json, write_json
    graph = nx.DiGraph()
    graph.add_node('x', label='é')
    stream = io.StringIO()
    write_json(graph, {}, stream, built_at_commit='abc')
    path = tmp_path / 'graph.json'
    assert to_json(graph, {}, str(path), built_at_commit='abc')
    assert path.read_text() == stream.getvalue()


def test_metadata_and_absent_probes_are_consumed(tmp_path):
    from graphify.source_io import SourceIO, SourceChanged
    from graphify.extract import extract
    source = tmp_path / 'app.ts'
    source.write_text("import {value} from '@lib'; console.log(value);")
    config = tmp_path / 'tsconfig.json'
    config.write_text('{"compilerOptions":{"paths":{"@lib":["lib.ts"]}}}')
    (tmp_path / 'lib.ts').write_text('export const value = 1;')
    with SourceIO(tmp_path) as inputs:
        extract([source], source_io=inputs, quiet=True)
        records = {(r['operation'], r['path']) for r in inputs.evidence}
        assert ('read', 'tsconfig.json') in records
        assert ('probe', 'lib.ts') in records
        config.write_text('{"compilerOptions":{}}')
        with pytest.raises(SourceChanged):
            inputs.read_bytes(config)
    with SourceIO(tmp_path) as inputs:
        assert inputs.probe(tmp_path / 'missing.json') is None
        (tmp_path / 'missing.json').write_text('{}')
        with pytest.raises(SourceChanged):
            inputs.probe(tmp_path / 'missing.json')


def test_detection_original_inputs_no_ambient_outputs(tmp_path, monkeypatch, capsys):
    from graphify.source_io import SourceIO, SourceChanged
    from graphify.detect import detect
    (tmp_path / 'doc.docx').write_bytes(b'original office bytes')
    (tmp_path / 'a.py').write_text('pass')
    (tmp_path / 'graphify-out').mkdir()
    (tmp_path / 'graphify-out/memory').mkdir()
    (tmp_path / 'graphify-out/memory/ambient.md').write_text('ambient')
    monkeypatch.setenv('GRAPHIFY_TRANSACTION_OUTPUT', '/untrusted/missing')
    monkeypatch.setenv('GRAPHIFY_GOOGLE_WORKSPACE', '1')
    monkeypatch.setattr('graphify.detect.convert_office_file', lambda *a, **k: pytest.fail('conversion'))
    monkeypatch.setattr('graphify.detect.convert_google_workspace_file', lambda *a, **k: pytest.fail('provider'))
    with SourceIO(tmp_path) as inputs:
        result = detect(tmp_path, source_io=inputs, quiet=True)
        assert result['files']['document'] == [str(tmp_path / 'doc.docx')]
        assert result['total_words'] == 0
        assert ('read', 'doc.docx') in {(r['operation'], r['path']) for r in inputs.evidence}
        assert not any('ambient.md' in r['path'] for r in inputs.evidence)
        (tmp_path / 'new.py').write_text('pass')
        with pytest.raises(SourceChanged):
            inputs.listdir(tmp_path)
    assert not (tmp_path / 'graphify-out/converted').exists()
    assert capsys.readouterr() == ('', '')


def test_scoped_refusals_and_poisoned_context(tmp_path, monkeypatch):
    from graphify.source_io import SourceIO, SourceUnsupported, engine_inputs
    from graphify.extract import extract, ExtractionIncomplete, collect_files
    for filename, content, expected in [('script.r', '', 'unsupported_extractor'),
                                        ('script.F90', 'program a\nend program', 'unsupported_input')]:
        path = tmp_path / filename
        path.write_text(content)
        with SourceIO(tmp_path) as inputs:
            with pytest.raises(ExtractionIncomplete) as exc:
                extract([path], source_io=inputs, quiet=True)
            assert exc.value.outcomes[0]['status'] == expected
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(SourceUnsupported), engine_inputs(inputs):
            collect_files(tmp_path)
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(SourceUnsupported):
            inputs.probe(tmp_path.parent / 'outside')
        with pytest.raises(SourceUnsupported):
            inputs.probe(tmp_path / 'script.r')


def test_scoped_valid_empty_missing_parser_and_unadapted_callback(tmp_path, monkeypatch):
    from graphify import extract as extraction
    from graphify.source_io import SourceIO
    path = tmp_path / 'empty.md'
    path.write_text('')
    with SourceIO(tmp_path) as inputs:
        result = extraction.extract([path], source_io=inputs, quiet=True)
        assert result['outcomes'][0]['status'] in {'empty', 'success'}
    path = tmp_path / 'empty.sql'
    path.write_text('')
    import sys
    monkeypatch.setitem(sys.modules, 'tree_sitter_sql', None)
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(extraction.ExtractionIncomplete) as exc:
            extraction.extract([path], source_io=inputs, quiet=True)
        assert exc.value.outcomes[0]['status'] == 'missing_parser'
    monkeypatch.setitem(extraction._DISPATCH, '.sql', lambda p: pytest.fail('unadapted callback'))
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(extraction.ExtractionIncomplete, match='unadapted'):
            extraction.extract([path], source_io=inputs, quiet=True)


def test_failed_read_and_partial_directory_are_not_empty(tmp_path, monkeypatch):
    from graphify.source_io import SourceIO, SourceUnavailable
    from graphify.extract import extract, ExtractionIncomplete
    from graphify.detect import detect
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(ExtractionIncomplete) as exc:
            extract([tmp_path / 'missing.py'], source_io=inputs, quiet=True)
        assert exc.value.outcomes[0]['status'] == 'failed_read'
    import os
    original = os.scandir
    class BrokenScan:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self):
            raise OSError('injected partial enumeration')
            yield
    monkeypatch.setattr(os, 'scandir', lambda path: BrokenScan() if isinstance(path, int) else original(path))
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(SourceUnavailable, match='partial enumeration'):
            detect(tmp_path, source_io=inputs, quiet=True)


@pytest.mark.parametrize('name,content', [
    ('main.cjs', 'const x = require("./lib.cjs"); module.exports = x;'),
    ('main.sql', 'CREATE TABLE users (id INTEGER);'),
    ('main.json', '{"name": "example", "nested": {"key": 2}}'),
    ('main.php', '<?php function answer() { return 42; }'),
])
def test_scoped_language_parity(tmp_path, name, content):
    from graphify.source_io import SourceIO
    from graphify.extract import extract
    path = tmp_path / name
    path.write_text(content)
    ordinary = extract([path], cache_root=tmp_path, strict=True, parallel=False)
    with SourceIO(tmp_path) as inputs:
        scoped = extract([path], source_io=inputs, quiet=True)
        assert scoped['nodes'] == ordinary['nodes']
        assert scoped['edges'] == ordinary['edges']
        assert any(r['operation'] == 'read' and r['path'] == name for r in inputs.evidence)


def test_xaml_supporting_inputs_and_membership(tmp_path):
    from graphify.source_io import SourceIO
    from graphify.extract import extract
    from tests.test_strict_extraction_completeness import _xaml_fixture
    view, companion, vm = _xaml_fixture(tmp_path)
    ordinary = extract([view], cache_root=tmp_path, strict=True, parallel=False)
    with SourceIO(tmp_path) as inputs:
        scoped = extract([view], source_io=inputs, quiet=True)
        assert scoped['nodes'] == ordinary['nodes']
        assert scoped['edges'] == ordinary['edges']
        read_paths = {r['path'] for r in inputs.evidence if r['operation'] == 'read'}
        assert companion.relative_to(tmp_path).as_posix() in read_paths
        assert vm.relative_to(tmp_path).as_posix() in read_paths
        assert any(r['operation'] == 'list' for r in inputs.evidence)


_COLD_QUERY = r'''
import sys, os, json
sys.path[:0] = json.loads(sys.argv[1])
mode = sys.argv[2]
blocked = []
def audit(event, args):
    mutation = event in {'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.chmod',
                         'os.link', 'os.symlink', 'os.truncate', 'os.utime'}
    if event == 'open':
        path, mode_arg, flags = args
        mutation = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if isinstance(path, (str, bytes)) and 'jieba.cache' in os.fsdecode(path):
            raise AssertionError('ambient cache read or write: ' + os.fsdecode(path))
    if mutation or event in {'subprocess.Popen', 'socket.connect', 'socket.bind'}:
        blocked.append(event)
        raise AssertionError('unexpected side effect: ' + event)
if mode != 'ordinary':
    sys.addaudithook(audit)
if mode == 'fallback':
    class NoJieba:
        def find_spec(self, fullname, *args):
            if fullname == 'jieba' or fullname.startswith('jieba.'):
                raise ModuleNotFoundError('optional jieba unavailable')
    sys.meta_path.insert(0, NoJieba())
from graphify.serve import memory_query_segmenter, _query_terms, _query_graph_text, _jieba
import networkx as nx
before = None if _jieba is None else (_jieba.dt.initialized, id(_jieba.dt.FREQ), _jieba.dt.total)
segmenter = None if mode == 'ordinary' else memory_query_segmenter()
graph = nx.DiGraph()
for i, label in enumerate(['南京市长江大桥', '南京市', '长江大桥', '自然语言处理', '机器学习', '缓存管理']):
    graph.add_node(str(i), label=label, file_type='code', source_file='example.py')
graph.add_edge('0', '1', relation='uses')
questions = ['南京市长江大桥', '自然语言处理和机器学习', '缓存管理 how does it work', '杭研大厦', 'foo中国bar']
result = {'tokens': [_query_terms(q, segmenter=segmenter) for q in questions],
          'ranking': [_query_graph_text(graph, q, segmenter=segmenter) for q in questions]}
if mode != 'ordinary':
    after = None if _jieba is None else (_jieba.dt.initialized, id(_jieba.dt.FREQ), _jieba.dt.total)
    assert before == after
    assert not blocked, blocked
    assert sys.dont_write_bytecode
print(json.dumps(result, ensure_ascii=False))
'''


def test_cold_chinese_tokens_ranking_and_no_transient_writes(tmp_path):
    import os
    import subprocess
    import sys
    import sysconfig
    import marshal
    paths = json.dumps([str(Path(__file__).resolve().parents[1]), sysconfig.get_path('purelib')])
    def run(mode, directory):
        directory.mkdir()
        env = {**os.environ, 'HOME': str(directory), 'TMPDIR': str(directory),
               'XDG_CACHE_HOME': str(directory), 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONHASHSEED': '0'}
        process = subprocess.run([sys.executable, '-B', '-S', '-c', _COLD_QUERY, paths, mode],
                                 env=env, capture_output=True, text=True, cwd=directory)
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout)
    ordinary = run('ordinary', tmp_path / 'ordinary')
    memory = run('memory', tmp_path / 'memory')
    assert ordinary == memory
    # A valid, deliberately wrong marshal cache is stronger than corrupt bytes:
    # the ordinary initializer accepts it without dictionary validation.
    misleading = tmp_path / 'misleading'
    misleading.mkdir()
    (misleading / 'jieba.cache').write_bytes(marshal.dumps(({'南京市长江大桥': 999}, 999)))
    env = {**os.environ, 'HOME': str(misleading), 'TMPDIR': str(misleading),
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONHASHSEED': '0'}
    process = subprocess.run([sys.executable, '-B', '-S', '-c', _COLD_QUERY, paths, 'memory'],
                             env=env, capture_output=True, text=True, cwd=misleading)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == ordinary
    fallback = run('fallback', tmp_path / 'fallback')
    assert fallback['tokens'][0] == ['南京', '京市', '市长', '长江', '江大', '大桥', '南京市长江大桥']


def test_source_descriptor_bounds_symlinks_and_root_rebinding(tmp_path):
    from graphify.source_io import SourceIO, SourceError, SourceChanged
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'a').write_text('abcdef')
    (root / 'link').symlink_to(root / 'a')
    with SourceIO(root) as inputs:
        with pytest.raises(SourceError, match='symlink'):
            inputs.read_bytes(root / 'link')
    with SourceIO(root, max_file_bytes=3) as inputs:
        with pytest.raises(SourceError, match='byte limit'):
            inputs.read_bytes(root / 'a')
    with SourceIO(root) as inputs:
        inputs.read_bytes(root / 'a')
        root.rename(tmp_path / 'old')
        root.mkdir()
        (root / 'a').write_text('abcdef')
        with pytest.raises(SourceChanged, match='root binding'):
            inputs.read_bytes(root / 'a')


def test_native_operational_recognition_uses_scoped_evidence(tmp_path, monkeypatch):
    from graphify.source_io import SourceIO
    from graphify.detect import detect
    from graphify import transaction as tx
    from tests.test_detect import _native_detection_fixture
    source = tmp_path / 'source.py'
    source.write_text('def source(): return 1\n')
    # Fixture writes happen before the observation; native recognition is reused.
    previous = tx._AUTHORITY.set(None)
    try:
        _native_detection_fixture(tmp_path, finalize=True)
        with SourceIO(tmp_path) as inputs:
            result = detect(tmp_path, source_io=inputs, quiet=True)
            assert result['walk_errors'] == []
            assert result['files']['code'] == [str(source)]
            assert any(r['operation'] == 'read' and 'owner' in r['path'] for r in inputs.evidence)
    finally:
        tx._AUTHORITY.reset(previous)


def test_linked_git_metadata_requires_explicit_allowlist(tmp_path):
    from graphify.source_io import SourceIO, SourceUnsupported
    from graphify.detect import detect
    root = tmp_path / 'source'
    git = tmp_path / 'git'
    root.mkdir()
    (git / 'info').mkdir(parents=True)
    (root / '.git').write_text(f'gitdir: {git}\n')
    (git / 'info/exclude').write_text('ignored.py\n')
    (root / 'ignored.py').write_text('pass')
    (root / 'kept.py').write_text('pass')
    with SourceIO(root) as inputs:
        with pytest.raises(SourceUnsupported, match='outside'):
            detect(root, source_io=inputs, quiet=True)
    with SourceIO(root, extra_roots={'git': git}) as inputs:
        result = detect(root, source_io=inputs, quiet=True)
        assert result['files']['code'] == [str(root / 'kept.py')]
        assert any(r['path'] == 'git:info/exclude' and r['operation'] == 'read' for r in inputs.evidence)


def test_workspace_package_listing_and_external_metadata_refusal(tmp_path):
    from graphify.source_io import SourceIO
    from graphify.extract import extract, ExtractionIncomplete
    (tmp_path / 'package.json').write_text('{"workspaces":["packages/*"]}')
    (tmp_path / 'packages/lib').mkdir(parents=True)
    (tmp_path / 'packages/lib/package.json').write_text('{"name":"@demo/lib","exports":"./index.ts"}')
    (tmp_path / 'packages/lib/index.ts').write_text('export const value = 1')
    source = tmp_path / 'app.ts'
    source.write_text("import { value } from '@demo/lib'; console.log(value)")
    with SourceIO(tmp_path) as inputs:
        extract([source], source_io=inputs, quiet=True)
        pairs = {(r['operation'], r['path']) for r in inputs.evidence}
        assert ('read', 'packages/lib/package.json') in pairs
        assert ('list', 'packages') in pairs
        assert ('probe', 'tsconfig.json') in pairs
    (tmp_path / 'tsconfig.json').write_text('{"extends":"../external.json"}')
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(ExtractionIncomplete, match='outside declared roots'):
            extract([source], source_io=inputs, quiet=True)


def test_all_builtin_dispatches_use_source_helpers(tmp_path, monkeypatch):
    """Inventory every registered built-in, not only representative fixtures.

    Empty/minimal payloads need not parse in every language. Every reachable read
    must use scoped descriptors; unsupported/error inputs must not report success.
    Rich resolver behavior is covered separately by the supporting-input fixtures.
    """
    from graphify import extract as extraction
    from graphify.source_io import SourceIO
    import subprocess
    paths = [tmp_path / ('input' + suffix) for suffix in extraction._DISPATCH]
    paths += [tmp_path / 'apm.yml', tmp_path / '.mcp.json', tmp_path / 'runner',
              tmp_path / 'input.blade.php', tmp_path / 'pyproject.toml']
    for path in paths:
        path.write_text('#!/usr/bin/env python\n' if path.name == 'runner' else '{}')
    def raw_io(path, *args, **kwargs):
        raise AssertionError('unadapted Path I/O: ' + str(path))
    for method in ('read_bytes', 'read_text', 'open', 'stat', 'resolve', 'iterdir', 'glob', 'rglob',
                   'exists', 'is_file', 'is_dir'):
        monkeypatch.setattr(Path, method, raw_io)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('external subprocess'))
    for path in paths:
        with SourceIO(tmp_path) as inputs:
            try:
                result = extraction.extract([path], source_io=inputs, quiet=True)
            except extraction.ExtractionIncomplete as exc:
                assert all('unadapted Path I/O' not in item.get('detail', '') for item in exc.outcomes), path
            else:
                assert result['outcomes'][0]['status'] in {'success', 'empty'}, path
                assert any(r['operation'] == 'read' and r['path'] == path.name for r in inputs.evidence), path


def test_valid_empty_json_has_explicit_successful_disposition(tmp_path):
    from graphify.source_io import SourceIO
    from graphify.extract import extract
    path = tmp_path / 'fixture.json'
    path.write_text('{"prices": [[1, 2]]}')
    with SourceIO(tmp_path) as inputs:
        result = extract([path], source_io=inputs, quiet=True)
        assert not result['nodes'] and not result['edges']
        assert result['outcomes'][0]['status'] == 'empty'


def test_cache_destination_does_not_change_ids(tmp_path):
    from graphify.extract import extract
    root = tmp_path / 'source'
    root.mkdir()
    path = root / 'main.py'
    path.write_text('def answer(): return 42')
    first = extract([path], source_root=root, cache_root=tmp_path / 'cache-one', parallel=False)
    second = extract([path], source_root=root, cache_root=tmp_path / 'cache-two', parallel=False)
    assert first == second
    assert (tmp_path / 'cache-one/graphify-out/cache').exists()
    assert (tmp_path / 'cache-two/graphify-out/cache').exists()
    assert not (root / 'graphify-out').exists()


def test_directory_memberships_share_one_aggregate_budget(tmp_path):
    from graphify.source_io import SourceIO, SourceUnsupported
    for directory in ('first', 'second'):
        (tmp_path / directory).mkdir()
        for i in range(4):
            (tmp_path / directory / str(i)).touch()
    with SourceIO(tmp_path, max_entries=10) as inputs:
        assert len(inputs.listdir(tmp_path / 'first')) == 4
        # A repeat revalidates but doesn't consume a new retained-evidence budget.
        assert len(inputs.listdir(tmp_path / 'first')) == 4
        with pytest.raises(SourceUnsupported, match='entry limit'):
            inputs.listdir(tmp_path / 'second')
        members = sum(len(r['value'][1]) for r in inputs.evidence if r['operation'] == 'list')
        assert len(inputs.evidence) + members <= 10


def test_quiet_scoped_failure_suppresses_ambient_debug_traceback(tmp_path, monkeypatch, capsys):
    from graphify.source_io import SourceIO
    from graphify.extract import extract, ExtractionIncomplete
    monkeypatch.setenv('GRAPHIFY_DEBUG', '1')
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(ExtractionIncomplete):
            extract([tmp_path / 'missing.cls'], source_io=inputs, quiet=True)
    assert capsys.readouterr() == ('', '')


def test_listing_binding_must_match_subsequent_read(tmp_path):
    from graphify.source_io import SourceIO, SourceChanged
    source = tmp_path / 'a.py'
    source.write_text('pass')
    with SourceIO(tmp_path) as inputs:
        inputs.listdir(tmp_path)
        source.rename(tmp_path / 'old.py')
        source.write_text('pass')
        with pytest.raises(SourceChanged, match='binding changed'):
            inputs.read_bytes(source)


def test_cold_scoped_engine_and_stream_make_no_filesystem_or_provider_writes(tmp_path):
    import os
    import subprocess
    import sys
    import sysconfig
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'main.py').write_text('def answer(): return 42\n')
    (source / 'notes.docx').write_bytes(b'original office bytes')
    # Reuse the audit interceptor, installed before importing candidate modules.
    script = _COLD_QUERY.split("if mode == 'fallback':")[0] + r'''
from pathlib import Path
import io
from graphify.source_io import SourceIO
from graphify.detect import detect
from graphify.extract import extract
from graphify.build import build_from_json
from graphify.export import write_json
root = Path(sys.argv[3])
with SourceIO(root) as inputs:
    inventory = detect(root, source_io=inputs, quiet=True)
    extraction = extract(inventory['files']['code'], source_io=inputs, quiet=True)
    graph = build_from_json(extraction, directed=True, root=root)
    output = io.StringIO()
    write_json(graph, {}, output, built_at_commit='fixture')
    assert json.loads(output.getvalue())['nodes']
assert not blocked, blocked
assert sys.dont_write_bytecode
print('ENGINE-NO-WRITES')
'''
    paths = json.dumps([str(Path(__file__).resolve().parents[1]), sysconfig.get_path('purelib')])
    env = {**os.environ, 'HOME': str(tmp_path), 'TMPDIR': str(tmp_path),
           'PYTHONDONTWRITEBYTECODE': '1', 'GRAPHIFY_GOOGLE_WORKSPACE': '1',
           'GRAPHIFY_OUT': '/untrusted/output', 'GRAPHIFY_TRANSACTION_OUTPUT': '/untrusted/tx'}
    process = subprocess.run([sys.executable, '-B', '-S', '-c', script, paths, 'engine', str(source)],
                             cwd=tmp_path, env=env, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert 'ENGINE-NO-WRITES' in process.stdout


def test_scoped_registered_resolver_must_be_adapted(tmp_path, monkeypatch):
    from graphify.source_io import SourceIO
    from graphify.extract import extract, ExtractionIncomplete
    from graphify import resolver_registry as registry
    path = tmp_path / 'main.py'
    path.write_text('def answer(): return 42')
    monkeypatch.setattr(registry, '_REGISTRY', [registry.LanguageResolver(
        'external', frozenset({'.py'}), lambda *args: pytest.fail('unadapted resolver ran'))])
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(ExtractionIncomplete, match='unadapted registered resolver'):
            extract([path], source_io=inputs, quiet=True)


def test_extra_roots_normalize_after_absolute_validation(tmp_path):
    from graphify.source_io import SourceIO
    source = tmp_path / 'source'
    policy = tmp_path / 'policy'
    source.mkdir()
    policy.mkdir()
    config = policy / 'config.json'
    config.write_bytes(b'{}')
    with SourceIO(source, extra_roots={'policy': source / '..' / 'policy'}) as inputs:
        assert inputs.read_bytes(config) == b'{}'
        assert any(r['operation'] == 'read' and r['path'] == 'policy:config.json'
                   for r in inputs.evidence)
    with pytest.raises(ValueError, match='root allowlist'):
        SourceIO(source, extra_roots={'policy': Path('policy')})


@pytest.mark.parametrize('flag', ['O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK'])
def test_unsupported_descriptor_flags_refuse_before_open(tmp_path, monkeypatch, flag):
    from graphify.source_io import SourceIO, SourceUnsupported
    import os
    monkeypatch.delattr(os, flag)
    monkeypatch.setattr(os, 'open', lambda *a, **k: pytest.fail('opened before support check'))
    with pytest.raises(SourceUnsupported, match='descriptors unavailable'):
        with SourceIO(tmp_path):
            pytest.fail('unsupported input context opened')


def test_direct_scoped_xaml_uses_accounted_project_listing(tmp_path, monkeypatch):
    from graphify.source_io import SourceIO, engine_inputs
    from graphify.extract import extract_xaml
    from tests.test_strict_extraction_completeness import _xaml_fixture
    view, _, vm = _xaml_fixture(tmp_path)
    monkeypatch.setattr(Path, 'iterdir', lambda *a: pytest.fail('unaccounted enumeration'))
    with SourceIO(tmp_path) as inputs, engine_inputs(inputs):
        result = extract_xaml(view)
        assert any(n['source_file'] == str(vm) for n in result['nodes'])
        assert any(r['operation'] == 'list' and r['path'] == '.' for r in inputs.evidence)


@pytest.mark.parametrize('kind', ['json', 'mcp'])
def test_scoped_config_size_limit_applies_before_payload_read(tmp_path, kind):
    from graphify.source_io import SourceIO, SourceUnsupported, engine_inputs
    from graphify.extractors.json_config import extract_json
    from graphify.mcp_ingest import extract_mcp_config
    extractor = extract_json if kind == 'json' else extract_mcp_config
    path = tmp_path / ('config.json' if kind == 'json' else '.mcp.json')
    path.write_bytes(b' ' * (2 * 1024 * 1024))
    expected = 'json file too large to index' if kind == 'json' else 'mcp config too large to index'
    assert extractor(path)['error'] == expected
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(SourceUnsupported, match='byte limit'), engine_inputs(inputs):
            assert extractor(path)['error'] == expected
            assert inputs._bytes == 0
            assert not any(r['operation'] == 'read' for r in inputs.evidence)
        with pytest.raises(SourceUnsupported, match='byte limit'):
            inputs.check()


def test_per_read_limit_keeps_full_evidence_and_global_bounds(tmp_path):
    from graphify.source_io import SourceIO, SourceUnsupported
    path = tmp_path / 'small'
    path.write_bytes(b'1234')
    with SourceIO(tmp_path, max_file_bytes=8, max_total_bytes=8) as inputs:
        assert inputs.read_bytes(path, max_bytes=4) == b'1234'
        assert inputs.read_bytes(path) == b'1234'
        assert len([r for r in inputs.evidence if r['operation'] == 'read']) == 1
        with pytest.raises(SourceUnsupported, match='byte limit'):
            inputs.read_bytes(path, max_bytes=4)
    with SourceIO(tmp_path, max_file_bytes=3) as inputs:
        with pytest.raises(SourceUnsupported, match='byte limit'):
            inputs.read_bytes(path, max_bytes=8)
        assert inputs._bytes == 0


def test_scoped_walk_honors_mutable_pruning_and_detection_exclusions(tmp_path, monkeypatch):
    from graphify.source_io import SourceIO, source_walk, engine_inputs
    from graphify.detect import detect
    for name in ('kept', 'ignored', 'node_modules'):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'module.py').write_text('pass')
    (tmp_path / '.gitignore').write_text('ignored/\n')
    with SourceIO(tmp_path) as inputs, engine_inputs(inputs):
        walk = source_walk(tmp_path)
        _, directories, _ = next(walk)
        directories[:] = ['kept']
        assert [Path(item[0]).name for item in walk] == ['kept']
    with SourceIO(tmp_path) as inputs:
        listing = inputs.listdir
        def checked_listing(path):
            assert Path(path).name not in {'ignored', 'node_modules'}
            return listing(path)
        monkeypatch.setattr(inputs, 'listdir', checked_listing)
        result = detect(tmp_path, source_io=inputs, quiet=True)
        assert result['files']['code'] == [str(tmp_path / 'kept/module.py')]


def test_unqualified_jieba_refuses_only_memory_path(monkeypatch):
    from graphify import serve
    from types import SimpleNamespace
    monkeypatch.setattr(serve, '_jieba', SimpleNamespace(
        __version__='unqualified', cut=lambda text: ['南京', '市'],
        Tokenizer=lambda: pytest.fail('unqualified tokenizer initialized')))
    assert '南京' in serve._segment_chinese('南京市')
    with pytest.raises(RuntimeError, match='qualified jieba 0.42.1'):
        serve.memory_query_segmenter()
