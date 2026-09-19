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
`codex/v8-workspace-contracts`. The initial implementation stopped at a local
diff. Subsequent operator authorization delivered commit `f02bca18` through
[PR #149](https://github.com/Villeneuve-Ventures/graphify/pull/149); the review
follow-up below records later repairs separately from that initial proof.

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

### Completed evidence for the initial candidate (`f02bca18`)

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
These machine-local artifacts are historical diagnostics, not portable review
attachments or certified release inputs. They may disappear after local cleanup.

The initial candidate completed its required S2 proof. S3/S4 operational behavior and S7 committed
whole-candidate/native lifecycle proof remain later work, as do the stated D3
compatibility decision and real installation/adoption boundaries.

### PR review follow-up

The review snapshot covered all five conversation comments, three submitted
reviews, and 15 inline threads on PR #149 at `f02bca18`, including the schema
mapping suggestion nested in the CodeRabbit review. Duplicate summaries were
evaluated with their corresponding inline findings.

| Finding | Disposition |
|---|---|
| Unbounded wheel expansion | Accepted: validate the complete namespace, member sizes and aggregate expansion before decompression; bound actual reads. |
| Partial fixture writes prevent retry | Accepted: write and close the complete fixture in private sibling staging, then publish without replacing an existing destination. Failure cleanup never recursively removes the public output. Hard-crash durability remains outside this local fixture helper. |
| Surrogates escape query refusal | Accepted: question and context-filter encoding failures now raise `QueryRejected`. |
| Installed-member filesystem races escape refusal | Accepted: member stat/read `OSError` now raises `WorkspaceAuthorityInvalid`; other errors retain their meaning. |
| Cold-import test has no timeout | Accepted: add a 30-second subprocess timeout. |
| Schema/document positional coupling | Accepted: choose documents by schema filename and check complete coverage. |
| Three descriptor-leak reports | Not reproduced: each successful open is tracked and closed by the existing `finally` block. Wrapping directory descriptors in `os.fdopen` would not preserve this implementation. |
| Three protocol ellipsis reports | No change: ellipses are intentional typed `Protocol` method declarations. |
| Two mixed-import reports | No change: tests need the module object to monkeypatch module-level bindings. `MAX_ENTRIES` is not an `InputManifest` class attribute; the suggested replacement is incorrect. |
| Normal wheel rejected due to Requires-Python order | Not reproduced: the actual built wheel emits `==3.14.*,>=3.14.2`, matching the existing check. Source declaration order does not determine emitted metadata order. |
| Missing protected acceptance packet | Not applicable: no authorized source designated S2 protected. Review findings cannot activate that policy. Portable reproduction guidance is supplied below without inventing a protected packet. |
| Default docstring coverage warning | No bulk changes: this is a bot advisory, not a configured repository gate or a demonstrated missing behavioral contract. |

The earlier full-suite totals apply to `f02bca18`, not to these modified files.
The operator's instruction not to rerun pytest remains in force. New focused
stdlib-unit regressions cover UTF-8 refusal and installed-member races; wheel
resource and failed-publication regressions are added for subsequent CI execution.
The follow-up uses scoped static checks and new isolated reproductions; it does
not claim a new full-suite pass or whole-candidate certification.

Follow-up evidence: six new stdlib unittest tests passed, covering both surrogate
fields, valid multibyte text, installed-member stat/read races, normal success,
specific hash-mismatch refusal, and propagation of unrelated errors. New isolated
wheel probes passed pre-decompression rejection for package/metadata/license
sizes, aggregate expansion and unexpected namespace; write-error/interruption
retry; and preservation of racing empty/populated public destinations. A fresh
wheel produced a source-matching fixture with every archive member/hash checked.
Scoped Ruff passed and Pyright reported zero errors using the repository's venv
interpreter. An initial Pyright invocation without that interpreter could not
resolve `packaging`; correcting the invocation resolved it without source or
dependency changes. AST graph update completed with 13,521 nodes / 30,087 edges.

The same independent reviewer identified a cleanup race in the first repair.
Private staging and no-replace publication replaced that repair; the focused
correction review returned CLEAN. Publication reuses the existing native
no-replace primitive on macOS/Linux and Windows rename semantics on Windows.
Only macOS was exercised here; Linux/Windows branches remain untested locally,
and unsupported POSIX publication fails closed. No fsync/crash-durability claim
is added to this local fixture builder.

Portable reproduction starts from the checked-out PR revision and the committed
lockfile. These commands are guidance for reviewers; listing them does not claim
they were rerun during the follow-up:

```sh
uv sync --all-extras --frozen
uv run --frozen python -B -m unittest tests.test_workspace_review_regressions -v
# When pytest execution is authorized:
uv run --frozen pytest tests/test_workspace_contracts.py tests/test_workspace_composition.py tests/test_wheel_packaging.py -q
uv run --frozen pytest tests/ -q --tb=short
```

The packaging tests build a fresh wheel, verify its full namespace, create
external local fixtures, and exercise disposable installation and tamper refusal.
They avoid requiring another reviewer's machine-local wheel or temporary paths.
The committed CI workflow specifies the remaining checks, and GitHub run results
must be inspected separately before making remote-CI claims.

### Second review follow-up (`51b830ba` input)

The refreshed snapshot contains 11 conversation comments, five reviews and 18
inline threads. Prior dispositions still apply; the repeated Python-specifier
order and protected-packet claims supply no new supporting evidence. Five new
findings are addressed:

- Fixture inputs are opened as descriptors, checked against their named identity
  before and after bounded reads, and reject regular-file/symlink substitutions,
  non-regular inputs and changes during capture. Available no-follow/nonblocking
  flags are used without introducing a new platform restriction.
- POSIX publication revalidates the pinned parent before and after rename.
  A detached parent cannot report successful publication at the requested path;
  a completed artifact in the detached directory is preserved on refusal.
- Installed script paths and their allowed directory use consistent resolved
  identities, accepting legitimate symlinked installation ancestors.
- Current-interpreter bytecode caches are compared with freshly compiled verified
  source, including normal timestamp/hash caches, optimization levels and the
  active external cache prefix. The admitting process never unmarshals caches;
  a disposable isolated interpreter compares every serialized code field without
  executing cached code. Comparison preserves constant types, values and float
  bits, but excludes interpreter interning/reference-sharing history. Ordinary
  same-source compilation was observed to vary immutable constant sharing;
  exact object-identity equivalence is not claimed. Nonstandard compiler/filename
  payloads refuse. Transport preserves decomposed Unicode filename spellings.
  Input and wall limits apply on all hosts, CPU limits on POSIX, and an
  address-space cap on non-Darwin POSIX. This is not an OS sandbox; Darwin and
  Windows do not have the address-space cap.
  This is observed on-disk evidence, not proof of already imported modules,
  custom import hooks, a hostile interpreter, or subsequent concurrent writes.
- Completion construction requires a `CompatibilityManifest`; an unrelated
  document with a `sha256` property can no longer supply the candidate binding.

All 31 focused stdlib unittest tests across the three review-regression modules
passed, including the six earlier refusal tests affected by the installed-check
changes. A fresh wheel passed source/fixture archive checks, valid completion
binding, disposable installation, and installed verification both without caches
and after compiling the full package at optimization levels 0, 1 and 2.
The full-package probe first exposed a false rejection from comparing raw marshal
bytes: string interning changes serialization without changing executable code.
Full code-field comparison corrected it; regressions cover the actual
`transaction.py` module and altered stack size, exception table and line metadata.
Independent review identified Unicode normalization in the child transport;
preserving exact filename strings corrected it. Its proposed constant-sharing
check was narrowed after ordinary same-source compilation demonstrated varying
sharing; the documented field/value equivalence contract covers that boundary.
The focused correction rereview returned CLEAN. Scoped Ruff and interpreter-pinned
Pyright passed, and the final AST graph update completed with 13,566 nodes and
30,233 edges (the same five zero-node JSON warnings remain).
Pytest remains unrun under the operator's instruction. Native Linux/Windows
validation and fresh full-suite proof are not claimed.

### Third review follow-up (`6ae7ff7f` input)

The current 24-thread snapshot added four materially related trust-boundary
claims. Three are accepted and repaired in this follow-up:

- Installed identity now compares the wheel-derived member set with the actual
  installed package tree. Unrecorded regular files and non-regular entries
  refuse, while only verified selected caches and canonical cache names that are
  inert under an external `sys.pycache_prefix` are admitted. A regression covers
  an unrecorded package that would otherwise shadow a recorded module.
- Completion binding, source observation and structural build construction now
  require actual validated `InputManifest` instances instead of accepting
  duck-typed objects with caller-chosen digests or completion flags.
- Fixture creation parses `WHEEL` before publication and requires the supported
  `Wheel-Version: 1.0`, pure-Python root and `py3-none-any` compatibility tag.
  Malformed, unsupported-version, non-purelib and incompatible-tag fixtures
  refuse without public output.

The constant-reference-sharing report is factually demonstrated by the existing
identity-sensitive marker regression, but it duplicates the explicitly stated
field/value-equivalence limit above. Requiring complete reference-topology
equivalence can reject ordinary same-source caches, so changing that admission
policy needs an explicit contract decision; this follow-up does not silently
broaden the promise or claim observational bytecode equivalence.

Focused validation after these repairs: all 16 installed-identity unittests,
55 contract/composition pytest cases and 154 wheel-packaging pytest cases passed.
Scoped Ruff and Pyright passed with zero findings. No full pytest run, native
Linux/Windows claim, or new security-enforcement gate is added.

### Fourth review follow-up (`2fe1e91f` input)

Five new current-head findings were accepted as incomplete evidence binding and
repaired without expanding S2 into operational certification:

- Installed members and selected caches are captured through bounded no-follow
  descriptors. Their exact filesystem identities remain attached to the bytes
  that were hashed or compared and are revalidated in the final package-tree
  pass, so a replacement during admission refuses. Writes after the completed
  observation remain outside the stated guarantee.
- Contract-bearing constructors require exact concrete manifest, authority,
  compatibility, tuple and runtime-input types. Subclasses cannot override
  `to_dict`, `complete` or `sha256` after presenting valid canonical bytes.
- Core `METADATA` uses the validating `packaging.metadata` parser, rejecting
  malformed or duplicate singleton fields. Project name/version must remain the
  supported S2 identity, while wheel name, version and normalized Python
  specifiers must match the captured project declaration.
- RECORD-listed console launchers must resolve strictly to stable regular files
  in the active scripts directory. Their generated bytes and execution remain
  outside compatibility identity and runtime certification.

Focused validation after these repairs: all 18 installed-identity unittests,
55 contract/composition pytest cases and 160 wheel-packaging pytest cases passed.
Scoped Ruff and Pyright passed with zero findings. The complete test suite was
not rerun because these localized regressions and affected modules cover the
demonstrated failures; no unrelated gate or policy was activated.

### Fifth review follow-up (`8c4d9ffb` input)

The GitHub Actions test job reached the end of the suite with 5,975 passing and
five failing assertions. All failures were stale review-regression mocks that
still intercepted the pre-descriptor `Path` APIs. They now inject failures at
the active `os.open`/`os.read` boundary and retain the check that unrelated
programming errors are not translated.

Six current-head findings were accepted and repaired within S2:

- bytecode-cache disappearance and permission races now use the documented
  `WorkspaceAuthorityInvalid` refusal while initial absence remains permitted;
- project dependency containers and requirement strings are validated before
  comparison, with parser failures translated to `ContractError`;
- every detection-phase code input must have regular-file identity evidence;
  incomplete consumed manifests may still honestly record unprocessed inputs;
- wheel `RECORD` must enumerate the accepted archive exactly, with correct
  SHA-256 hashes and sizes and the required empty self-entry;
- the PEP 427 filename must bind the captured project name/version, reject a
  build tag and carry exactly the supported `py3-none-any` tag; and
- the stale descriptor tests noted above were updated without weakening their
  refusal or exception-boundary assertions.

Focused validation after these repairs: 172 wheel-packaging tests, 63
contract/composition/review-regression tests with 11 subtests, and all 19
installed-identity unittests passed. Scoped Ruff, Pyright and diff checks passed.
The failed CI run already supplied the broader evidence: 5,975 other tests
passed before the stale-test failure was reported, so no redundant local full
suite was added.

Out-of-scope baseline follow-ups are tracked separately:
[XML admission #150](https://github.com/Villeneuve-Ventures/graphify/issues/150)
and [dependency maintenance #151](https://github.com/Villeneuve-Ventures/graphify/issues/151).
The existing [security baseline #113](https://github.com/Villeneuve-Ventures/graphify/issues/113)
remains the umbrella for contextual hash/bind-address dispositions and any future
enforcement policy. No duplicate umbrella issue, blanket suppression, public-bind
behavior change, dependency update or S3 implementation is included here.

### Sixth review follow-up (`77478f6f` input)

Nine new current-head comments were reviewed against the implementation. Eight
were valid S2 admission gaps and were repaired:

- authority ancestors now retain their owner/mode policy through the final
  descriptor-relative recheck, including the state root's exact owned `0700`;
- bytecode-cache lookup failures other than ordinary absence now use the
  documented authority refusal type;
- the checked `pyproject.toml` bytes are bound to the source inventory and reused
  for both package selection and project metadata, while corrupt archive and
  decompression failures are translated to `ContractError`;
- detection-phase code inputs require a full six-field regular-file probe;
  directory membership alone no longer supplies admission identity;
- installed `METADATA` and `RECORD` are bounded before `importlib.metadata`
  parses them, expected metadata identities are retained to the final admission
  point, and every verified package member must still be present in the final
  tree walk.

The common cause was incomplete end-to-end preservation of evidence: the code
captured a safe identity or policy at one boundary but discarded or weakened it
before the final success decision. The remaining console-launcher-content report
is a valid limitation, not an S2 defect: this result already states that generated
launcher bytes and execution are outside compatibility identity, and the design's
S7 exact-candidate packaging and aggregate proof owns that later certification.
It therefore does not need a separate GitHub issue unless S7 itself is moved from
the repository design into issue-based scheduling.

Focused validation after these repairs: 88 contract/composition/review and
installed-identity tests plus 46 subtests, and 174 wheel-packaging tests passed.
The wheel suite includes the disposable noneditable installation/authority smoke.
Scoped Ruff and Pyright passed with zero findings. The current pre-repair head was
already green in GitHub Actions; no unrelated full-suite rerun or additional gate
was added for these localized changes.
