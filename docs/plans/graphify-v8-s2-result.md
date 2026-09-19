# S2 structural contracts and candidate composition

This task implements S2 as a local diff based on `v8` commit
`7a6f667acf805b34e28f1658d9c69de16a282892`. S1 is merged through
[PR #145](https://github.com/Villeneuve-Ventures/graphify/pull/145), merge commit
`14934e8ca263ae876724abdd2a83342a87f05cda`. The published design and consolidation
plan retain their historical snapshot claims. This result supersedes their
implementation status only for S1 and this bounded S2 task.

The donor `workspace/v1` reference was inspected at
`01ad5a3ca345da879c2f0c35161c4c185eb30d66`; no donor engine, store or semantic
implementation was copied. The adapter query bounds and canonical encoding
conventions were adapted. The implementation worktree is
`/Users/lisrel.claw/graphify-v8-workspace-contracts`, branch
`codex/v8-workspace-contracts`. No commit, push or PR is part of this task.

## Contract decisions

- Distribution and ordinary package version remain `graphifyy==0.10.0`.
  Python requirements, entry points and optional extras are unchanged.
- Adapter contract **2**, state schema **2**, extractor/cache ABI
  `graphify-v8-structural-1`, detector
  `graphify-v8/workspace-observer-v2`, input-manifest format **1**, and directed
  graph payload format **1** identify the new interpretation. The exact baseline
  is the verified destination commit above; it is provenance, not a final S2 commit.
- Compatibility document schema **2** identifies only a `local-fixture`, with
  `certified=false` and `fixture:sha256:<source-inventory-digest>` build identity.
  Its wheel digest, package members and immutable distribution metadata are part
  of exact equality. There is no version-range or future-version fallback.
- Runtime-authority envelope **2** is deliberate: its explicit structural policy
  adds capacity limits to queue limits, so the donor v1 shape is not reused.
  No operational defaults exist. Tests use queue limits `8/16384/1` and explicit
  generation/payload bounds only in disposable fixtures.
- Selection accepts the exact explicitly expected fixture only for **PROBE**.
  It returns neither an executable adapter nor promotion permission. Other
  intents refuse. A structural composition validates inputs without touching
  disk; requesting a runtime explicitly refuses until S3/S4 exist.

## S1 evidence mapping

`InputManifest.from_engine` snapshots an open `SourceIO` context. The wire data
retains S1's sorted `(operation, path)` records: three-integer directory bindings,
six-integer regular-file identities plus SHA-256 for reads, present/absent probes,
and sorted directory names with type/mode/device/inode bindings. Reads require
matching probes; traversed directories and explicit root bindings must exist;
shared bindings must agree. Atime is absent, as in S1. Root labels are explicitly
limited to source, Git and policy. Absolute/scratch paths, traversal, alias labels,
duplicate records and noncanonical filesystem labels refuse before any reopen.

The hard ceiling is 100,000 evidence units (records plus directory members),
64 MiB per consumed file, 512 MiB aggregate unique consumed bytes and 16 MiB
canonical document bytes. UTF-8 labels are bounded to 4096 bytes. S1 enforces
the live repeated-read budget; a retained manifest cannot reconstruct the number
of repeated reads from its deduplicated evidence. Limits can only be tightened
by the scoped reader. Canonical JSON uses NFC, sorted keys and a final newline,
rejecting floats, duplicate keys, surrogates and excessive nesting.

Initial detection has no extraction outcomes. Consumed-input manifests require
exactly one disposition per admitted code input. `empty` is successful extraction;
`not_processed`, missing parsers, unsupported dispatch and read/enumeration failures
are incomplete. S1 resolver-wide failure is retained separately even when every
per-input extraction succeeded. Failure details are omitted because they can
contain absolute source or scratch paths; bounded S1 status codes remain.

`CompletionBinding.bind` records distinct initial and consumed digests together
with graph and compatibility digests. It requires complete extraction, identical
admitted code/root sets, and all initial evidence unchanged in the final manifest.
This is a pure staged-completion contract, not persistence or certification.
Two-sided observations, actual descriptor/fence validation and sealed payload
verification still belong to S3/S4. Freshness remains observed-current, with no
atomic-snapshot or inter-observation ABA guarantee.

## Authority and fixture boundary

The explicit authority loader walks existing directories without following links,
requires an owned 0700 state root and owned singular regular 0600 authority file,
performs bounded canonical decoding, and compares the entire explicit candidate
and installed noneditable package bytes/metadata. It creates or repairs nothing.
It does not infer state roots from HOME, XDG, cwd or source text. Read-only
descriptor inspection is portable where its primitives exist; lifecycle mutation
support remains unimplemented and is not expanded beyond the design's later
non-elevated macOS/local-APFS boundary.

`tools.workspace_artifacts.candidate.build_fixture` inventories tracked and
unignored untracked input files, including explicit deletions, and checks the
wheel against complete intended package/data members. It validates the captured
wheel bytes, complete wheel namespace, entry points, Python requirement, optional
extras, dependencies and license. The installed verifier independently checks
distribution metadata and refuses unexpected recorded installation members.
The builder rechecks source inputs before writing to a new external output root.
Two runs with identical inputs and the same wheel produce identical fixture files.

The fixture bundle includes schemas, contract test sources and canonical source,
compatibility and runtime documents. It does not supply release provenance/SBOM,
production installers, offline compensation, signed trust or a certified runtime.
Replacement of both local authority and its external expected identity by the
same user is outside the tamper claim. The wheel hash covers the wheel archive;
installed verification covers package and immutable metadata bytes, not a
reconstruction of the installed wheel archive.

## Validation and remaining dependency

S2 is implemented and locally validated at the authorized local-diff boundary. This
host is macOS 27.0 build 26A428, CPython 3.14.3, Git 2.55.0 and uv 0.11.30.
Installation/authority smoke uses disposable absolute HOME/XDG_STATE_HOME/
CODEX_HOME and no provider credentials. Such fixtures are not native APFS
lifecycle/durability qualification. Final whole-candidate certification remains S7.

S3 is the next dependency: lifecycle/storage guards, denial-only state sentinel
installation, exact staged binding persistence, policy enforcement and ownership.
S4 supplies the operational adapter. S5 commands, S6/S7 delivery, D1 security
enforcement, D2 semantic release, D3 legacy-state policy and consumer adoption
remain outside S2. Real state, retained graph outputs and other worktrees remain
untouched.

### Changed-file index

| Files | Purpose |
|---|---|
| `graphify/workspace/__init__.py`, `contracts.py` | Lazy package and immutable canonical compatibility/input/completion/state-root models |
| `graphify/workspace/adapters/__init__.py`, `base.py` | Exact PROBE-only selection, bounded query and engine-neutral protocol types |
| `graphify/workspace/composition.py` | Explicit policy, existing-only authority inspection, installed identity verification and pure composition |
| `graphify/workspace/schemas/{compatibility,input-manifest,completion-binding,runtime-authority,state-root}.schema.json` | Five versioned wire shapes with model-enforced cross-field invariants |
| `tools/workspace_artifacts/__init__.py`, `candidate.py` | Whole-wheel member checks and content-derived local fixture bundles |
| `pyproject.toml` | Two packages and schema data; no dependency, script, version or Python requirement change |
| `tests/test_workspace_contracts.py`, `tests/test_workspace_composition.py`, `tests/test_wheel_packaging.py` | Evidence/schema/refusal/import/authority/package/installation/tamper proof |
| This document | Task-owned current progress and evidence; historical plans preserved |

### Completed evidence

- Final full serial gate, `uv run --frozen pytest tests/ -q --tb=short`:
  **5,918 passed, 42 skipped**, five warnings, **254.32 seconds**. All-extra frozen
  dependencies were installed before testing. No individual-test timing profile
  was collected, so no module-level bottleneck conclusion is claimed.
- Focused S2 contracts/composition/packaging: **194 passed**. Representative
  detection/hook/watch/package checks after correcting the test environment:
  **351 passed**.
- Optimized-Python protected verifier: **80 passed**. This preserves the current
  CI check without designating S2 protected or invoking protected review.
- Pinned PR-Agent `f3b385ea2927247ddcff2fe252472380b9c8f5fc`, installed in a disposable
  environment: **22 passed**, offline and without provider credentials.
- All five generated-skill checks passed: generated output, coverage,
  schema singleton, monolith round trip and always-on round trip.
- Whole-wheel members, same-input fixture reproducibility, disposable
  noneditable installation/help/Codex skill install, cold imports, canonical
  runtime authority and package/metadata tamper refusal passed.
- Native macOS Leiden weighted-partition smoke passed. Scoped Pyright reported
  **0 errors**; scoped Ruff passed; tracked/untracked whitespace, Python syntax
  and JSON checks passed.
- Advisory scoped Bandit reported **six low-severity subprocess warnings** at
  the three argument-list Git calls in fixture tooling, no medium/high findings.
  No security enforcement or dependency-audit policy was changed.
- One independent native review found two packaging issues: wheel hash and
  validation used different reads, and unexpected executable wheel members were
  accepted. Both were repaired; the same reviewer verified the focused repairs
  as CLEAN with six regression tests. The implementation owner reviewed the
  complete integrated diff. No additional reviewer or durable workflow ran.
- Task-owned `graphify update . --no-cluster` succeeded in **6.94 seconds**:
  **13,501 nodes / 30,026 edges**, including the S2 modules. It reported five
  zero-node JSON corpus fixtures. This ordinary AST output is not workspace
  certification. Retained outputs and their separate #147 recovery issue were
  untouched.

Two preliminary aggregate runs were invalidated by the test environment, with
no source changes between runs. First, removing TMPDIR from the isolated process
environment sent fixtures to `/private/tmp`; group 0 was inherited and macOS
cleared setgid immediately on chmod. The unchanged hook-permission test failed
on both S2 and the pinned base (5,917 other tests passed). Second, selecting a
temporary directory under `.venv` preserved setgid but activated existing source
exclusions and correctly violated the candidate builder's outside-checkout output
rule (211 failures). The final environment used a fresh private directory under
the user's native temporary root, outside the checkout, with the user's group.
The 351-test focused check passed before the successful final aggregate run.
No test was weakened, skipped or changed to hide either setup problem.

The local proof directory is `/private/tmp/graphify-s2-proof-zkaol32b`:
`logs/full-pytest-native-tmp.log` is the final aggregate record; the earlier logs
and pinned-base reproduction are retained. `review.diff` includes all 17 changed
files, including new untracked files. `wheel/` contains the final wheel, and
`fixture/` contains its canonical local identity and `fixture-bundle.zip`.
Neither these artifacts nor the uncommitted branch are certified release inputs.

No required S2 proof remains open. S3/S4 operational behavior and S7 committed
whole-candidate/native lifecycle proof remain later work, as do the stated D3
compatibility decision and real installation/adoption boundaries.
