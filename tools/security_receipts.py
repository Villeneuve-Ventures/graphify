"""Advisory CI scans with reconciled coverage and reproducible outcome receipts.

The finalizer uses only the standard library so it can report failed preparation.
This is CI tooling, not a public Graphify command or a security-policy baseline.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import time
import uuid
import tomllib
from urllib.parse import unquote, urlsplit

SCHEMA = 'graphify.security-receipt.v1'
SCANNERS = ('bandit', 'pip-audit')
EXPORT = ['uv', 'export', '--frozen', '--format', 'requirements.txt',
          '--no-default-groups', '--group', 'dev', '--no-emit-project',
          '--no-hashes', '--no-header', '--no-annotate']


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_bytes(canonical(value) + b'\n')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def normalized(name):
    require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name),
            'invalid distribution name')
    return re.sub(r'[-_.]+', '-', name).lower()


def pairs(rows):
    require(isinstance(rows, list), 'package inventory must be a list')
    result = {}
    for row in rows:
        name = normalized(row['name'])
        version = row['version']
        require(isinstance(version, str) and bool(version), 'missing package version')
        require(name not in result, f'duplicate package: {name}')
        result[name] = version
    return result


def installed_distributions():
    # Default discovery can select cwd's egg-info instead of the installed root.
    paths = sorted({sysconfig.get_path('purelib'), sysconfig.get_path('platlib')})
    rows = []
    for dist in metadata.distributions(path=paths):
        direct = dist.read_text('direct_url.json')
        rows.append({'name': normalized(dist.metadata['Name']), 'version': dist.version,
                     'direct_url': json.loads(direct) if direct else None})
    return sorted(rows, key=lambda row: row['name'])


def inventory(root, exported, installed, marker_environment):
    from packaging.requirements import Requirement
    from packaging.version import Version

    project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    lock = tomllib.loads((root / 'uv.lock').read_text())
    name, version = normalized(project['name']), project['version']
    actual = pairs(installed)
    require(actual.get(name) == version, 'local project missing or wrong version')
    local = next(row for row in installed if row['name'] == name)
    direct = local['direct_url']
    require(isinstance(direct, dict) and direct.get('dir_info') == {'editable': True},
            'root project is not a proven editable installation')
    url = urlsplit(direct['url'])
    require(url.scheme == 'file' and not url.netloc and not url.query and not url.fragment
            and Path(unquote(url.path)).resolve() == root.resolve(),
            'root editable source does not match this checkout')
    roots = [p for p in lock['package'] if normalized(p['name']) == name]
    require(len(roots) == 1 and roots[0]['version'] == version
            and roots[0]['source'] == {'editable': '.'}, 'lock root identity mismatch')
    expected = {}
    for line in exported.splitlines():
        if not line.strip():
            continue
        req = Requirement(line)
        require(not req.url and not req.extras, 'unauditable export requirement')
        specs = list(req.specifier)
        require(len(specs) == 1 and specs[0].operator == '==' and '*' not in specs[0].version,
                'export requirement must have one exact version')
        Version(specs[0].version)
        if req.marker and not req.marker.evaluate(marker_environment):
            continue
        key = normalized(req.name)
        require(key != name and key not in expected, 'duplicate or root export requirement')
        expected[key] = specs[0].version
    require(expected, 'empty third-party inventory')
    third_party = [row for row in installed if row['name'] != name]
    require(pairs(third_party) == expected,
            f'installed/export mismatch: missing={sorted(set(expected) - set(actual))}, '
            f'unexpected={sorted(set(actual) - set(expected) - {name})}; check versions too')
    for row in third_party:
        require(row['direct_url'] is None, f'unauditable non-registry package: {row["name"]}')
        matches = [p for p in lock['package'] if normalized(p['name']) == row['name']
                   and p['version'] == row['version']]
        require(len(matches) == 1 and matches[0]['source'] == {'registry': 'https://pypi.org/simple'},
                f'package not uniquely bound to public registry lock: {row["name"]}')
    return {'scope': 'frozen default plus dev; no extras', 'installed': installed,
            'third_party': [{'name': n, 'version': v} for n, v in sorted(expected.items())],
            'excluded_project': {**local, 'reason': 'local root project is not a PyPI dependency'},
            'marker_environment': marker_environment}


def execute(command, root, directory, stem, timeout=300):
    started = time.monotonic()
    error = None
    code = None
    try:
        proc = subprocess.run(command, cwd=root, capture_output=True, timeout=timeout, check=False)
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = exc.stdout or b'', exc.stderr or b''
        error = f'process timed out after {timeout}s'
    except OSError as exc:
        stdout, stderr = b'', b''
        error = f'process unavailable: {type(exc).__name__}'
    (directory / f'{stem}.stdout').write_bytes(stdout)
    (directory / f'{stem}.stderr').write_bytes(stderr)
    return {'command': command, 'exit_code': code, 'error': error,
            'duration_seconds': round(time.monotonic() - started, 3),
            'stdout_sha256': digest(stdout), 'stderr_sha256': digest(stderr)}


def identity(root):
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=root)
    return {'revision': head, 'dirty': bool(status), 'status_sha256': digest(status),
            'lock_sha256': digest((root / 'uv.lock').read_bytes()),
            'pyproject_sha256': digest((root / 'pyproject.toml').read_bytes()),
            'helper_sha256': digest(Path(__file__).read_bytes())}


def source_files(root):
    return {str(path.relative_to(root)): digest(path.read_bytes())
            for path in sorted((root / 'graphify').rglob('*.py'))}


def audit_result(scanner, payload, exit_code, stderr, coverage):
    findings, errors = None, []
    try:
        require(isinstance(payload, dict), 'missing or malformed JSON object')
        if scanner == 'pip-audit':
            deps = payload['dependencies']
            require(isinstance(deps, list) and payload['fixes'] == [], 'invalid dependencies/fixes')
            findings = []
            require(coverage['third_party'], 'empty expected dependency inventory')
            for row in deps:
                if isinstance(row, dict) and isinstance(row.get('vulns'), list):
                    findings.extend({'package': row.get('name'), 'vulnerability': v}
                                    for v in row['vulns'])
            require(pairs(deps) == pairs(coverage['third_party']), 'audit package coverage mismatch')
            for row in deps:
                require('skip_reason' not in row and isinstance(row['vulns'], list),
                        'skipped or malformed audited package')
                for vuln in row['vulns']:
                    require(isinstance(vuln, dict) and isinstance(vuln['id'], str) and vuln['id']
                            and isinstance(vuln['fix_versions'], list)
                            and all(isinstance(v, str) for v in vuln['fix_versions']),
                            'malformed vulnerability record')
        else:
            require(isinstance(payload['results'], list), 'invalid Bandit results')
            findings = payload['results']
            require(payload['errors'] == [], 'Bandit skipped files or parse errors')
            metrics = payload['metrics']
            require(isinstance(metrics, dict) and coverage and
                    set(metrics) == set(coverage) | {'_totals'}, 'Bandit file coverage mismatch')
            for path, metric in metrics.items():
                require(isinstance(metric, dict) and metric
                        and all(type(value) is int and value >= 0 for value in metric.values()),
                        'invalid Bandit metrics')
            require(metrics['_totals']['loc'] > 0, 'empty Bandit scan')
            require(sum(metrics[p]['loc'] for p in coverage) == metrics['_totals']['loc'],
                    'Bandit line totals mismatch')
            for finding in findings:
                require(isinstance(finding, dict) and finding['filename'] in coverage
                        and finding['issue_severity'] in ('MEDIUM', 'HIGH')
                        and finding['issue_confidence'] in ('LOW', 'MEDIUM', 'HIGH')
                        and re.fullmatch(r'B\d{3}', finding['test_id'])
                        and type(finding['line_number']) is int and finding['line_number'] > 0
                        and isinstance(finding['issue_text'], str) and finding['issue_text'],
                        'malformed Bandit finding')
            for level in ('MEDIUM', 'HIGH'):
                key = f'SEVERITY.{level}'
                require(sum(metrics[path][key] for path in coverage) == metrics['_totals'][key],
                        'Bandit aggregate severity mismatch')
                for path in coverage:
                    require(metrics[path][key] == sum(
                        f['filename'] == path and f['issue_severity'] == level for f in findings),
                        'Bandit per-file finding count mismatch')
        require(not re.search(r'\b(ERROR|CRITICAL)\b|internal error|Traceback \(', stderr,
                              re.IGNORECASE), 'scanner emitted error diagnostics')
        require(type(exit_code) is int and exit_code == (1 if findings else 0),
                'scanner exit code inconsistent with completed result')
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(f'invalid/incomplete scanner result: {exc}')
    return {'completion': 'incomplete' if errors else 'complete',
            'result': 'incomplete' if errors else ('findings' if findings else 'clean'),
            'findings': findings, 'finding_count': len(findings) if findings is not None else None,
            'errors': errors}


def load_json(path):
    def unique_keys(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    return json.loads(path.read_bytes(), object_pairs_hook=unique_keys)


def scanner_command(scanner, directory):
    if scanner == 'pip-audit':
        return [sys.executable, '-m', 'pip_audit', '--strict', '--no-deps', '--disable-pip',
                '--progress-spinner', 'off', '--vulnerability-service', 'pypi', '--format', 'json',
                '--requirement', str(directory / 'requirements.txt')]
    # Bandit progress output goes to stdout even with JSON formatting.
    return [sys.executable, '-m', 'bandit', '-r', 'graphify', '-ll', '-f', 'json',
            '-o', str(directory / 'scanner.json')]


def result_path(directory, scanner):
    return directory / ('scanner.json' if scanner == 'bandit' else 'scanner.stdout')


def scan(root, output, scanner):
    attempt = uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=True)
    # Record the new invocation even if exclusive creation below rejects reuse.
    write_json(output / f'{scanner}-attempt.json', {'attempt': attempt})
    directory = output / scanner
    # Exclusive creation prevents stale raw output/receipts from becoming current proof.
    directory.mkdir(parents=True, exist_ok=False)
    receipt = {'schema': SCHEMA, 'scanner': scanner, 'attempt': attempt, 'created_at': datetime.now(timezone.utc).isoformat(),
               'identity': None, 'python': sys.version, 'scanner_version': None,
               'coverage': None, 'coverage_sha256': None, 'process': None,
               'completion': 'incomplete', 'result': 'incomplete', 'findings': None,
               'finding_count': None, 'errors': []}
    try:
        receipt['identity'] = identity(root)
        installed = installed_distributions()
        receipt['scanner_version'] = pairs(installed)[scanner]
        if scanner == 'pip-audit':
            from packaging.markers import default_environment
            export = execute(EXPORT, root, directory, 'export')
            receipt['export_process'] = export
            require(export['exit_code'] == 0 and not export['error'], 'frozen export failed')
            coverage = inventory(root, (directory / 'export.stdout').read_text(),
                                 installed, default_environment())
            requirements = ''.join(f'{r["name"]}=={r["version"]}\n' for r in coverage['third_party'])
            (directory / 'requirements.txt').write_text(requirements)
            receipt['requirements_sha256'] = digest(requirements.encode())
            receipt['uv_version'] = subprocess.check_output(['uv', '--version'], text=True).strip()
        else:
            coverage = source_files(root)
        command = scanner_command(scanner, directory)
        receipt['coverage'] = coverage
        receipt['coverage_sha256'] = digest(canonical(coverage))
        receipt['process'] = execute(command, root, directory, 'scanner')
        proc = receipt['process']
        try:
            raw_path = result_path(directory, scanner)
            receipt['result_sha256'] = digest(raw_path.read_bytes())
            payload = load_json(raw_path)
        except (ValueError, OSError):
            payload = None
        receipt.update(audit_result(scanner, payload, proc['exit_code'],
                                    (directory / 'scanner.stderr').read_text(errors='replace'), coverage))
        if proc['error']:
            receipt['errors'].append(proc['error'])
        require(identity(root) == receipt['identity'], 'repository identity changed during scan')
        if scanner == 'bandit':
            require(source_files(root) == coverage, 'source changed during scan')
        else:
            require(installed_distributions() == installed, 'installed inventory changed during scan')
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        receipt['errors'].append(f'preparation/evidence failure: {exc}')
    if receipt['errors']:
        receipt.update(completion='incomplete', result='incomplete')
    write_json(directory / 'receipt.json', receipt)
    print(f'{scanner}: {receipt["completion"]}, {receipt["finding_count"]} finding records')
    return 0 if receipt['result'] == 'clean' else 1


def finalize(root, output, outcomes):
    receipts = []
    for scanner in SCANNERS:
        directory = output / scanner
        try:
            receipt = load_json(directory / 'receipt.json')
            require(isinstance(receipt['attempt'], str)
                    and re.fullmatch(r'[0-9a-f]{32}', receipt['attempt'])
                    and load_json(output / f'{scanner}-attempt.json') == {'attempt': receipt['attempt']},
                    'receipt does not belong to the current scan attempt')
            require(receipt['schema'] == SCHEMA and receipt['scanner'] == scanner,
                    'receipt schema/scanner mismatch')
            require(receipt['identity'] == identity(root), 'receipt revision/lock identity mismatch')
            require(isinstance(receipt['errors'], list), 'malformed receipt errors')
            proc, coverage = receipt['process'], receipt['coverage']
            locked_versions = {p['version'] for p in tomllib.loads((root / 'uv.lock').read_text())
                               ['package'] if p['name'] == scanner}
            require(receipt['scanner_version'] in locked_versions, 'scanner version not in lock')
            require(isinstance(receipt['python'], str) and receipt['python']
                    and isinstance(receipt['created_at'], str) and receipt['created_at']
                    and isinstance(proc['command'], list) and len(proc['command']) > 3
                    and proc['command'][1:] == scanner_command(scanner, directory)[1:],
                    'malformed scanner provenance')
            require(digest(canonical(coverage)) == receipt['coverage_sha256'], 'coverage hash mismatch')
            for stream in ('stdout', 'stderr'):
                require(digest((directory / f'scanner.{stream}').read_bytes()) == proc[f'{stream}_sha256'],
                        'raw scanner hash mismatch')
            if scanner == 'pip-audit':
                require(receipt['export_process']['command'] == EXPORT
                        and receipt['export_process']['exit_code'] == 0
                        and not receipt['export_process']['error']
                        and isinstance(receipt['uv_version'], str) and receipt['uv_version'],
                        'malformed export provenance')
                requirements = (directory / 'requirements.txt').read_bytes()
                require(digest(requirements) == receipt['requirements_sha256'],
                        'requirements hash mismatch')
                expected = ''.join(f'{n}=={v}\n' for n, v in sorted(pairs(coverage['third_party']).items()))
                require(requirements == expected.encode(), 'requirements/coverage mismatch')
                require(digest((directory / 'export.stdout').read_bytes()) ==
                        receipt['export_process']['stdout_sha256'], 'export hash mismatch')
            else:
                require(source_files(root) == coverage, 'Bandit source identity mismatch')
            raw_path = result_path(directory, scanner)
            require(digest(raw_path.read_bytes()) == receipt['result_sha256'], 'result hash mismatch')
            result = audit_result(scanner, load_json(raw_path), proc['exit_code'],
                                  (directory / 'scanner.stderr').read_text(errors='replace'), coverage)
            # Recompute the verdict, preserving earlier preparation/process errors.
            result['errors'] = list(dict.fromkeys(receipt['errors'] + result['errors']))
            if proc['error']:
                result['errors'].append(proc['error'])
            if result['errors']:
                result.update(completion='incomplete', result='incomplete')
            receipt.update(result)
        except (ValueError, KeyError, TypeError, OSError, AttributeError, subprocess.SubprocessError) as exc:
            # The original scanner receipt/raw files remain available for inspection.
            receipt = {'schema': SCHEMA, 'scanner': scanner, 'completion': 'incomplete',
                       'result': 'incomplete', 'finding_count': None, 'findings': None,
                       'errors': [f'missing/invalid receipt: {exc}'], 'process': None}
        receipt['ci_step'] = outcomes[scanner]
        receipts.append(receipt)
    result = {'schema': 'graphify.security-summary.v1', 'receipts': receipts,
              'job_status_at_reporting': os.environ.get('JOB_STATUS', 'unavailable'),
              'final_job_conclusion': 'not yet available; consult the linked Actions run',
              'run_url': os.environ.get('RUN_URL', ''),
              'policy': 'advisory; tolerated failure is not a clean scan'}
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / 'summary.json', result)
    (output / 'summary.md').write_text(summary(result))
    print((output / 'summary.md').read_text())
    return result


def summary(result, artifact_url=''):
    lines = ['## Advisory security scans', '',
             '| Scanner | Completion | Finding records | Scanner exit | Step outcome | Tolerated conclusion |',
             '| --- | --- | ---: | --- | --- | --- |']
    for r in result['receipts']:
        code = r['process']['exit_code'] if r['process'] else 'unavailable'
        count = r['finding_count'] if r['finding_count'] is not None else 'unknown'
        lines.append(f'| {r["scanner"]} | {r["completion"]} | {count} | {code} | '
                     f'{r["ci_step"]["outcome"]} | {r["ci_step"]["conclusion"]} |')
    lines += ['', 'A tolerated failure is not a clean scan. Findings are rule/advisory records, ' +
              'not demonstrated exploits. Duplicate advisory records may share an ID.',
              f'Job status observed at reporting: {result["job_status_at_reporting"]}. '
              'Final job conclusion must be read from Actions after completion.']
    for r in result['receipts']:
        if r.get('coverage') and r['scanner'] == 'pip-audit':
            c = r['coverage']
            lines += [f'Audit inventory: {len(c["third_party"])} third-party packages; '
                      f'explicit local exclusion: {c["excluded_project"]["name"]} '
                      f'{c["excluded_project"]["version"]}.']
        for error in r['errors']:
            # JSON quoting + HTML escaping keeps scanner text from adding Markdown commands/markup.
            import html
            lines.append(f'<pre>{html.escape(r["scanner"] + ": " + str(error))}</pre>')
    if result.get('run_url'):
        lines.append(f'[Actions run]({result["run_url"]})')
    if artifact_url:
        lines.append(f'[Download receipts and raw results]({artifact_url})')
    else:
        lines.append('Receipt artifact link unavailable until a successful artifact upload.')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('scan', 'finalize', 'publish'))
    parser.add_argument('--scanner', choices=SCANNERS)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    output = args.output.resolve()
    if args.action == 'scan':
        if not args.scanner:
            parser.error('--scanner is required')
        return scan(root, output, args.scanner)
    if args.action == 'finalize':
        outcomes = {s: {k: os.environ.get(f'{s.upper().replace("-", "_")}_{k.upper()}', 'unavailable')
                        for k in ('outcome', 'conclusion')} for s in SCANNERS}
        finalize(root, output, outcomes)
    else:
        try:
            report = load_json(output / 'summary.json')
            markdown = summary(report, os.environ.get('ARTIFACT_URL', ''))
        except (ValueError, OSError, KeyError, TypeError):
            markdown = '## Advisory security scans\n\nINCOMPLETE: security reporting failed; inspect job logs.\n'
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(markdown)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
