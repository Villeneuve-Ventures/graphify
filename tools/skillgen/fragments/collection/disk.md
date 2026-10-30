Wait for all dispatches. Follow **Disk result staging** in `references/extraction-spec.md` before B3: stop on any failed, missing, invalid or truncated result, retain diagnostics, and do not import siblings, cache or publish. The public `graphify-out` directory is not the chunk success signal.

For each successful dispatch, bind GRAPHIFY_CHUNK_USAGE_JSON to its actual parent-observed tool usage (or explicit JSON null when unavailable), then use the exact current prepared-token import. Success requires verified prepared chunk readback and registration in this batch's `.graphify_completed_chunks.json`. Confirm every planned chunk was imported before proceeding. An empty extraction with explicit validated completion is valid; partial or unowned claims do not certify complete cache entries.

Merge only the registered prepared paths into `.graphify_semantic_new.json`. The command below checks every plan belonging to this owner and batch against the manifest and checks that their assignments cover exactly B0's uncached sources, including when an aggregate already exists. Missing, duplicate, overlapping or foreign assignments/registrations stop before aggregate creation and cache saving. Assignment coverage is not extraction completion: preserve validated successful-empty and partial completion distinctions. Old-batch plans remain diagnostic evidence and do not grant success. Read chunks in numeric order, with the filename breaking padded-number ties, so first-record selection is independent of import order. Token counts were applied during import; do not rewrite registered chunk JSON or substitute public paths. Then run:

```bash
"$(cat graphify-out/.graphify_python)" -E -P -B -c "
import json, re
from pathlib import Path
from graphify.build import _replacement_source_set
from graphify.transaction import current_transaction, pin_output, _read_managed_bytes, commit_prepared_bytes

owner = current_transaction()
all_nodes, all_edges, all_hyperedges, completed_sources = [], [], [], set()
total_in, total_out = 0, 0
with pin_output(Path.cwd(), mutation=False) as prepared:
    def read(name):
        return _read_managed_bytes(prepared, name, 512 * 1024 * 1024)[0]
    batch = read('.graphify_dispatch_batch.txt').decode('utf-8')
    chunks = json.loads(read('.graphify_completed_chunks.json'))
    if (not isinstance(chunks, list) or any(not isinstance(path, str) for path in chunks)
            or len(set(chunks)) != len(chunks)):
        raise ValueError('each planned chunk must be registered exactly once')
    expected = set()
    assignments = set()
    uncached = _replacement_source_set(read('.graphify_uncached.txt').decode('utf-8').splitlines(), owner.root)
    for path in Path.cwd().glob('.graphify_intake_plan_*.json'):
        plan = json.loads(read(path.name))
        if not isinstance(plan, dict):
            raise ValueError('invalid chunk plan')
        if plan.get('owner') != owner.id or plan.get('batch') != batch:
            continue
        number = plan.get('number')
        if (not isinstance(number, str) or re.fullmatch('[0-9]{2,}', number) is None
                or int(number) < 1 or path.name != '.graphify_intake_plan_' + number + '.json'):
            raise ValueError('invalid current chunk plan number')
        expected.add(str(Path.cwd() / ('.graphify_chunk_' + number + '.json')))
        assigned = _replacement_source_set(plan.get('assigned'), owner.root)
        if not assigned or assignments & assigned:
            raise ValueError('current planned assignments must be nonempty and disjoint')
        assignments.update(assigned)
    if set(chunks) != expected:
        raise ValueError('every current planned chunk must have exactly one registered import')
    if assignments != uncached:
        raise ValueError('current planned assignments must cover exactly B0 uncached inputs')
    # Capture and aggregate one regular file at a time through the bounded reader.
    for path in sorted(chunks, key=lambda path: (
            int(Path(path).name[len('.graphify_chunk_'):-len('.json')]), Path(path).name)):
        result = json.loads(read(Path(path).name))
        all_nodes += result.get('nodes', [])
        all_edges += result.get('edges', [])
        all_hyperedges += result.get('hyperedges', [])
        completed_sources.update(result.get('completed_sources', []))
        total_in += result.get('input_tokens', 0)
        total_out += result.get('output_tokens', 0)
payload = json.dumps({
    'nodes': all_nodes, 'edges': all_edges, 'hyperedges': all_hyperedges,
    'completed_sources': sorted(completed_sources),
    'input_tokens': total_in, 'output_tokens': total_out,
}, indent=2, ensure_ascii=False).encode('utf-8')
commit_prepared_bytes(owner, '.graphify_semantic_new.json', payload)
print(f'Merged {len(chunks)} chunks: {total_in:,} in / {total_out:,} out tokens')
"
```
