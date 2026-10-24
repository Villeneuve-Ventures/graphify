# Pyright baseline repair (#223)

Base: `e57f699cf2eb958f5ddd605a26b696df96c63780` on `v8`.
The bounded Pyright check passes with no configuration changes or suppressions.
Keep #147 open. This repair adds no continuity or rebinding authority.

## Contracts and changes

Pyright 1.1.409 reproduced exactly two errors and zero warnings before editing:
`TextIO.reconfigure` attribute access and the corpus directory generator's return
annotation. The CLI now uses `getattr` inside the existing `hasattr` guard and
exception handler. Custom streams remain supported. UTF-8 encoding, replacement
errors, absent streams, missing/noncallable methods and method exceptions retain
their handling. No stream is wrapped or replaced.

The generator declares both concrete directory types it yields. Its observation
helpers and the transaction-record parser accept that union where they read
path, identity or scoped metadata. Two internal assertions narrow native
descriptor access after the scoped-reader branch has returned. They confer no
authority. Writer APIs and their runtime type checks remain unchanged.
`_ScopedCorpusDirectory` remains a separate observation-only class without `fd`;
it is neither an `OutputCapability` subclass nor cast to one.

## Local validation

Environment: macOS 27.0.1 (26A434), ARM64, CPython 3.14.8, Git 2.55.0,
Pyright 1.1.409. Dependencies were installed in a fresh project environment with
`uv sync --frozen --all-extras`; `pyproject.toml` and `uv.lock` remain exact.

| Check | Result |
| --- | --- |
| Explicit-interpreter Pyright on four product files | Two errors before; zero errors/warnings/information messages after |
| Final Pyright including both new test modules | Zero errors/warnings/information messages |
| Focused CLI, corpus, engine, restore/root/takeover, authority, qualification-runner and README tests | 649 passed; one default A14 opt-out skip |
| Four repository pytest shards | 11,019 passed, 60 skipped, 235 subtests passed; zero failures |
| Final eight new behavior cases | Passed again after test-only typing refinements |
| Optimized-Python corpus cases | Two passed; expected pytest warning about disabled non-test assertions |
| Ruff, whitespace, five generated-artifact gates | Passed |
| Installed CLI help/default-skill smoke | Passed with a disposable home |

The product-file Pyright command, run from the checkout root, was:

```text
.venv/bin/pyright --pythonpath .venv/bin/python graphify/__main__.py graphify/transaction.py graphify/checkpoint_restore.py graphify/recovery_authority.py
```

The final check adds `tests/test_cli_streams.py` and
`tests/test_corpus_directory_capabilities.py`. This is a six-file check, not a
full-repository Pyright result. Eight new behavior cases also passed before the
product edits. They cover actual UTF-8 output, custom/unsupported/failing stream
reconfiguration, both corpus readers, descriptor closure, marker reads, file
preservation and runtime refusal of a scoped reader as a writer root capability.

The full suite used `uv run --frozen --no-sync python -m pytest` with
`-p tools.pytest_partition -p tools.pytest_timings`, `--ci-shard=N` for all four
shards, `--ci-timing-json=PATH`, `tests/ -q --tb=short`, and separate fixture roots.
`PYTHONHASHSEED=0` was fixed. Dependencies had already been synchronized.
The four complete selected inventories are disjoint and equal the same full
**11,079-item** collection. The slowest shard took **3,536.92 seconds (58m56s)**;
repeated rollback fixture setups took about 35–36 seconds each. The other shards
took 22m07s–22m49s. Four expected cache-scope/preservation warnings were reported.
The earlier focused run also reported three existing Jieba invalid-escape warnings.

Product source stayed fixed through the full suite. Subsequent changes only
narrowed the new test doubles and made their intentional invalid-type call
dynamic. Both changed test modules were rerun, along with Pyright and Ruff;
passing evidence for the unchanged product and remaining tests was retained.

Initial noncanonical invocations are retained as failures: xdist first rejected
unordered worker collection; a fixed-seed xdist run then reported seven bootstrap
selection failures without the active `VIRTUAL_ENV`, plus unittest-subtest
serialization failures for enums, classes and functions. The seven bootstrap
cases pass through `uv run`; the affected workspace modules pass without xdist
(51 tests and 139 subtests). No test, guard or dependency was weakened to obtain
the complete repository-shard result above.

## Native requalification decision

**No new privileged A14 run is required for this bounded repair.** Capture,
preview, restore, root validation, publication, admission, recovery, permission
checks and writer APIs are unchanged. An annotation-erased AST comparison of
`transaction.py` against the base is identical after excluding only the two
added descriptor-type assertions. Valid native and scoped paths satisfy those
invariants. The optimized-Python checks also preserve writer refusal when those
assertions are removed. The stream change retains attribute lookup and error
handling at the existing boundary.

This decision claims no installed, source-matched native A14 pass for this
candidate. The ordinary A14 row stayed disabled. No native guest, protected
authority store or prior qualification fixture was modified. Material authority,
runtime or provisioning changes still need new relevant proof under the
[qualification guide](../checkpoint-qualification.md).

The [root-lifecycle receipt](issue147-root-lifecycle-20261024.md) remains bound to
`f96bc70d50cc38c18c671a0b0cd5228ab470d081`. The
[binding-fix receipt](issue147-restore-binding-20261024.md),
[earlier requalification](issue147-a14-requalification-20261024.md) and
[initial receipt](issue147-a14-20261024.md) retain their original source revisions,
wheel identities and evidence. Committed prior receipt/report bytes remain exact;
their historical results do not qualify this changed candidate.

## Private AST refresh and limits

The public `graphify update .` path completed against a new private output outside
Git with the freshly bound project interpreter. The final graph contains
**17,900 nodes, 46,312 edges and 982 communities**, with zero extraction input/output
tokens. The initial private refresh is also retained. Existing graphs were not
refresh targets. The report's Git stamp is the base commit because the refresh
preceded commit; the local source-hash manifest binds its dirty candidate inputs.

HTML was skipped at the 5,000-node guard. Eight source files produced no nodes;
optional learning-sidecar admission reported an I/O failure and was skipped.
The managed graph and report were published and the command exited zero.
No provider or semantic extraction was used.

No physical-continuity, historical-output enrollment, in-place identity-rebinding,
arbitrary-writer fencing or power-loss claim is added. #147 remains open.
Delivery is one draft PR to `v8`. CI and reviews are not awaited.
