"""Security receipts must distinguish complete scans from tolerated failures."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tools import security_receipts as security


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="graphifyy"\nversion="0.10.0"\n')
    (tmp_path / 'uv.lock').write_text('''[[package]]
name="graphifyy"
version="0.10.0"
source={editable="."}
[[package]]
name="example-pkg"
version="1.0"
source={registry="https://pypi.org/simple"}
[[package]]
name="pip-audit"
version="2.10.0"
source={registry="https://pypi.org/simple"}
''')
    return tmp_path


def installed(root):
    return [{'name': 'graphifyy', 'version': '0.10.0',
             'direct_url': {'url': root.as_uri(), 'dir_info': {'editable': True}}},
            {'name': 'example-pkg', 'version': '1.0', 'direct_url': None}]


def inventory(root, rows=None, exported='Example.Pkg==1.0\n'):
    return security.inventory(root, exported, rows or installed(root),
                              {'sys_platform': 'linux', 'os_name': 'posix'})


def pip_payload(findings=False):
    return {'dependencies': [{'name': 'example-pkg', 'version': '1.0',
                             'vulns': [{'id': 'CVE-example', 'fix_versions': ['1.1']}]
                             if findings else []}], 'fixes': []}


COVERAGE = {'third_party': [{'name': 'example-pkg', 'version': '1.0'}]}


def bandit_payload(findings=False):
    return {'errors': [], 'metrics': {
        'graphify/a.py': {'loc': 2, 'SEVERITY.MEDIUM': int(findings), 'SEVERITY.HIGH': 0},
        '_totals': {'loc': 2, 'SEVERITY.MEDIUM': int(findings), 'SEVERITY.HIGH': 0}},
        'results': [{'filename': 'graphify/a.py', 'issue_severity': 'MEDIUM',
                     'issue_confidence': 'HIGH', 'test_id': 'B101', 'line_number': 1,
                     'issue_text': 'Example rule alert'}] if findings else []}


def test_complete_inventory_excludes_only_proven_local_root(project):
    result = inventory(project)
    assert result['third_party'] == COVERAGE['third_party']
    assert len(result['installed']) == 2
    assert result['excluded_project']['name'] == 'graphifyy'
    assert 'local root' in result['excluded_project']['reason']


def test_export_markers_select_only_audited_platform(project):
    result = inventory(project, exported="example-pkg==1.0\nwindows-only==2 ; sys_platform == 'win32'\n")
    assert result['third_party'] == COVERAGE['third_party']


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'duplicate', 'wrong-version', 'editable',
                                      'wrong-root', 'no-provenance', 'wrong-root-version'])
def test_inventory_rejects_omissions_or_unproven_entries(project, mutation):
    rows = installed(project)
    if mutation == 'missing':
        rows.pop()
    elif mutation == 'extra':
        rows.append({'name': 'extra', 'version': '1', 'direct_url': None})
    elif mutation == 'duplicate':
        rows.append(rows[-1].copy())
    elif mutation == 'wrong-version':
        rows[-1]['version'] = '2'
    elif mutation == 'editable':
        rows[-1]['direct_url'] = rows[0]['direct_url']
    elif mutation == 'wrong-root':
        rows[0]['direct_url']['url'] = project.parent.as_uri()
    elif mutation == 'no-provenance':
        rows[0]['direct_url'] = None
    else:
        rows[0]['version'] = '2'
    with pytest.raises(ValueError):
        inventory(project, rows)


@pytest.mark.parametrize('exported', ['example-pkg>=1', 'example-pkg==1.*',
                                      'example-pkg @ https://example.test/pkg.whl',
                                      'example-pkg==1.0\nexample_pkg==1.0', ''])
def test_inventory_rejects_unauditable_export(project, exported):
    with pytest.raises(ValueError):
        inventory(project, exported=exported)


def test_inventory_requires_matching_registry_lock(project):
    p = project / 'uv.lock'
    p.write_text(p.read_text().replace('https://pypi.org/simple', 'https://example.test/simple'))
    with pytest.raises(ValueError, match='registry lock'):
        inventory(project)


@pytest.mark.parametrize('scanner', ['pip-audit', 'bandit'])
@pytest.mark.parametrize('findings', [False, True])
def test_complete_clean_and_findings_are_distinct(scanner, findings):
    payload = pip_payload(findings) if scanner == 'pip-audit' else bandit_payload(findings)
    coverage = COVERAGE if scanner == 'pip-audit' else {'graphify/a.py': 'sha256'}
    result = security.audit_result(scanner, payload, int(findings), '', coverage)
    assert result['completion'] == 'complete'
    assert result['result'] == ('findings' if findings else 'clean')
    assert result['finding_count'] == int(findings)


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'duplicate', 'skipped', 'malformed',
                                      'wrong-version', 'bad-vuln'])
def test_dependency_result_requires_complete_valid_inventory(mutation):
    payload = pip_payload()
    deps = payload['dependencies']
    if mutation == 'missing':
        deps.clear()
    elif mutation == 'extra':
        deps.append({'name': 'extra', 'version': '1', 'vulns': []})
    elif mutation == 'duplicate':
        deps.append(copy.deepcopy(deps[0]))
    elif mutation == 'skipped':
        deps[0] = {'name': 'example-pkg', 'skip_reason': 'Dependency not found'}
    elif mutation == 'malformed':
        deps[0]['vulns'] = None
    elif mutation == 'wrong-version':
        deps[0]['version'] = '2'
    else:
        deps[0]['vulns'] = [{}]
    assert security.audit_result('pip-audit', payload, 0, '', COVERAGE)['result'] == 'incomplete'


@pytest.mark.parametrize('payload,code,stderr', [
    (None, 0, ''), (pip_payload(), 1, ''),
    (pip_payload(), 2, ''), (pip_payload(), 0, 'ERROR: advisory service unavailable'),
    (pip_payload(True), 0, '')])
def test_missing_malformed_network_and_exit_failures_are_incomplete(payload, code, stderr):
    result = security.audit_result('pip-audit', payload, code, stderr, COVERAGE)
    assert result['completion'] == 'incomplete'
    if payload is None:
        assert result['finding_count'] is None
    if payload == pip_payload(True):
        assert result['finding_count'] == 1


def test_bandit_errors_coverage_and_internal_failures_cannot_be_clean():
    for mutation in ('errors', 'coverage', 'metrics', 'omitted-finding', 'stderr'):
        payload = bandit_payload()
        stderr = ''
        if mutation == 'errors':
            payload['errors'] = [{'filename': 'graphify/a.py', 'reason': 'syntax error'}]
        elif mutation == 'coverage':
            del payload['metrics']['graphify/a.py']
        elif mutation == 'metrics':
            payload['metrics']['_totals']['loc'] = 0
        elif mutation == 'omitted-finding':
            payload['metrics']['_totals']['SEVERITY.HIGH'] = 1
        else:
            stderr = '[tester] ERROR Bandit internal error running plugin'
        result = security.audit_result('bandit', payload, 0, stderr, {'graphify/a.py': 'sha'})
        assert result['result'] == 'incomplete', mutation


def test_execute_captures_nonzero_launch_and_timeout(tmp_path):
    nonzero = security.execute([sys.executable, '-c', 'print("partial");raise SystemExit(3)'],
                               tmp_path, tmp_path, 'failure')
    assert nonzero['exit_code'] == 3
    assert (tmp_path / 'failure.stdout').read_text() == 'partial\n'
    unavailable = security.execute([str(tmp_path / 'missing')], tmp_path, tmp_path, 'missing')
    assert unavailable['exit_code'] is None and 'unavailable' in unavailable['error']
    timeout = security.execute([sys.executable, '-c', 'import time;time.sleep(10)'],
                               tmp_path, tmp_path, 'timeout', timeout=0.01)
    assert timeout['exit_code'] is None and 'timed out' in timeout['error']


@pytest.fixture
def receipt_dir(project, monkeypatch):
    output = project / 'output'
    d = output / 'pip-audit'
    d.mkdir(parents=True)
    coverage = inventory(project)
    monkeypatch.setattr(security, 'identity', lambda root: {'revision': 'abc', 'lock_sha256': 'def'})
    raw = security.canonical(pip_payload(True))
    (d / 'scanner.stdout').write_bytes(raw)
    (d / 'scanner.stderr').write_bytes(b'')
    (d / 'requirements.txt').write_bytes(b'example-pkg==1.0\n')
    (d / 'export.stdout').write_bytes(b'example-pkg==1.0\n')
    security.write_json(output / 'pip-audit-attempt.json', {'attempt': 'a' * 32})
    receipt = {'schema': security.SCHEMA, 'scanner': 'pip-audit', 'attempt': 'a' * 32,
               'identity': security.identity(project), 'scanner_version': '2.10.0',
               'python': '3.14.3', 'created_at': '2026-09-05T00:00:00Z',
               'coverage': coverage, 'coverage_sha256': security.digest(security.canonical(coverage)),
               'requirements_sha256': security.digest((d / 'requirements.txt').read_bytes()),
               'uv_version': 'uv 0.11.30',
               'export_process': {'command': security.EXPORT, 'exit_code': 0, 'error': None,
                                  'stdout_sha256': security.digest((d / 'export.stdout').read_bytes())},
               'result_sha256': security.digest(raw),
               'process': {'command': security.scanner_command('pip-audit', d),
                           'exit_code': 1, 'error': None, 'stdout_sha256': security.digest(raw),
                           'stderr_sha256': security.digest(b'')},
               'errors': [], 'result': 'clean', 'completion': 'complete'}
    security.write_json(d / 'receipt.json', receipt)
    return output


def outcomes():
    return {s: {'outcome': 'failure', 'conclusion': 'success'} for s in security.SCANNERS}


def test_finalizer_recomputes_verdict_and_reports_tolerance(project, receipt_dir):
    result = security.finalize(project, receipt_dir, outcomes())
    pip = result['receipts'][1]
    assert pip['result'] == 'findings'
    assert pip['finding_count'] == 1
    assert pip['ci_step'] == {'outcome': 'failure', 'conclusion': 'success'}
    assert result['receipts'][0]['completion'] == 'incomplete'
    markdown = security.summary(result, 'https://github.com/example/artifacts/1')
    assert '| pip-audit | complete | 1 | 1 | failure | success |' in markdown
    assert 'not a clean scan' in markdown
    assert 'not yet available' in result['final_job_conclusion']
    assert 'Download receipts' in markdown


def test_finalizer_rejects_corruption_without_losing_raw_evidence(project, receipt_dir):
    d = receipt_dir / 'pip-audit'
    original = (d / 'receipt.json').read_bytes()
    for change in ('malformed', 'missing-version', 'wrong-revision', 'coverage-hash', 'raw-hash'):
        data = json.loads(original)
        if change == 'malformed':
            (d / 'receipt.json').write_text('null')
        else:
            if change == 'missing-version':
                del data['scanner_version']
            elif change == 'wrong-revision':
                data['identity']['revision'] = 'wrong'
            elif change == 'coverage-hash':
                data['coverage_sha256'] = 'wrong'
            else:
                data['process']['stdout_sha256'] = 'wrong'
            security.write_json(d / 'receipt.json', data)
        result = security.finalize(project, receipt_dir, outcomes())
        assert result['receipts'][1]['result'] == 'incomplete', change
        assert (d / 'scanner.stdout').read_bytes() == security.canonical(pip_payload(True))


def test_failed_preparation_and_reused_output_cannot_pass(project, monkeypatch):
    monkeypatch.setattr(security, 'identity', lambda root: {'revision': 'abc'})
    monkeypatch.setattr(security, 'installed_distributions', lambda: [])
    output = project / 'results'
    assert security.scan(project, output, 'pip-audit') == 1
    assert security.load_json(output / 'pip-audit/receipt.json')['completion'] == 'incomplete'
    with pytest.raises(FileExistsError):
        security.scan(project, output, 'pip-audit')
    result = security.finalize(project, output, outcomes())
    assert all(r['completion'] == 'incomplete' for r in result['receipts'])


def test_json_duplicate_keys_are_rejected(tmp_path):
    p = tmp_path / 'result.json'
    p.write_text('{"dependencies":null,"dependencies":[]}')
    with pytest.raises(ValueError, match='duplicate'):
        security.load_json(p)


def test_finalizer_runs_without_site_packages_or_successful_sync(project):
    helper = Path(security.__file__).resolve()
    proc = subprocess.run([sys.executable, '-I', '-S', str(helper), 'finalize',
                           '--output', str(project / 'receipts')], cwd=project,
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    result = json.loads((project / 'receipts/summary.json').read_text())
    assert all(r['completion'] == 'incomplete' for r in result['receipts'])


def test_workflow_preserves_scope_and_observes_actual_tolerated_results():
    workflow = Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml'
    steps = yaml.safe_load(workflow.read_text())['jobs']['security-scan']['steps']
    install = next(s for s in steps if s.get('name') == 'Install dependencies')
    assert install['run'] == 'uv sync --frozen'
    scans = [s for s in steps if s.get('id') in ('bandit', 'pip_audit')]
    assert len(scans) == 2 and all(s['continue-on-error'] for s in scans)
    # Progress stays on stdout; Bandit JSON has its own explicit file.
    command = security.scanner_command('bandit', Path('/tmp/results'))
    assert command[-2:] == ['-o', '/tmp/results/scanner.json']
    final = next(s for s in steps if s.get('name') == 'Finalize security receipts')
    assert final['if'] == 'always()' and '--no-project --no-sync' in final['run']
    assert final['env']['PIP_AUDIT_OUTCOME'] == '${{ steps.pip_audit.outcome }}'
    assert final['env']['PIP_AUDIT_CONCLUSION'] == '${{ steps.pip_audit.conclusion }}'
    upload = next(s for s in steps if s.get('id') == 'security_artifact')
    publish = steps[-1]
    assert upload['if'] == publish['if'] == 'always()'
    assert steps.index(final) < steps.index(upload) < steps.index(publish)
    assert publish['env']['ARTIFACT_URL'] == '${{ steps.security_artifact.outputs.artifact-url }}'


def test_bandit_rejects_invalid_counters_and_hidden_per_file_findings():
    for medium, high in ((-1, 1), (0.0, 0.0), (False, 0)):
        payload = bandit_payload()
        payload['metrics']['_totals'].update({'SEVERITY.MEDIUM': medium, 'SEVERITY.HIGH': high})
        assert security.audit_result('bandit', payload, 0, '', {'graphify/a.py': 'sha'})['result'] == 'incomplete'
    payload = bandit_payload()
    payload['metrics']['graphify/a.py'].update({'SEVERITY.MEDIUM': 1, 'SEVERITY.HIGH': 0})
    assert security.audit_result('bandit', payload, 0, '', {'graphify/a.py': 'sha'})['result'] == 'incomplete'


def test_rejected_reuse_invalidates_previously_completed_scan(project, receipt_dir):
    # The old scan had findings (exit1), indistinguishable by exit code from
    # the newly rejected invocation; invocation identity must disambiguate it.
    assert security.finalize(project, receipt_dir, outcomes())['receipts'][1]['completion'] == 'complete'
    with pytest.raises(FileExistsError):
        security.scan(project, receipt_dir, 'pip-audit')
    report = security.finalize(project, receipt_dir, outcomes())
    assert report['receipts'][1]['completion'] == 'incomplete'
    assert security.load_json(receipt_dir / 'pip-audit/scanner.stdout') == pip_payload(True)
