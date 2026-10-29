## Disk result staging

Parent-only steps for disk/manual hosts. CHUNK_PATH is an untrusted intake file, never the published or prepared chunk path. Use only the exact current GRAPHIFY_TRANSACTION_TOKEN retained from Step 1/B0 and the freshly admitted interpreter below; stop if that token is missing or refused. The runner alone can commit into its pinned prepared output.

Before dispatch, assign each current chunk a positive padded number. Bind GRAPHIFY_CHUNK_NUMBER and GRAPHIFY_CHUNK_FILES_JSON as environment data (the latter is the JSON array of that chunk's exact FILE_LIST paths). Set complete quoted data literals: POSIX single quotes with embedded apostrophes escaped as '\''; PowerShell single quotes with doubled apostrophes. Never substitute path or JSON bytes into Python code. Export those variables on POSIX; use $env: variables on PowerShell. Do not dispatch a file outside B0's uncached list or assign a file to multiple chunks. Run this allocation once per chunk; give the printed absolute CHUNK_PATH to the subagent's Write/file editor as data. The compact prompt's inline-return instruction is overridden for these disk hosts: write JSON to CHUNK_PATH and return only a completion notice.

```bash
"$GRAPHIFY_PYTHON" -E -P -B -m graphify.transaction run-prepared-token "$GRAPHIFY_TRANSACTION_TOKEN" -- -c "
import json, os, re, tempfile
from pathlib import Path
from graphify.transaction import current_transaction, commit_prepared_bytes, pin_output
owner = current_transaction()
number = os.environ['GRAPHIFY_CHUNK_NUMBER']
if not re.fullmatch('[0-9]{2,}', number) or int(number) < 1:
    raise ValueError('chunk number must be a positive padded decimal')
assigned = json.loads(os.environ['GRAPHIFY_CHUNK_FILES_JSON'])
uncached = set(Path('.graphify_uncached.txt').read_text(encoding='utf-8').splitlines())
if (not isinstance(assigned, list) or not assigned or len(assigned) > 25
        or any(not isinstance(item, str) for item in assigned)
        or len(set(assigned)) != len(assigned) or not set(assigned) <= uncached):
    raise ValueError('chunk assignment must name only current uncached input files')
batch = Path('.graphify_dispatch_batch.txt').read_text(encoding='utf-8')
plan_name = '.graphify_intake_plan_' + number + '.json'
if Path(plan_name).exists() and json.loads(Path(plan_name).read_text())['batch'] == batch:
    raise ValueError('this chunk already has an intake; retain it for diagnosis')
intake_root = Path(tempfile.gettempdir()).resolve(strict=True)
if any(intake_root.is_relative_to(path.resolve()) for path in (Path(owner.output), Path.cwd())):
    raise ValueError('intake storage must be outside public and prepared output')
intake = Path(tempfile.mkdtemp(prefix='graphify-chunk-', dir=intake_root))
with pin_output(intake) as capability:
    identity = capability.identity.json()
plan = {'owner': owner.id, 'batch': batch, 'number': number,
        'assigned': assigned, 'intake': str(intake), 'identity': identity}
print('Intake retained for this chunk: ' + str(intake), file=__import__('sys').stderr)
commit_prepared_bytes(owner, plan_name, json.dumps(plan).encode('utf-8'))
print(json.dumps({'CHUNK_PATH': str(intake / 'result.json'), 'CHUNK_NUM': number}))
"
```

Wait for all dispatches. If any fails, has no result or returns invalid/truncated output, retain its intake and diagnostics and stop before B3; do not import siblings, save cache or publish. Treat this stop rule as overriding B3's older skip-failed-chunk advice. Check explicit completed_sources against the assigned files and actual full reads; omit failed, unreadable, partial and truncated sources. A successful empty extraction is valid, but an absent result is not.

Then the parent runs this import serially for each successfully completed chunk, with GRAPHIFY_CHUNK_NUMBER bound to its trusted number. The existing bounded native reader rejects symlink/nonregular results and reads at most 512 MiB (the graph artifact limit). The intake and its JSON are untrusted data. Foreign record ownership refuses import; explicit completion is intersected with the assigned files and excludes incomplete or partial results. No record ownership or intake path grants completion. B3 keeps v8's existing cache and merge contracts: only explicit validated completion can certify a complete cache entry; retained partial claims cannot. Only verified prepared readback is registered in the current success manifest. Do not write .graphify_semantic_new.json here.

```bash
"$GRAPHIFY_PYTHON" -E -P -B -m graphify.transaction run-prepared-token "$GRAPHIFY_TRANSACTION_TOKEN" -- -c "
import json, os, re
from pathlib import Path
from graphify.build import _merge_source_file
from graphify.llm import _completed_source_paths
from graphify.transaction import (current_transaction, commit_prepared_bytes,
    pin_output, _read_relative_bytes)
owner = current_transaction()
number = os.environ['GRAPHIFY_CHUNK_NUMBER']
if not re.fullmatch('[0-9]{2,}', number) or int(number) < 1:
    raise ValueError('invalid chunk number')
plan = json.loads(Path('.graphify_intake_plan_' + number + '.json').read_text())
if (plan['owner'] != owner.id or plan['number'] != number
        or plan['batch'] != Path('.graphify_dispatch_batch.txt').read_text()):
    raise ValueError('intake belongs to a different owner or dispatch batch')
with pin_output(Path(plan['intake'])) as capability:
    if capability.identity.json() != plan['identity']:
        raise ValueError('intake directory identity changed')
    # Existing native no-follow regular-file reader; capture immutable bytes once.
    payload = _read_relative_bytes(capability, 'result.json', 512 * 1024 * 1024)
    capability.validate()
result = json.loads(payload)
if (not isinstance(result, dict)
        or any(not isinstance(result.get(key), list) for key in ('nodes', 'edges'))
        or not isinstance(result.get('hyperedges', []), list)
        or any(not isinstance(item, dict) for key in ('nodes', 'edges', 'hyperedges')
               for item in result.get(key, []))
        or not isinstance(result.get('completed_sources', []), list)
        or any(not isinstance(item, str) for item in result.get('completed_sources', []))
        or not isinstance(result.get('incomplete_sources', []), list)
        or any(not isinstance(item, str) for item in result.get('incomplete_sources', []))):
    raise ValueError('chunk must be an extraction object with record lists')
if (result.get('error') or result.get('failed_chunks')
        or result.get('finish_reason') not in (None, 'stop', 'length')):
    raise ValueError('failed chunk cannot be registered')
root = Path(owner.root)
assigned = _completed_source_paths({'completed_sources': plan['assigned']}, root)
# Reuse v8's ownership keys and explicit-completion validator. A foreign claim
# refuses the entire intake instead of importing the donor's graph-scope filter.
for key in ('nodes', 'edges', 'hyperedges'):
    for record in result.get(key, []):
        source = record.get('source_file', record.get('source') if key == 'nodes' else None)
        if source:
            source = _merge_source_file(source, str(root))
            if str(Path(os.path.abspath(root / source))) not in assigned:
                raise ValueError('record ownership is outside this chunk assignment')
complete = _completed_source_paths(result, root) & assigned
incomplete = _completed_source_paths({'completed_sources': result.get('incomplete_sources', [])}, root)
complete -= incomplete
if (result.get('_partial') or any(
        record.get('_partial') or not record.get('source_file', record.get('source') if key == 'nodes' else None)
        for key in ('nodes', 'edges', 'hyperedges') for record in result.get(key, []))):
    complete.clear()
result['completed_sources'] = sorted(complete)
# JSON stays disk-backed across the host/subagent boundary; never interpolate it into code.
payload = json.dumps(result, ensure_ascii=False).encode('utf-8')
chunk_name = '.graphify_chunk_' + number + '.json'
commit_prepared_bytes(owner, chunk_name, payload)
if Path(chunk_name).read_bytes() != payload:
    raise ValueError('prepared chunk readback changed')
manifest_name = '.graphify_completed_chunks.json'
completed = json.loads(Path(manifest_name).read_text())
chunk_path = str(Path(chunk_name).absolute())
if chunk_path not in completed:
    completed.append(chunk_path)
commit_prepared_bytes(owner, manifest_name, json.dumps(completed).encode('utf-8'))
print('Staged and registered chunk ' + number + '; retain intake until consumers finish.')
"
```

Keep failed/partial intake bytes and diagnostics for recovery. Once import/readback and all dependent consumers finish, the parent may remove only that successful intake file/directory after rechecking its recorded identity, content, links and consumers. Do not run an unconditional cleanup on failure or follow intake symlinks.
