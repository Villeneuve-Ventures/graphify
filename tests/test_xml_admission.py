"""Five XML readers share declaration rejection and bounded, complete reads."""
from __future__ import annotations

import builtins
import hashlib
import io
import os
from pathlib import Path
import socket
import urllib.request
import xml.etree.ElementTree as ET

import pytest

from graphify.extract import (
    ExtractionIncomplete, _PROJECT_XML_MAX_BYTES, extract, extract_csproj,
    extract_lazarus_package, extract_slnx, extract_xaml,
)
from graphify.manifest_ingest import _MAX_MANIFEST_BYTES, extract_package_manifest
from graphify.source_io import (
    SourceChanged, SourceIO, SourceTooLarge, SourceUnsupported, engine_inputs,
)
from graphify.xml_admission import XMLDeclarationError, parse_xml


CASES = [
    pytest.param('app.lpk', extract_lazarus_package,
                 '<CONFIG><Package><Name Value="Café"/><RequiredPkgs><Item1>'
                 '<PackageName Value="Peer"/></Item1></RequiredPkgs></Package></CONFIG>',
                 _PROJECT_XML_MAX_BYTES, 'package file too large', id='lazarus'),
    pytest.param('app.slnx', extract_slnx,
                 '<Solution><Project Path="Café.csproj"/></Solution>',
                 _PROJECT_XML_MAX_BYTES, 'project file too large', id='slnx'),
    pytest.param('app.csproj', extract_csproj,
                 '<Project><PropertyGroup><TargetFramework>Café</TargetFramework></PropertyGroup>'
                 '<ItemGroup><PackageReference Include="Peer" Version="1.0"/></ItemGroup></Project>',
                 _PROJECT_XML_MAX_BYTES, 'project file too large', id='csproj'),
    pytest.param('app.xaml', extract_xaml,
                 '<Window><Button Name="Café"/></Window>',
                 _PROJECT_XML_MAX_BYTES, 'xaml file too large', id='xaml'),
    pytest.param('pom.xml', extract_package_manifest,
                 '<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>Café</artifactId>'
                 '<dependencies><dependency><artifactId>Peer</artifactId></dependency>'
                 '</dependencies></project>',
                 _MAX_MANIFEST_BYTES, 'manifest too large to index', id='maven'),
]
ENCODINGS = ['utf-8', 'utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be',
             'utf-16-le-bom', 'utf-16-be-bom', 'iso-8859-1']


def _encode(xml, encoding):
    if encoding.endswith('-bom'):
        codec = encoding.removesuffix('-bom')
        bom = b'\xff\xfe' if codec.endswith('le') else b'\xfe\xff'
        return bom + xml.encode(codec)
    declaration = {'utf-8-sig': 'UTF-8', 'utf-16-le': 'UTF-16LE',
                   'utf-16-be': 'UTF-16BE'}.get(encoding, encoding)
    return (f'<?xml version="1.0" encoding="{declaration}"?>\n' + xml).encode(encoding)


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def _protected(root):
    for name in ['graphify-out/graph.json', 'graphify-out/manifest.json',
                 'graphify-out/.graphify_receipt.json', 'graphify-out/cache/retained.json',
                 '.git/config', '.graphify/workspace.json']:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'retained state\n')


def _read_record(inputs, path, raw):
    records = [r for r in inputs.evidence if r['operation'] == 'read' and r['path'] == path.name]
    assert len(records) == 1
    identity, digest = records[0]['value']
    assert identity[3] == len(raw)
    assert digest == hashlib.sha256(raw).hexdigest()
    assert inputs._bytes == len(raw)


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
@pytest.mark.parametrize('encoding', ENCODINGS)
def test_healthy_encodings_preserve_output_and_evidence(tmp_path, name, reader, xml, cap, error,
                                                       encoding):
    path = tmp_path / name
    raw = _encode(xml, encoding)
    path.write_bytes(raw)
    _protected(tmp_path)
    before = _snapshot(tmp_path)
    result = reader(path)
    if name == 'pom.xml' and encoding not in {'utf-8', 'utf-8-sig'}:
        # Maven still decodes UTF-8 text. Do not add raw-byte XML encoding support.
        if encoding == 'iso-8859-1':
            assert result['nodes'][0]['label'] == 'Caf�'
        elif encoding in {'utf-16-le', 'utf-16-be'}:
            # The old lossy text path can parse these but cannot find artifactId.
            assert result == {'nodes': [], 'edges': []}
        else:
            assert result['nodes'] == result['edges'] == [] and result['error']
    else:
        assert 'error' not in result
        assert 'Café' in {n['label'] for n in result['nodes']}
        assert result['edges']
    with SourceIO(tmp_path) as inputs, engine_inputs(inputs):
        assert reader(path) == result
        _read_record(inputs, path, raw)
        assert inputs.failure is None
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
@pytest.mark.parametrize('encoding', ENCODINGS)
@pytest.mark.parametrize('declaration', [
    '<!DOCTYPE root>',
    '<!DOCTYPE root [<!ENTITY expansion "expanded">]>',
    '<!DOCTYPE root [<!ENTITY % parameter "expanded">%parameter;]>',
    '<!DOCTYPE root [<!ENTITY malformed "unterminated]>',
    '<!ENTITY expansion "expanded">',
])
def test_declarations_never_produce_facts(tmp_path, name, reader, xml, cap, error, encoding,
                                         declaration):
    path = tmp_path / name
    raw = _encode(declaration + xml.replace('Café', '&expansion;'), encoding)
    path.write_bytes(raw)
    _protected(tmp_path)
    before = _snapshot(tmp_path)
    result = reader(path)
    assert result['nodes'] == result['edges'] == []
    assert result['error']
    if declaration.startswith('<!DOCTYPE') and (
        name != 'pom.xml' or encoding in {'utf-8', 'utf-8-sig', 'iso-8859-1'}
    ):
        assert 'refusing XML with DOCTYPE/ENTITY declaration' in result['error']
    with pytest.raises(RuntimeError, match='AST extraction incomplete'):
        extract([path], strict=True, parallel=False, ambient_output=False, quiet=True)
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(ExtractionIncomplete) as caught:
            extract([path], source_io=inputs, ambient_output=False, quiet=True)
        assert caught.value.outcomes == [
            {'path': str(path), 'status': 'failed_extraction', 'detail': result['error']}
        ]
        _read_record(inputs, path, raw)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize('encoding', ENCODINGS)
def test_callback_stops_before_tree_or_internal_subset(encoding, monkeypatch):
    from graphify import xml_admission
    calls = []

    class Observed(xml_admission._RejectingTreeBuilder):
        def start(self, tag, attrs):
            calls.append(tag)
            return super().start(tag, attrs)

    monkeypatch.setattr(xml_admission, '_RejectingTreeBuilder', Observed)
    with pytest.raises(XMLDeclarationError, match='DOCTYPE/ENTITY'):
        parse_xml(_encode('<!DOCTYPE root [<!ENTITY malformed "unterminated]><root/>', encoding))
    assert calls == []


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
def test_declaration_text_is_content_not_markup(tmp_path, name, reader, xml, cap, error):
    path = tmp_path / name
    path.write_text(xml, encoding='utf-8')
    expected = reader(path)
    harmless = xml.replace('>', '><!-- <!DOCTYPE root [<!ENTITY x "ok">]> -->', 1)
    head, tail = harmless.rsplit('</', 1)
    harmless = head + '<![CDATA[<!DOCTYPE root><!ENTITY x "ok">]]></' + tail
    path.write_text(harmless, encoding='utf-8')
    assert reader(path) == expected
    assert parse_xml('<root>&lt;!DOCTYPE root&gt;&lt;!ENTITY x "ok"&gt;</root>').text


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
@pytest.mark.parametrize('extra', [0, 1])
def test_exact_caps_bound_read_requests_and_keep_complete_evidence(tmp_path, monkeypatch, name,
                                                                 reader, xml, cap, error, extra):
    path = tmp_path / name
    raw = xml.encode('utf-8')
    raw += b' ' * (cap + extra - len(raw))
    path.write_bytes(raw)
    requests = []
    original = Path.open

    class Observed:
        def __enter__(self):
            self.stream = original(path, 'rb')
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size):
            requests.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(Path, 'open', lambda p, *a, **kw: Observed() if p == path
                        else original(p, *a, **kw))
    result = reader(path)
    assert requests == [cap + 1]
    if extra:
        assert result == {'nodes': [], 'edges': [], 'error': error}
    else:
        assert result['nodes'] and 'error' not in result
    with SourceIO(tmp_path) as inputs:
        if extra:
            with pytest.raises(SourceTooLarge, match='byte limit'), engine_inputs(inputs):
                assert reader(path) == result
                assert inputs._bytes == 0
                assert not any(r['operation'] == 'read' for r in inputs.evidence)
                assert any(r['operation'] == 'probe' and r['path'] == name for r in inputs.evidence)
            with pytest.raises(SourceTooLarge):
                inputs.check()
        else:
            with engine_inputs(inputs):
                assert reader(path) == result
                _read_record(inputs, path, raw)


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
@pytest.mark.parametrize('limit', ['file', 'total'])
def test_global_source_limits_are_not_widened(tmp_path, name, reader, xml, cap, error, limit):
    path = tmp_path / name
    raw = xml.encode('utf-8')
    path.write_bytes(raw)
    bounds = {f'max_{limit}_bytes': len(raw) - 1}
    with SourceIO(tmp_path, **bounds) as inputs:
        with pytest.raises(ExtractionIncomplete) as caught:
            extract([path], source_io=inputs, ambient_output=False, quiet=True)
        assert caught.value.outcomes[0]['status'] == 'unsupported_input'
        assert caught.value.failure['status'] == 'unsupported_input'
        assert inputs._bytes == 0
        assert not any(r['operation'] == 'read' for r in inputs.evidence)
        with pytest.raises(SourceUnsupported):
            inputs.check()


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
def test_consumed_bytes_still_count_against_aggregate_limit(tmp_path, name, reader, xml, cap, error):
    path = tmp_path / name
    raw = xml.encode('utf-8')
    path.write_bytes(raw)
    with SourceIO(tmp_path, max_total_bytes=len(raw) * 2 - 1) as inputs:
        with engine_inputs(inputs):
            assert reader(path)['nodes']
        _read_record(inputs, path, raw)
        evidence = inputs.evidence
        with pytest.raises(ExtractionIncomplete) as caught:
            extract([path], source_io=inputs, ambient_output=False, quiet=True)
        assert caught.value.outcomes[0]['status'] == 'unsupported_input'
        assert inputs.evidence == evidence
        _read_record(inputs, path, raw)
        with pytest.raises(SourceUnsupported):
            inputs.check()


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
def test_ordinary_growth_cannot_return_a_valid_prefix(tmp_path, monkeypatch, name, reader, xml,
                                                     cap, error):
    path = tmp_path / name
    path.write_text(xml, encoding='utf-8')
    _protected(tmp_path)
    before = _snapshot(tmp_path)
    original = Path.open
    requests = []

    class Growing:
        def __enter__(self):
            with original(path, 'ab') as writer:
                writer.write(b' ' * cap)
            self.stream = original(path, 'rb')
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size):
            requests.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(Path, 'open', lambda p, *a, **kw: Growing() if p == path
                        else original(p, *a, **kw))
    assert reader(path) == {'nodes': [], 'edges': [], 'error': error}
    assert requests == [cap + 1]
    monkeypatch.setattr(Path, 'open', original)
    after = _snapshot(tmp_path)
    assert after.keys() == before.keys()
    assert {k: v for k, v in after.items() if k != name} == {
        k: v for k, v in before.items() if k != name}


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
@pytest.mark.parametrize('mutation', ['growth', 'rewrite', 'rebind'])
def test_scoped_mutation_latches_failure_and_withholds_evidence(tmp_path, monkeypatch, name,
                                                               reader, xml, cap, error, mutation):
    path = tmp_path / name
    raw = xml.encode('utf-8')
    path.write_bytes(raw)
    _protected(tmp_path)
    before = _snapshot(tmp_path)
    original_read = os.read
    requests = []

    def mutate(fd, size):
        if not requests:
            if mutation == 'growth':
                with path.open('ab') as stream:
                    stream.write(b' ' * cap)
            elif mutation == 'rewrite':
                path.write_bytes(raw.replace(b'Peer', b'Other') if b'Peer' in raw
                                 else raw.replace(b'Caf', b'New'))
            else:
                replacement = tmp_path / 'replacement'
                replacement.write_bytes(raw)
                os.replace(replacement, path)
        requests.append(size)
        return original_read(fd, size)

    monkeypatch.setattr(os, 'read', mutate)
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(ExtractionIncomplete) as caught:
            extract([path], source_io=inputs, ambient_output=False, quiet=True)
        status = 'unsupported_input' if mutation == 'growth' else 'inconsistent_input'
        assert caught.value.outcomes[0]['status'] == status
        assert caught.value.failure['status'] == status
        assert not any(r['operation'] == 'read' for r in inputs.evidence)
        assert inputs._bytes <= cap + 1
        assert requests and all(0 < size <= min(65536, cap + 1) for size in requests)
        with pytest.raises(SourceTooLarge if mutation == 'growth' else SourceChanged):
            inputs.check()
    after = _snapshot(tmp_path)
    assert after.keys() == before.keys()
    assert {k: v for k, v in after.items() if k != name} == {
        k: v for k, v in before.items() if k != name}


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
def test_repeated_read_mutation_retains_original_evidence(tmp_path, name, reader, xml, cap, error):
    path = tmp_path / name
    raw = xml.encode('utf-8')
    path.write_bytes(raw)
    with SourceIO(tmp_path) as inputs:
        with engine_inputs(inputs):
            assert reader(path)['nodes']
        evidence = inputs.evidence
        path.write_bytes(raw + b' ')
        with pytest.raises(ExtractionIncomplete) as caught:
            extract([path], source_io=inputs, ambient_output=False, quiet=True)
        assert caught.value.outcomes[0]['status'] == 'inconsistent_input'
        assert inputs.evidence == evidence
        _read_record(inputs, path, raw)
        with pytest.raises(SourceChanged):
            inputs.check()


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
def test_scoped_failure_keeps_completed_and_unprocessed_outcomes(tmp_path, name, reader, xml,
                                                               cap, error):
    good, bad, later = tmp_path / 'good.slnx', tmp_path / name, tmp_path / 'later.slnx'
    good.write_text('<Solution/>')
    bad.write_bytes(b' ' * (cap + 1))
    later.write_text('<Solution/>')
    _protected(tmp_path)
    before = _snapshot(tmp_path)
    with SourceIO(tmp_path) as inputs:
        with pytest.raises(ExtractionIncomplete) as caught:
            extract([good, bad, later], source_io=inputs, ambient_output=False, quiet=True)
        assert [r['status'] for r in caught.value.outcomes] == [
            'success', 'unsupported_input', 'not_processed']
        assert [r['path'] for r in caught.value.outcomes] == list(map(str, [good, bad, later]))
        assert {r['path'] for r in inputs.evidence if r['operation'] == 'read'} == {'good.slnx'}
        with pytest.raises(SourceTooLarge):
            inputs.check()
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize('name,reader,xml,cap,error', CASES)
@pytest.mark.parametrize('external', ['file', 'network', 'entity', 'parameter'])
def test_external_declarations_never_access_targets_or_change_state(tmp_path, monkeypatch, name,
                                                                  reader, xml, cap, error, external):
    source = tmp_path / 'source'
    source.mkdir()
    target = tmp_path / 'secret.dtd'
    target.write_text('<!ENTITY expansion "private">')
    uri = target.as_uri() if external == 'file' else 'http://xml.invalid/secret.dtd'
    if external in {'file', 'network'}:
        declaration = f'<!DOCTYPE root SYSTEM "{uri}">'
    elif external == 'entity':
        declaration = f'<!DOCTYPE root [<!ENTITY expansion SYSTEM "{target.as_uri()}">]>'
    else:
        declaration = f'<!DOCTYPE root [<!ENTITY % external SYSTEM "{uri}">%external;]>'
    path = source / name
    raw = _encode(declaration + xml.replace('Café', '&expansion;'), 'utf-8')
    path.write_bytes(raw)
    _protected(source)
    before = _snapshot(tmp_path)
    calls = []

    def guarded(original):
        def invoke(file, *args, **kwargs):
            if not isinstance(file, int) and 'secret.dtd' in os.fsdecode(file):
                calls.append(('file', str(file)))
                pytest.fail('external file accessed')
            return original(file, *args, **kwargs)
        return invoke

    def network(*args, **kwargs):
        calls.append(('network', args))
        pytest.fail('external network accessed')

    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        patch.setattr(os, 'open', guarded(os.open))
        patch.setattr(urllib.request, 'urlopen', network)
        patch.setattr(socket, 'create_connection', network)
        patch.setattr(socket.socket, 'connect', network)
        patch.setattr(socket.socket, 'connect_ex', network)
        result = reader(path)
        assert result['nodes'] == result['edges'] == []
        assert 'DOCTYPE/ENTITY' in result['error']
        with pytest.raises(RuntimeError, match='AST extraction incomplete'):
            extract([path], strict=True, parallel=False, ambient_output=False, quiet=True)
        with SourceIO(source) as inputs:
            with pytest.raises(ExtractionIncomplete) as caught:
                extract([path], source_io=inputs, ambient_output=False, quiet=True)
            assert caught.value.outcomes[0]['status'] == 'failed_extraction'
            _read_record(inputs, path, raw)
            assert not any('secret.dtd' in r['path'] for r in inputs.evidence)
        assert calls == []
    assert _snapshot(tmp_path) == before
