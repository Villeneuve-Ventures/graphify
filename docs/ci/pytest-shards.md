# Pytest CI shards

The Ubuntu test matrix runs four deterministic whole-module shards, with
`fail-fast: false`. Each process collects the entire normal `tests/` suite before
selecting items. Collection failures remain fatal even in modules owned by another
shard. Loading `tools.pytest_partition` without `--ci-shard` leaves selection inert.
Discovery filters, partial paths, last-failed/stepwise selection and distributed
execution are rejected in shard mode. New and nested modules use a stable SHA-256
path hash fallback; they are never silently omitted. Ordinary local pytest needs
no shard option.

## Measured allocation

`tools/pytest_shards.json` binds the allocation to the successful Linux run
[36871547583](https://github.com/Villeneuve-Ventures/graphify/actions/runs/36871547583),
on revision `6dcd6941ec47a5e5d83044a2ac702dc2d3a6c984`. The preserved archive's
SHA-256 is `ca30c8b93af204818b8cf4d08b073ff2582f3c59d7f30c6ab99e023ef9991967`;
its `timings.json` hash is
`6bbcb47b2c7a1e2a03c01e769130b17b54903e799f7d095fb023cc2a03ffa9d1`.
The sample completed 7,021 items in 229 modules using Python 3.14.7, pytest 9.0.3
and Git 2.55.0, with zero collection errors or deselection. Module phase totals
sum to 2,599.339259 seconds; collection took 13.003818 seconds.

To reproduce the allocation from those exact preserved bytes:

```sh
uv run --frozen python -m tools.ci_shard_allocation /path/to/timings.json --check
```

The generator checks successful completion, inventory counts, finite phase totals,
source/environment bindings and immutable baseline file hashes. It sorts descending
measured module totals (integer microseconds), breaks ties by ascending POSIX path,
and assigns each module to the lowest current load, breaking load ties by ascending
shard number. The committed measured loads are:

| Shard | Predicted module seconds |
| --- | ---: |
| 1 | 649.834884 |
| 2 | 649.834632 |
| 3 | 649.834859 |
| 4 | 649.834884 |

These are estimates from one serial Linux cohort. Additional tests and unmeasured
modules use their assigned owners without invented weights. Collection, dependency
installation and pinned Git compilation repeat on each runner; hosted speedup is
unproven until actual shard timings exist. The older sample is not used for weights.

## Execution gate and diagnostics

The stable `Python quality gate` runs after the matrix using `always()` while
preserving the draft-PR condition. It requires a successful matrix conclusion plus
four separate required execution receipts, each containing its shard, pytest step
outcome, job status, source SHA, run ID and attempt. Every expected receipt must
report success. Missing, duplicate, unexpected, failed, cancelled, skipped or
mismatched executions fail closed. Partial reruns must supply all four receipts
for the same attempt; an earlier attempt cannot satisfy the gate.

Matrix outputs can overwrite one another and a single rollup does not enumerate
children. Separate immutable, uniquely named artifacts prove each execution.
[The v4 artifact migration guidance](https://github.com/actions/download-artifact/blob/main/docs/MIGRATION.md)
documents unique matrix artifact names and pattern downloads. Downloads keep
artifacts in distinct named directories (`merge-multiple: false`); the gate checks
the exact four expected paths and payloads. A failed receipt upload fails the
matrix child and cannot produce a successful gate. Whole-run cancellation also
has an explicit failing gate step.

After accepting the receipts, the gate installs its own acceptance-pinned Git
2.55.0 before optimized-Python protected-verifier conformance. Its installation
smoke check invokes the installed `graphify` console launcher for both `--help`
and `install` in a disposable process-scoped home.
It never reruns the full suite. Skill generation, advisory security scans and the
macOS Leiden job retain their policies.

Timing uploads remain advisory and run after failed tests. The `timings.json`
artifact includes full and selected node/module inventories, counts, shard and
allocation hash. Console output uses `CI_TIMING_SUMMARY=` for a fixed set of
counts, status, shard and timings; it does not print inventories, module lists or
environment data. This avoids sending potentially huge parameter-derived node
IDs through the runner's log processing. Read the artifact for the full record;
the former full-record `CI_TIMING_JSON=` console line is no longer emitted. Only
validated shard deselection is eligible for complete timing telemetry; arbitrary
selection, failed/partial execution, collection errors, invalid reports and output
failures retain incomplete diagnostics. Timing writes and pytest 9 subtest handling
never change pytest's exit result. Execution receipts do not depend on timing files.
