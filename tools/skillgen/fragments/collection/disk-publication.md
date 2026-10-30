After staging, cache, merge and export consumers finish, keep disk staging records out of publication. The parent preserves exact prepared staging bytes in a separate private evidence directory, verifies every copy and its source, then detaches only those prepared copies through the current owner. Original intakes stay untouched. Record the printed evidence directory and its `retention.json` in the local retention index with its consumers, hold reason and release condition. This is evidence preservation before finalization, not permission to dispose of retained inputs. Stop on any failure; do not finalize an incomplete retention step or remove failure evidence.

```bash
"$(cat graphify-out/.graphify_python)" -E -P -B -c "
import hashlib, json, re, sys, tempfile
from pathlib import Path
from graphify.transaction import (current_transaction, pin_output, _read_managed_bytes,
    _read_relative_bytes, _replace_relative_bytes, unlink_prepared)

owner = current_transaction()
with pin_output(Path.cwd(), mutation=False) as prepared:
    names = sorted(path.name for path in Path.cwd().iterdir() if (
        re.fullmatch(r'\.graphify_(?:intake_plan|chunk)_[0-9]{2,}\.json', path.name)
        or path.name in {'.graphify_completed_chunks.json', '.graphify_dispatch_batch.txt'}))
    if names:
        storage = Path(tempfile.gettempdir()).resolve(strict=True)
        if any(storage.is_relative_to(path.resolve()) for path in
               (Path(owner.root), Path(owner.output), Path.cwd())):
            raise ValueError('staging archive must be outside corpus and public/prepared output')
        archive = Path(tempfile.mkdtemp(prefix='graphify-staging-evidence-', dir=storage))
        print('Disk staging evidence directory: ' + str(archive), file=sys.stderr)
        with pin_output(archive) as evidence:
            records, identities = {}, {}
            for name in names:
                payload, identity = _read_managed_bytes(prepared, name, 512 * 1024 * 1024)
                _replace_relative_bytes(evidence, name, payload)
                if _read_relative_bytes(evidence, name, 512 * 1024 * 1024) != payload:
                    raise ValueError('staging archive readback changed')
                records[name] = hashlib.sha256(payload).hexdigest()
                identities[name] = identity
            receipt = json.dumps({'owner': owner.id, 'prepared': str(Path.cwd()),
                'identity': evidence.identity.json(), 'files': records}, indent=2).encode('utf-8')
            _replace_relative_bytes(evidence, 'retention.json', receipt)
            if _read_relative_bytes(evidence, 'retention.json') != receipt:
                raise ValueError('staging archive receipt readback changed')
            # Verify the complete keep set before detaching any prepared copy.
            for name in names:
                payload, identity = _read_managed_bytes(prepared, name, 512 * 1024 * 1024)
                if identity != identities[name] or hashlib.sha256(payload).hexdigest() != records[name]:
                    raise ValueError('prepared staging evidence changed before retention')
            for name in names:
                unlink_prepared(owner, name)
        print('Retained disk staging evidence: ' + str(archive))
" || exit $?
```
