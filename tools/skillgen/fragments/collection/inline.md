**Step B3 - Collect, cache, and merge**

Wait for all current dispatches and validate their JSON responses. The normal
success signal is the inline aggregate staged by B2 in the prepared output;
it does not require chunk files or a disk-writing agent. Check explicit
`completed_sources` against the assigned files and the parent's evidence of full
reads. Never infer completion from record ownership, token usage or a callback.
A successful empty result with explicit completion is valid; a missing result
is not. Retain failed, invalid or truncated responses and stop before cache saving
or Part C. Partial claims may remain useful, but cannot certify completion.

Codex requires the direct aggregate. OpenCode also supports B2's serial fallback:
write each validated response to a chunk file through the exact prepared-token
runner, read it back, then register its absolute prepared path in the current
`.graphify_completed_chunks.json`. Never scan public files or reuse an earlier
batch. B0 resets both the manifest and the direct aggregate. Use the parent's
observed token counts when the host exposes them; report unavailable usage and
retain numeric zero placeholders otherwise. Do not report placeholders as measured
usage.

Run this validation in both modes before saving cache. A direct aggregate takes
precedence over the serial fallback, even when its record lists are empty. The
fallback accepts only registered, regular files in this prepared directory.

```bash
"$(cat graphify-out/.graphify_python)" -E -P -B -c "
import json, os, re
from pathlib import Path
from graphify.build import _merge_source_file, _replacement_source_set
from graphify.llm import _completed_source_paths
from graphify.transaction import current_transaction, commit_prepared_bytes, pin_output, _read_managed_bytes

root = Path('INPUT_PATH').resolve(strict=True)
allow_serial_fallback = @@INLINE_SERIAL_FALLBACK@@
aggregate_name = '.graphify_semantic_new.json'
chunks = []
with pin_output(Path.cwd(), mutation=False) as prepared:
    def read(name):
        return _read_managed_bytes(prepared, name, 512 * 1024 * 1024)[0].decode('utf-8')
    allowed = {os.path.abspath(root / source) for source in _replacement_source_set(
        read('.graphify_uncached.txt').splitlines(), str(root))}
    try:
        direct = read(aggregate_name)
    except FileNotFoundError:
        if not allowed:
            results = []  # B0 found only cache hits; no dispatch was needed.
        elif not allow_serial_fallback:
            raise ValueError('Codex requires the current inline aggregate')
        else:
            chunks = json.loads(read('.graphify_completed_chunks.json'))
            if (not isinstance(chunks, list) or not chunks
                    or any(not isinstance(path, str) for path in chunks)
                    or len(set(chunks)) != len(chunks)):
                raise ValueError('No valid current inline aggregate or serial results')
            results = []
            for path in chunks:
                candidate = Path(path)
                if (not candidate.is_absolute() or candidate.parent != Path.cwd()
                        or re.fullmatch(r'\.graphify_chunk_[0-9]+\.json', candidate.name) is None):
                    raise ValueError('Serial result is outside the current prepared output')
                results.append(json.loads(read(candidate.name)))
    else:
        results = [json.loads(direct)]

all_nodes, all_edges, all_hyperedges, completed_sources, incomplete_sources = [], [], [], set(), set()
total_in, total_out = 0, 0
for result in results:
    if (not isinstance(result, dict)
            or any(not isinstance(result.get(key), list) for key in ('nodes', 'edges'))
            or not isinstance(result.get('hyperedges', []), list)
            or any(not isinstance(record, dict) for key in ('nodes', 'edges', 'hyperedges')
                   for record in result.get(key, []))):
        raise ValueError('Inline result must be an extraction object with record lists')
    if (result.get('error') or result.get('errors') or result.get('failed_chunks')
            or result.get('finish_reason') not in (None, 'stop')):
        raise ValueError('Failed or truncated result cannot enter B3')
    # Use the existing path validator directly: a malformed negative declaration
    # must refuse instead of silently discarding another source's veto.
    _replacement_source_set(result.get('completed_sources', []), str(root))
    incomplete_sources.update(os.path.abspath(root / source) for source in
                              _replacement_source_set(result.get('incomplete_sources', []), str(root)))
    complete = _completed_source_paths(result, root) & allowed
    partial = bool(result.get('_partial'))
    for key in ('nodes', 'edges', 'hyperedges'):
        for record in result.get(key, []):
            owner = record.get('source_file', record.get('source') if key == 'nodes' else None)
            _merge_source_file(owner, str(root))
            partial = partial or bool(record.get('_partial')) or not owner
    if partial:
        incomplete_sources.update(complete)
        complete.clear()
    completed_sources.update(complete)
    all_nodes += result['nodes']
    all_edges += result['edges']
    all_hyperedges += result.get('hyperedges', [])
    for key in ('input_tokens', 'output_tokens'):
        if type(result.get(key, 0)) is not int or result.get(key, 0) < 0:
            raise ValueError('Token counts must be nonnegative integers')
    total_in += result.get('input_tokens', 0)
    total_out += result.get('output_tokens', 0)
aggregate = {
    'nodes': all_nodes, 'edges': all_edges, 'hyperedges': all_hyperedges,
    'completed_sources': sorted(completed_sources - incomplete_sources),
    'input_tokens': total_in, 'output_tokens': total_out,
}
commit_prepared_bytes(current_transaction(), aggregate_name,
                      json.dumps(aggregate, indent=2, ensure_ascii=False).encode('utf-8'))
print(f'Merged {len(chunks)} chunks: {total_in:,} in / {total_out:,} out tokens' if chunks
      else f'Validated inline aggregate: {total_in:,} in / {total_out:,} out tokens')
"
```
