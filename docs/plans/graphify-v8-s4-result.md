# S4 v8 structural library round trip

This candidate is based on v8 `8798f3981847f98c7ce5c647f0a9338398c3489f`.
It implements the S4 library boundary from the
[structural design](graphify-v8-structural-workspace-design.md). Publication is
one draft PR. S5 commands, S6 maintenance transports, S7 release qualification,
semantic execution, migration, and consumer adoption remain separate work.

## Library boundary

`StructuralComposition.require_runtime()` now rereads the explicit runtime
manifest and verifies the actual installed distribution before constructing
an operational adapter and S3 stores. Construction is read-only. Probe selection
still grants no execution; operational selection requires the S4 package members
and exact whole-candidate equality. Package-version fallback remains forbidden.
The compatibility manifest still identifies an uncertified **local fixture**;
certifying a disposable generation does not certify a release candidate.

After explicit registry enrollment, adoption, and source activation, an embedding
caller uses:

```python
from graphify.workspace.sync import prepare_structural_sync, synchronize_structural
from graphify.workspace.query import query_structural
from graphify.workspace.adapters.base import QueryRequest

request = prepare_structural_sync(
    runtime, repo_uuid=repo_uuid, generation_id="gen-example",
    source_epoch=1, desired_watermark=1, expected_payload_bytes=1024 * 1024,
)
result = synchronize_structural(runtime, request, attempt_sha256=attempt_sha256)
text = query_structural(runtime, repo_uuid, QueryRequest("caller"))
```

The example limits and epochs are fixture inputs, not operational defaults.
`runtime` comes from `compose_workspace_runtime(load_workspace_runtime_inputs(
state_root=..., expected=...)).require_runtime()` with an explicitly installed
candidate and authority. Registration remains the existing explicit registry API.
The tests demonstrate adoption and activation of a second linked worktree;
enrollment's initial source selection is not counted as activation.

Persist `request.canonical` in caller-owned storage if needed; recover it with
`StructuralSyncRequest.from_json`. Retry the same frozen request. A fresh attempt
uses a new explicit digest after release; an uncertain acquisition or retained
lease requires its original attempt identity. The caller must inspect a refusal
rather than silently create a replacement request. The library admits no other
request over a nonterminal request or retained semantic lifecycle.

## Implementation and evidence

- `workspace/adapters/v8.py` is the only bridge to engine-private computation.
  It reuses scoped detection, strict synchronous extraction, graph construction,
  stream serialization, traversal, and the memory-only tokenizer. No ordinary
  output transaction, parser, provider, or alternate graph engine is introduced.
- Initial detection remains separate from consumed-input completion. Original
  non-code bytes, policy, Git routing/refs, supporting reads, absent probes, and
  directory membership are reobserved through fresh bounded `SourceIO` contexts.
  Complete agreeing passes must reproduce the entire manifest. Every admitted
  code input has an explicit successful or empty disposition; missing parsers,
  incomplete extraction, unaccounted inputs, and external resolver escapes refuse.
  Non-code inputs are hashed for freshness, not semantically extracted.
- Structural sync uses exact S3 request/attempt bindings, explicit capacity,
  heartbeat and fence checks, descriptor-bound staging, durable completion,
  structural empty-queue reconciliation, sealed-input binding, certification,
  and pointer promotion. The writer joins its heartbeat before completion.
  Pending record recovery projects and checks exact authority before mutation.
  A durable certification binding is recovered without rerunning the engine.
- Query holds existing registry/workspace locks and the shared generation lock,
  verifies the sealed generation, buffers v8 output, observes source freshness
  before and after traversal, and revalidates pointer and runtime authority before
  returning text. It creates no lock, lease, cache, log, receipt, or repair record.
  Deadline expiry withholds output. Callers launch Python with `-B` or
  `PYTHONDONTWRITEBYTECODE=1` **before imports** to suppress startup bytecode writes.

The focused suites cover source/supporting-input drift, missing parsers and
unsupported extraction, original Office inputs and Google-shortcut refusal,
relative IDs and v8 facts/ranking, exact candidate/authority rejection,
request serialization, stale fences, queue watermark changes, preserved sources,
ordinary-write refusal, query GC protection, and suppressed stale query output.
Injected interruptions cover request, acquisition, reservation, staging, build,
completion, queue/sealed-input binding, receipt durability/install, certification,
pointer intent/visibility/journal, promotion, and release. Retries reconstruct
runtime/request objects and verify the same result without rebuilding sealed data.
Pointer recovery may advance the pointer revision to record repair, as required
by S3; it does not create a second generation.

`test_workspace_s4_installed.py` builds a wheel and content-bound fixture bundle,
installs only into a disposable venv, and exercises actual installed-package
admission. Cold subprocesses install an audit hook before candidate imports and
query with absent and misleading tokenizer caches. They deny filesystem writes,
ambient jieba-cache access, network effects, and unexpected subprocesses, and
compare state/source/installed-tree bytes, modes, identities, and mtimes.
The existing S1 cold tests independently qualify Chinese token/ranking parity
and the missing-extra fallback. No public workspace CLI is added by this proof.

## PR #167 review repairs

The feedback snapshot at `1a17c3f01d72c73a849487958f184bfc9a988073` included
three conversation comments, two reviews, and six inline threads, with no linked
issues. Three independent execution-boundary defects were reproduced and repaired
in this priority order:

1. [Query bounds](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4114054405):
   both library and adapter entry points reconstruct a validated request before
   authority, locks, payload access, or traversal. Exact type alone is insufficient
   for a deserialized or altered dataclass.
2. [Payload modes](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4114054412):
   the adapter sets mode `0600` on each newly opened payload descriptor before
   writing, independent of umask. A `0277` fixture now completes and promotes.
3. [Git refs](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4114054409):
   reference names use Git's own validator through the existing bounded Git
   helper, with the query deadline preserved. UTF-8 loose and packed refs accept
   valid `+`, `@`, and Unicode names, including Unicode whitespace. Only Git's LF
   delimiter is removed or split; invalid ref paths still refuse before probing.

Regression tests reproduced all three defects and the Unicode whitespace
edge cases. The focused run passed 44 tests, including the adapter and actual
installed-wheel no-write proof. The remaining inline warnings require no repair: module and member imports serve
distinct monkeypatch/call sites, the ellipsis is a standard protocol method stub,
and heartbeat `BaseException` handling transfers thread failure to the caller
after joining, preventing silent heartbeat loss. Review-thread state is unchanged.

The subsequent feedback snapshot at `9f94657013d39acc8d14c40ae0ece1936f9a4875`
contained four conversation comments, three reviews, and eight inline threads,
with no linked issues. The two new findings concern adapter observation:

1. [Replay budget](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4115528274):
   detection and consumed-manifest replay now use separate bounded `SourceIO`
   contexts. Replay retains the selected roots and all reader limits, compares
   the complete retained evidence, and must still extend the initial detection.
   A tightened-budget fixture reproduces the former post-build failure and now
   completes, promotes, and queries without writes; over-budget input still refuses.
2. [Ref bytes](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4115528275):
   packed-ref matching preserves bytes, so an unrelated non-UTF-8 ref no longer
   breaks a supported checkout. Selected refs still require the canonical UTF-8
   filesystem labels mandated by S2, including absent loose-ref probes. Such
   unsupported selected names now fail explicitly before probing, rather than
   leaking a decoder exception. Arbitrary byte-path support would require a
   separate input-manifest contract change and is not claimed here.

The review of `319ced6361b513a5e9f14d33235bf056acc466f6` added two admission
findings (eight conversation comments, four reviews, ten inline threads, no linked
issues). The [watermark finding](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4115711920)
reproduced: invalid reconciliation coordinates could reach durable staging.
Preparation now checks the locked queue snapshot, and execution repeats that
check before staging. Backward watermarks and same-watermark requests bound to
different source evidence refuse without state writes; matching evidence and
exact retries remain valid. New requests also refuse an already-held lease:
otherwise its holder could advance the queue and release without changing the
request's operation epoch between admission and staging. After a lease-free
snapshot, a new writer must advance that epoch, which S3 checks atomically before
persisting the staged request. Existing exact staged recovery remains permitted.
A later valid request can still promote.

The [oversized-reservation finding](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4115711915)
did not reproduce: the existing `GenerationStore.request_staged_build` payload
ceiling already runs before persistence. An S4 integration regression confirms
unchanged source/state snapshots and a successful valid call after rejection.

The review of `89414e4189de35687b351d0da122eefd86a7d178` added one
[extraction-budget finding](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4116508830)
(eight conversation comments, five reviews, eleven inline threads, no linked
issues). Detection and extraction shared one cumulative read budget, so input
that fit either pass could fail when their reads were added together. Extraction
now uses a separate context with the same roots and limits. Its consumed evidence
is combined with detection evidence, rejecting disagreements and revalidating the
combined manifest bounds before payload writes. Repeated reads within extraction
still count against that pass's limit; this does not change the S1 I/O contract.
Focused regressions cover promotion and a no-write query, inter-pass source drift,
and the combined unique-input ceiling with detection-only and extraction-only inputs.

The review of `ed68b9df6056b62aa36571c220f395bcf9021b73` added two
resource-lifetime findings (nine conversation comments, six reviews, thirteen
inline threads, no linked issues), repaired in this order:

1. [Payload reservation](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4116735049):
   the adapter receives the durable allocation ceiling. Its UTF-8 serialization
   buffer stops at the remaining budget after charging the input manifest, before
   either payload file is created. A proved overflow follows the existing fenced
   capacity-abandonment path after trusted source re-observation and an empty
   staging check, freeing the reservation for a later valid request. Interruption
   after durable abandonment intent remains recoverable. S3 still checks the
   payload plus canonical receipt before any certification write.
2. [Lease lifetime](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4116735056):
   heartbeat now covers the acquired build/certification and promotion sections,
   including source observations. It joins before lease release. Five-second
   lease fixtures with six-second observations reproduce the former expiry at
   allocation, completion, and promotion, and now complete without retained
   leases or heartbeat threads.

The feedback snapshot at `a904c250e9088b919576751c621e6f22733648e2`
contained ten conversation comments, nine reviews and eighteen inline threads,
with no linked issues. Four current findings share late admission or incomplete
resource accounting. Repairs are prioritized as follows:

1. [Git includes](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4117494292):
   every discovery Git command first checks bounded, descriptor-read local config,
   including common-directory and worktree config. Includes refuse before a Git
   subprocess can read an external target, including FIFO targets and BOM-prefixed
   config. Existing remote rewriting remains supported. This is observed preflight,
   not a claim of safety against hostile concurrent configuration replacement.
2. [Receipt headroom](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4117009633):
   reserve 4 KiB for the fixed structural receipt fields plus the decimal lengths
   of its five lifecycle counters. The schema fixes hashes, proof names, two
   payload paths and bounded generation/lock identifiers; maximal-field regression
   coverage checks the bound, including large lifecycle counters. Charge the same
   allowance to early serialization failures, preserving fenced abandonment and
   subsequent valid requests. Tiny reservations refuse before staging.
3. [Adapter ceiling](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4117494287):
   preparation and execution reject reservations above the adapter's 512 MiB
   ceiling before staging, even if runtime capacity policy would admit them.
4. [Retained lease](https://github.com/Villeneuve-Ventures/graphify/pull/167#discussion_r4117494290):
   renew synchronously on heartbeat entry before waiting for periodic renewal.
   An existing grant with less than one renewal interval remaining now survives
   observation; release still follows heartbeat join.

The repeated Protocol ellipsis warning remains intentional, as do the previously
reviewed mixed imports and exception transfer. CodeRabbit's default docstring
coverage warning does not identify a behavioral defect or an adopted repository
gate; broad docstring generation is outside these repairs. No review threads or
issue state were changed.

## Validation receipt

Validation uses frozen all-extra dependencies, CPython 3.14.3,
and Git 2.55.0. The native fixture runs on macOS 27.0 build 26A428 and native
non-elevated local APFS admission. Portable failure fixtures explicitly inject
capability support and are not native durability evidence.

| Check | Result |
| --- | --- |
| `uv sync --all-extras --frozen` | Passed. |
| Full serial `pytest tests/ -q --tb=short` | 6,745 passed, 41 skipped, 6 warnings, and 235 subtests passed in 1,805.92 seconds (30m05s). |
| Optimized Python protected-verifier suite | 80 passed in 12.50 seconds (one optimized-mode pytest warning). |
| Five `tools.skillgen` checks | Check (134 artifacts), coverage audit, schema singleton, monolith round trip, and always-on round trip passed. |
| Current feedback regressions | 14 passed in 31.67 seconds; near-limit certification, maximal receipt bounds, early admission, capacity cleanup, retained renewal, and ordinary/linked/BOM Git includes. |
| Current admission and recovery checks | 24 follow-up/admission cases passed in 194.10 seconds; 5 uncertain-commit recovery cases passed in 63.34 seconds. |
| Focused resource and capacity regressions | Prior candidate: 15 passed in 87.53 seconds, including bounded serialization, manifest accounting, recoverable abandonment, slow observations, and existing receipt-capacity checks. |
| Focused extraction-budget regressions | Build/promotion/no-write query passed for code and non-code inputs; inter-pass drift and oversized combined evidence refused before payload writes. |
| Focused admission tests | Prior candidate: 10 passed in 137.86 seconds, including state preservation, the existing-lease queue race, valid follow-up requests, and exact retries. |
| Adapter/review/installed-wheel tests | Prior candidate: 47 passed, including actual installed-candidate admission and absent/misleading-cache no-write checks; also included in the current full suite. |
| Disposable help/install and native Leiden smoke | Passed; real user installation/configuration remained unchanged. |
| Focused S3 regressions | 112 passed, 24 subtests passed. |
| Diff review and Ruff F checks | Passed on task paths. |
| Task-owned `graphify update .` | Canonical graph/report refreshed; no provider tokens. |

The final serial and optimized pytest runs disabled bytecode and pytest-cache
writes. The graph refresh is local
orientation output, not a release or immutable-candidate certification receipt.
Advisory Bandit completed with 14 finding records in unchanged files. Pip-audit
completed with 12 finding records against the unchanged default-plus-dev lock
environment. Its first all-extras attempt was explicitly incomplete because the
receipt helper requires the CI default-plus-dev scope; a separate disposable
environment supplied that scope without altering the test environment. Findings
remain advisory; no dependency or enforcement change is included.

## Limits

Freshness is observed-current, not an atomic snapshot or proof against changes
made and reverted between observations. Power-loss and hostile concurrent-rename
proof are not established. Git observation supports ordinary files-based Git
repositories and standard linked worktrees; includes/extensions, alternate object
stores, shallow history, and unsupported reference routing refuse explicitly.
Selected Git ref paths must satisfy S2's canonical NFC UTF-8 input-label contract;
unrelated ref names inside the byte-bound `packed-refs` file need not be UTF-8.
Queries without retained migration lineage refuse nonzero migration epochs;
S4 introduces no migration or historical-query compatibility promise.

Security enforcement and D1/D2/D3 remain unchanged. Real installed tools, managed
state, unrelated worktrees, and the ignored coordination tracker are untouched.
No consumer repository is enrolled. CI, review bots, and approvals are not awaited.
