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

## Validation receipt

Validation uses frozen all-extra dependencies, CPython 3.14.3,
and Git 2.55.0. The native fixture runs on macOS 27.0 build 26A428 and native
non-elevated local APFS admission. Portable failure fixtures explicitly inject
capability support and are not native durability evidence.

| Check | Result |
| --- | --- |
| `uv sync --all-extras --frozen` | Passed. |
| Full serial `pytest tests/ -q --tb=short` | 6,705 passed, 41 skipped, 235 subtests passed, 6 warnings in 1,349.52 seconds (22m 29s). |
| Optimized Python protected-verifier suite | 80 passed in 11.27 seconds. |
| Five `tools.skillgen` checks | Check (134 artifacts), coverage audit, schema singleton, monolith round trip, and always-on round trip passed. |
| Installed-wheel cold-query fixture | Passed actual installed-candidate admission and absent/misleading-cache no-write checks. |
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
Queries without retained migration lineage refuse nonzero migration epochs;
S4 introduces no migration or historical-query compatibility promise.

Security enforcement and D1/D2/D3 remain unchanged. Real installed tools, managed
state, unrelated worktrees, and the ignored coordination tracker are untouched.
No consumer repository is enrolled. CI, review bots, and approvals are not awaited.
