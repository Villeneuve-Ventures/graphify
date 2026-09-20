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
matching probes; successfully traversed directories and explicit root bindings must exist;
shared bindings must agree. Atime is absent, as in S1. Root labels are explicitly
limited to source, Git and policy. Absolute/scratch paths, traversal, alias labels,
duplicate records and noncanonical filesystem labels refuse before any reopen.
Negative probes may stop at an absent or non-directory ancestor, for which S1
emits no directory binding. Positive probes, reads, lists and recorded directory
bindings still require their full traversed ancestry.

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

### Seventh review follow-up (`c53ec6ea` input)

Six new current-head threads were valid and repaired. Two code-quality findings
rename nested test-method receivers without changing behavior. Four behavioral
findings close remaining fixture and installed-cache refusal gaps:

- selected bytecode caches now retain both their named and resolved paths plus
  exact identities, including external `sys.pycache_prefix` entries, and are
  revalidated after the package-tree walk before admission succeeds;
- setuptools package selection validates required containers, canonical dotted
  package names, package-data ownership and non-escaping relative patterns before
  globbing, so malformed configuration refuses with `ContractError`;
- the exact workspace-test set and captured bytes are reread before publication,
  including tests omitted from Git's tracked/unignored source inventory; and
- unsupported ZIP compression now joins corrupt/decompression failures at the
  fixture API's `ContractError` boundary.

These findings extend the same central issue as the prior follow-up: every input
or executable artifact used for admission must retain its authoritative identity
through the final success decision. All six are in S2 scope; there is no justified
out-of-scope follow-up and no new GitHub issue is required.

Focused validation after these repairs: 209 installed-identity and wheel-packaging
tests plus 35 subtests passed; the overlapping contract/composition/review and
installed-identity selection passed 89 tests plus 46 subtests. Scoped Ruff and
Pyright passed with zero findings, and `git diff --check` passed. No unrelated
full-suite rerun or additional gate was added.

### Consolidated review (`cf8ad45a` input)

This pass read all 36 conversation comments, 21 review bodies (including nested
and outside-diff findings), and 59 inline threads. It checked claims against the
current implementation, rather than treating unresolved GitHub threads or bot
severity labels as current defects. Prior test counts above are historical
receipts for their named inputs, not fresh proof of the current candidate.

The recurring causes are incomplete preservation of captured evidence and
producer/verifier disagreement about the supported artifact. The source-mode
finding below also demonstrates why reviewing only newly added inline comments
misses valid older feedback. Additional churn comes from duplicate style reports,
old reviewed-commit summaries, and PR-Agent treating explicitly deferred tickets
as mandatory S2 acceptance. None creates a new gate.

| Claim group | Current disposition and evidence |
|---|---|
| [Missing declared launchers](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055531571) | Valid, repaired. The installed verifier requires both supported logical launcher names in RECORD, checks regular-file presence, and retains their captured identities through its final recheck. Tests remove rows alone, rows plus files, and a launcher after package verification. |
| [Builder accepts unsupported scripts](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055531577) | Valid, repaired. Builder and verifier share `SUPPORTED_CONSOLE_SCRIPTS`; the builder requires the exact supported name set and matching case-preserved entry-point metadata. Malformed entry-point text exposed raw parser/encoding exceptions during reproduction; those now use `ContractError`. |
| [Nonexistent configured packages](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055531581), [symlinked source ancestors](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055531588) | Valid, repaired at the shared source-capture boundary. Configured directories must exist, and ancestors beneath the resolved checkout must remain real directories with the same observed identities around each capture. Ordinary aliases above the checkout and external wheel paths remain supported. This is not an atomic filesystem snapshot or an ABA guarantee. |
| [Wrong top-level namespace](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055531592) | Valid, repaired. `top_level.txt` must declare exactly the configured package roots; a tampered value with an otherwise correct RECORD refuses before publication. |
| [Noncanonical Git paths](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055531594) | Valid, repaired. Git paths must decode as UTF-8 and pass the existing canonical source-label contract before capture. NFC conversion cannot silently change an inventory key. |
| CodeRabbit outside-diff review: source hash and mode may describe different files | Valid older finding, still present at the review input and repaired here. `_capture` returns bytes plus the validated descriptor stat; `source_manifest` uses that stat's mode. A replacement after descriptor capture cannot combine the old hash with the new file's mode. Two existing race mocks now intercept this capture boundary. |
| [Malformed `foo/**bar` glob](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055414898) | Not reproduced on the declared Python 3.14.3: this pattern matches `foo/mybar`; a missing match already refuses. A positive regression preserves this supported behavior. No speculative glob restriction was added. |
| Earlier wheel expansion, RECORD, filename, core metadata, dependencies, staged publication, configuration capture, bundled-test drift and archive errors | Already fixed in the current candidate: `candidate.py` validates these boundaries and rechecks captured inputs before publication. Original specifier-order speculation is obsolete; `_validate_core_metadata` compares parsed specifier sets with the captured project. |
| Earlier model, query, schema and detection-evidence findings | Already fixed: exact concrete manifest types in `CompletionBinding.bind` and adapter constructors; six-field regular-file probes for detected code inputs; UTF-8 query refusal; filename-based schema pairing; bounded cold-import timeout. |
| Earlier installed-member, package-tree, metadata, cache and authority-permission findings | Already fixed: descriptor capture; required-set presence and identity checks; bounded metadata before parsing; final metadata/external-cache rechecks; final authority ancestor policy checks. |
| Descriptor-leak, Protocol ellipsis, mixed-import and adjacent-string reports | Unsupported behavioral claims or advisory duplicates. Descriptors close in `finally`; ellipses declare Protocol methods; module imports are needed to patch the actual module constants; adjacent strings form one `python -c` argument. The proposed test-module `MAX_ENTRIES` patch would miss the production constant. Nested receiver naming was already changed. |
| Local proof paths, protected packet and docstring coverage | Local artifacts are explicitly nonportable and the document supplies reproducible commands. No authorized protected designation exists; the conditional policy and default bot docstring threshold are not S2 gates. Published historical test counts are not independently rerun or recertified by this audit. |
| Immutable constant aliasing and generated launcher contents | Valid disclosed limitations, not regressions within the stated S2 contract. Cache comparison establishes field/type/value equality, not identity-sensitive observational equivalence; launcher presence does not certify generated launcher bytes. Exact installed-entry/candidate qualification remains S7 in the structural design. No duplicate ticket is needed for that already scheduled design scope. |
| PR-Agent ticket-compliance claims for #150, #151 and #113 | Scope misclassification. Their live issue bodies explicitly call for separate follow-ups. `extract.py`, `manifest_ingest.py`, and `uv.lock` remain byte-identical to the PR base; dependency declarations and advisory scanner policy are unchanged. |

All six new behavioral inline claims were valid, while the new glob claim was
not reproduced and the two new import-style comments repeat prior dispositions.
The older outside-diff mode claim and related malformed-entry-point refusal were
also repaired. No actionable published claim is left awaiting clarification.

The only justified out-of-scope work remains the existing XML admission ticket
[#150](https://github.com/Villeneuve-Ventures/graphify/issues/150), dependency
maintenance ticket [#151](https://github.com/Villeneuve-Ventures/graphify/issues/151),
and policy umbrella [#113](https://github.com/Villeneuve-Ventures/graphify/issues/113),
plus the already designed S7 qualification above. No new GitHub ticket is needed.

Current repair evidence: 58 focused stdlib unittest tests passed, including the
new source/metadata cases and the affected installed/refusal/publication tests.
Scoped Ruff and interpreter-pinned Pyright passed. A fresh wheel passed fixture
archive/completion checks and disposable installation verification both without
caches and after full-package compilation at optimization levels 0, 1 and 2.
The AST graph update completed at 13,644 nodes / 30,470 edges with the same five
zero-node JSON warnings. Pytest was not rerun during this pass. Native
Linux/Windows validation and fresh full-suite certification are not claimed;
CI was not awaited. No new enforcement, protected-review, or bot-threshold gate
was introduced.


### New-feedback review (`62d1b89d` input)

The refreshed snapshot contains 39 conversation comments, 23 review bodies and
63 inline threads. Comparing it with the preceding complete audit found four
new technical claims. Updated bot summaries, review triggers, resolved-thread
flags and shifted line references supply no new evidence against settled
findings. The persistent Qodo review repeats the new Windows launcher claim;
its other technical claims duplicate dispositions above.

| New claim | Disposition and evidence |
|---|---|
| [Windows launcher companions](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055706242) | Unsupported for the admitted pip wheel-install route. Current [pip/distlib](https://github.com/pypa/pip/blob/main/src/pip/_vendor/distlib/scripts.py) embeds a ZIP `__main__.py` in each `.exe`; [setuptools wheel builds](https://github.com/pypa/setuptools/blob/1fe0c5d2f46e141d6097910d2905a3fce5e30c52/setuptools/command/bdist_wheel.py) disable the separate entry-point script writer. An isolated exercise of the local pip 26.1.1 Windows writer branch produced only `graphify.exe`. The cited setuptools writer describes another installation path; broadening admission requires a concrete supported wheel-install reproduction. No allowlist change or native Windows execution is claimed. |
| [Redirected staging parent](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055739035) | Valid: reproduction observed two checkout directory creations before the former late refusal. Fixture output now pins the external parent before staging and uses descriptor-relative creation, writes, cleanup and no-replace publication. Windows fixture generation now uses the existing `pin_output` mutation refusal before staging; ordinary package installation and installed inspection are unchanged. Native Windows qualification remains deferred, rather than preserving an unsafe pathname-write fallback. |
| [Aggregate bundled-test resources](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055739038) | Valid: scaled byte/count reproductions previously accepted over-limit inputs. The collector now bounds the bundle to 100,000 members and 256 MiB of retained schema/test/document bytes, preflights declared sizes, and limits each capture to the remaining budget. |
| [Duck-typed public verifier input](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055739042) | Valid: a duck object bypassed compatibility validation at this public entry point. `verify_installed_candidate` now requires the exact validated `CompatibilityManifest`, including refusal of subclasses. Installed-identity regressions now construct real validated manifests, so they exercise the same admission contract as callers. |

The central problem remains incomplete enforcement at the responsible boundary:
validation must precede the first side effect, total retained input needs an
aggregate bound, and every public admission entry point must enforce the same
validated model. These three repairs are S2 work. The Windows companion assertion
needs evidence for the supported install route before it can justify a change;
it is not an established defect or a reason for another gate.

The existing XML admission #150, dependency maintenance #151 and policy umbrella
#113 remain the only separately ticketed justified follow-ups. S7 already owns
exact-candidate and native qualification. No new or duplicate GitHub ticket is
needed. Historical test receipts above retain their original snapshot scope.

Current repair evidence: **68 focused stdlib unittest tests passed** across
installed identity, review regressions, fixture reads/publication, fixture inputs
and the new output/budget cases. Scoped Ruff and interpreter-pinned Pyright passed.
A fresh wheel passed fixture archive/completion checks and disposable noneditable
installation verification, both without caches and after package compilation at
optimization levels 0, 1 and 2. Tests cover rejection before staging, redirected
cleanup, preservation of a racing destination, aggregate/count refusal before
payload capture, growth after preflight, and the existing Windows pre-write
refusal. The budget bounds retained bundle payload, not total process RSS.

Reproduce the focused selection without pytest:

```sh
.venv/bin/python -B -m unittest tests.test_workspace_installed_identity tests.test_workspace_review_regressions tests.test_workspace_fixture_review tests.test_workspace_fixture_inputs tests.test_workspace_fixture_output_bounds -q
```

Pytest was not rerun, CI was not awaited, and no native Linux/Windows execution or
fresh full-suite certification is claimed. No additional approval or review gate
was introduced.

The required AST graph update completed at **13,668 nodes / 30,534 edges**; the
same five zero-node JSON corpus warnings remain.


### Boundary-completion review (`b2528c13` input)

The refreshed GitHub snapshot has 42 conversation comments, 26 reviews and 69
inline threads. Six new inline reports represent four distinct behavioral
claims, one duplicate and one comment-only suggestion. The persistent Qodo body
adds the same staging claim; its other retained claims have unchanged substance.
It says four lower-priority findings are omitted from that body; unpublished
portal-only content is not evidence available to this review. Every exposed new
claim is accounted for below.

The previous repair was incomplete: pinning the output parent did not bind the
staging directory's own identity to publication and cleanup. Likewise, bounding
the collected bundle did not bound earlier Git inventory materialization, and
the canonical document limit was checked after expensive allocation. These are
remaining S2 boundary defects, not merely noisy bots or a reason to add gates.

| New report | Disposition |
|---|---|
| [Foreign fixture publication/cleanup](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055854654) and [pinned-stage publication](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055881920) | One valid cause, reported twice; repaired. Publication checks the opened stage identity before rename and the destination identity afterward. Cleanup uses the retained stage descriptor and only known created entries; replaced or vacated names are preserved. |
| [Project extra normalization](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055881913) | Valid; repaired by normalizing project extra names for metadata comparison. An actual local setuptools metadata writer demonstrates that `foo_bar` becomes `foo-bar`; requirement markers already normalize through `packaging`. No dependency declaration changed. |
| [Source inventory materialization](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055881916) | Valid; repaired with bounded streaming of `git ls-files`, a 16 MiB output ceiling, a 100,000-name ceiling and canonical path validation before source payload capture. Early refusal terminates and reaps the enumerator. |
| [Canonicalization allocation](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055881922) | Valid; repaired by charging a shared canonical byte budget during normalization, before full-tree or JSON allocation. Model construction applies that bound before derived validator inventories while still validating original filesystem labels. Exact canonical bytes and existing refusal rules are preserved. |
| [Empty cleanup exception](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055818497) | Comment-only suggestion, not an independent behavioral defect. The cleanup repair documents why absent or replaced stage entries must be preserved rather than recursively removed. |

Settled Windows-launcher, cache-sharing, glob and deferred-ticket dispositions
remain unchanged: the new persistent review supplies no new evidence for them.
No new out-of-scope defect or GitHub ticket is established by this snapshot.


Integrated repair evidence: **89 focused stdlib unittest tests passed** across
the prior five regression modules plus `test_workspace_canonical_budget` and
`test_workspace_fixture_capture_identity`. Scoped Ruff, interpreter-pinned
Pyright and diff checks passed. A fresh wheel passed fixture archive/completion
checks and disposable noneditable installation verification, with no caches and
with package caches compiled at optimization levels 0, 1 and 2. The extra-name
test uses actual local setuptools metadata generation; the Git streaming path
also captured the real checkout successfully.

The new refusal tests use scaled limits and injected directory/file substitutions.
A substitution inside the rename primitive is detected afterward; it can leave a
foreign destination and the original staged directory intact. The operation
refuses and does not attempt an unsafe rollback. These observed-identity checks
do not promise atomic exclusion of hostile same-user writers. Canonical and
inventory limits bound their input/encoding work, not total process RSS or the
caller's already allocated objects. Existing destination preservation, canonical
byte parity, original-label validation and ordinary wheel installation remain
covered. No pytest rerun, native Linux/Windows certification, CI wait, review-bot
request or new approval gate was added.

Reproduce the new regressions with:

```sh
.venv/bin/python -B -m unittest tests.test_workspace_canonical_budget tests.test_workspace_fixture_capture_identity -q
```

The AST graph update completed at **13,712 nodes / 30,624 edges**, retaining the
same five zero-node JSON corpus warnings.

### Independent OMX review repairs (`f41509b7` input)

The attached-tmux OMX review pinned `f41509b7`, the complete 24-file PR diff,
44 conversation comments, 28 reviews and 71 inline threads. Independent
code-reviewer and architect lanes found three distinct in-scope defects. This
repair addresses those findings without reopening unchanged dispositions or
adding an acceptance gate.

- The latest [Qodo member-publication report](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4055931683)
  was valid: directory identity alone did not bind the files being published.
  Each member now binds writer-intended bytes, length and descriptor identity;
  completed members must remain singular owned regular files with mode 0600.
  The exact five-name namespace and member contents are checked before rename
  and after publication. Cleanup preserves foreign or drifted entries, including
  changes to the original inode. ZIP output hashes the writer's emitted bytes,
  so a tampered archive cannot become its own expected content.
- Valid S1 negative probes below absent or non-directory ancestors previously
  failed S2 ancestry validation. Such probes now retain exactly the evidence S1
  emits, including successful prefixes, without inventing missing bindings.
  Positive observations and explicit root bindings retain their prior checks.
  A real TypeScript alias resolver reproduces the missing-first-target fallback.
- Installed admission captured bounded METADATA/RECORD bytes but then reopened
  them through `Distribution.version` and `Distribution.files`. Version and CSV
  inventory now parse from those captures; the same METADATA bytes supply its
  content hash. Malformed RECORD fields refuse through the authority exception,
  and final metadata identity checks remain. Tests use real `PathDistribution`
  objects and actual RECORD bytes, including mutation after capture.

The shared cause was verifying an intermediate object while later consumption
or publication used a less constrained observation. These repairs bind the
consumed metadata and published members directly, while matching the actual S1
negative-observation contract. The namespace/content checks are observations,
not atomic exclusion of a hostile writer; post-rename refusal may leave the
published artifact in place and never rolls it back unsafely.

No new justified out-of-scope issue was found. Existing tickets #150, #151 and
#113 and design-assigned S7 qualification remain the appropriate follow-ups.

Repair verification: **109 focused stdlib unittest tests passed**. The new
fixture module includes 16 mutation subcases around publication, write/close
failure preservation and deterministic archive contents. The original member
publication code failed the initial regression run; actual S1 missing-ancestor
observations likewise failed before the contract repair. Metadata regressions
exercise the real distribution reader and forbid property-driven rereads.
Independent read-only verification of the metadata and S1 repairs found no
actionable regression.

A fresh source-matching wheel passed fixture archive/completion checks and a
disposable noneditable installation, both without caches and after compiling
package caches at optimization levels 0, 1 and 2. Scoped Ruff, interpreter-pinned
Pyright and diff checks passed. The required AST update completed with
**13,753 nodes / 30,761 edges**, retaining the same five zero-node JSON warnings.

Reproduce the integrated focused selection without pytest:

```sh
.venv/bin/python -B -m unittest tests.test_workspace_installed_identity tests.test_workspace_review_regressions tests.test_workspace_fixture_review tests.test_workspace_fixture_inputs tests.test_workspace_fixture_output_bounds tests.test_workspace_canonical_budget tests.test_workspace_fixture_capture_identity tests.test_workspace_fixture_member_binding tests.test_workspace_negative_probes -q
```

Pytest was not rerun. CI was not awaited. Historical full-suite and review
receipts retain their named snapshot scope; no fresh full-suite or native
Linux/Windows qualification is claimed.

### Consolidated claim closure (`251bf45a` input)

The next feedback snapshot contains 47 conversation comments, 30 reviews and
75 inline threads. Four new distinct claims were triaged together against the
pinned revision. The prior fixture-member publication finding is marked resolved
by Qodo. No review bot was triggered by this repair pass.

| Claim | Disposition and bounded repair |
|---|---|
| [Paired source observations](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4056136488) | Valid. `SourceObservation` accepted unrelated detection and consumed inputs. It now shares the existing completion-binding relation: identical roots/code inventory and preservation of every initial evidence record. Detection-only observations and new supporting consumed evidence remain valid; no operational stability passes are inferred. |
| [Unmodeled package selectors](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4056136487) | Valid. A real setuptools wheel included `extra.py` via `py-modules` while the fixture's member model omitted it. Only explicit `packages`, `package-data` and `include-package-data=false` are supported; other configuration keys or implicit manifest-driven data refuse before package payload collection/wheel reads/publication. Existing source-inventory capture still precedes that validation. |
| [RECORD hash syntax](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4056122108) | Valid format-admission gap, not demonstrated bypass of independently trusted package hashes. Require a guaranteed algorithm, canonical URL-safe unpadded base64 and a supported digest length. Empty hash/size fields remain allowed; SHAKE and configurable BLAKE2 lengths remain supported. |
| [Absent external caches](https://github.com/Villeneuve-Ventures/graphify/pull/149#discussion_r4056136486) | Valid. Reproduction moves a timestamp-valid forged cache into a previously absent selected path during the package-tree check, before admission returns. Selected absent cache names now receive a final `lstat` absence check, including nonregular entries, with no mutation or cleanup. This corrects the earlier OMX WATCH disposition: the defect is a missing before-return check within existing S2 admission, not protection against future writes. |

RECORD syntax follows the [installed-project specification](https://packaging.python.org/en/latest/specifications/recording-installed-packages/#the-record-file).
The independent repair review caught an initially overstrict BLAKE2 length check;
the correction preserves [Python's configurable digest lengths](https://docs.python.org/3/library/hashlib.html#using-different-digest-sizes).
Neither recorded hash nor size becomes content authority: immutable package and
metadata bytes still compare with the separately expected compatibility manifest.

The confirmed defects concern relationships between validated observations,
unsupported build inputs, or absent selected paths. The existing observed-state
limits remain: rechecks do not create an atomic snapshot or exclude later writes.
No new out-of-scope ticket or acceptance gate is justified. Existing #150, #151,
#113 and design-assigned S7 work retain their scope.

Final repair evidence: **125 focused stdlib unittest tests passed**. The
before-repair admission cases failed in 12 subcases; the package-selector
regressions failed in 13, and unrelated source-observation pairs were accepted
while completion binding already refused them. The independent review of the
four repairs found the BLAKE2 compatibility issue above; its focused correction
review found that issue resolved and no further actionable repair finding.

A fresh source-matching wheel passed fixture archive/completion checks and
noneditable installation verification without caches and with optimization
levels 0, 1 and 2. Scoped Ruff, interpreter-pinned Pyright and diff checks passed.
The final AST update completed with **13,783 nodes / 30,864 edges** and the same
five zero-node JSON warnings. Pytest was not rerun; CI was not awaited. No native
Linux/Windows qualification or new whole-PR clean verdict is claimed.

```sh
.venv/bin/python -B -m unittest tests.test_workspace_installed_identity tests.test_workspace_review_regressions tests.test_workspace_fixture_review tests.test_workspace_fixture_inputs tests.test_workspace_fixture_output_bounds tests.test_workspace_canonical_budget tests.test_workspace_fixture_capture_identity tests.test_workspace_fixture_member_binding tests.test_workspace_negative_probes tests.test_workspace_package_selectors tests.test_workspace_observation_binding -q
```


### Build-input boundary closure (`073c13ec` input)

The requested full OMX review of `073c13ec` approved the code-reviewer lane
and found one remaining architect P2: intended package membership omitted
setuptools automatic typing data and alternate build configuration. Repeated
hashing could therefore bind a stale wheel to an incomplete member model.

Package-local `*.pyi` and `py.typed` now enter the intended hashed inventory,
including typing files in explicitly listed subpackages. Stale wheels without
those members refuse; rebuilt wheels containing them pass. Hidden files and
unlisted package directories follow the backend's automatic-selection boundary.
A separate real setuptools 82 wheel experiment confirmed these selections.

Build-input admission accepts the modeled `setuptools.build_meta` backend with
setuptools-only declared requirements. Alternate backends, backend paths, build
plugins, dynamic metadata, additional entry-point groups, `setup.py` and
`setup.cfg` refuse before wheel reads. Setup-file checks use `lstat`, including
ignored files and dangling links. Configuration and absence are checked again
immediately before publication. Existing source-inventory capture still precedes
admission. This constrains declared build inputs, not arbitrary build environments
or backend versions; the existing observed-state and S7 limits remain.

Seven new stdlib regressions cover stale/fresh typing wheels, package-local
selection, alternate declarations, setup files and their late appearance. The
before-repair run reproduced the omissions. **132 integrated focused stdlib
tests passed**, and a fresh source-matching wheel passed fixture archive and
completion checks plus noneditable installation verification without caches and
with compiled caches at optimization levels 0, 1 and 2. Scoped Ruff,
interpreter-pinned Pyright and diff checks passed. The AST update completed with
**13,796 nodes / 30,892 edges** and the five existing zero-node JSON warnings.

The same architect resumed through attached tmux and marked the single P2
**RESOLVED**, with no remaining finding or repair-caused regression established.
The three repair-file hashes matched at both review boundaries;
`attached_tmux_review=yes`. This was a bounded correction follow-up, not another
whole-PR review. The original full-review verdict and historical full-suite
receipts retain their named snapshot scopes. No new out-of-scope ticket or
acceptance gate was justified. Pytest was not rerun; CI was not awaited.

```sh
.venv/bin/python -B -m unittest tests.test_workspace_installed_identity tests.test_workspace_review_regressions tests.test_workspace_fixture_review tests.test_workspace_fixture_inputs tests.test_workspace_fixture_output_bounds tests.test_workspace_canonical_budget tests.test_workspace_fixture_capture_identity tests.test_workspace_fixture_member_binding tests.test_workspace_negative_probes tests.test_workspace_package_selectors tests.test_workspace_observation_binding tests.test_workspace_build_inputs -q
```
