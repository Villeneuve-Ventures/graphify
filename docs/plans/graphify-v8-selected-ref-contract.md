# Selected packed-ref observation contract

This implementation follows issue #173's review at v8 commit
`20abd385f817eee7d3fa6060b848264cf09edc37` and the operator's subsequent request
to implement before preparing a PR. It supersedes the whole-file packed-ref
freshness interpretation documented in the S2/S4 result records. Those records
remain historical evidence for observer v2.

## Version and compatibility boundary

The current tuple uses adapter contract **3**, observer
`graphify-v8/workspace-observer-v3`, input-manifest format **2**, and scoped-input
ABI `graphify-v8-structural-2`. State schema **2**, graph payload format **1**,
distribution version **0.10.0**, and the engine baseline stay unchanged.

The adapter accepts only the new exact tuple and manifest format. Old authority
bundles, input manifests, receipts, and staged requests are not converted or
re-signed. Use the matching old candidate for old state, or explicitly prepare a
fresh compatible fixture/root through existing enrollment and lifecycle contracts.
This change adds no migration or historical-query transport. A shared state-schema
number does not establish candidate compatibility.

## Retained freshness evidence

HEAD, selected loose and symbolic-ref files, source/policy/supporting inputs,
negative probes, directory bindings, and ordinary/linked Git routing remain
strictly bound. Same-OID symbolic retargeting and loose/packed transitions remain
stale. Equal terminal HEAD OIDs alone do not establish freshness.

Format 2 adds one optional evidence operation:

```json
{
  "operation": "packed_refs",
  "path": "git:packed-refs",
  "value": [["refs/heads/main", "git:refs/heads/main", "0123456789012345678901234567890123456789"]]
}
```

The value contains at most one consulted terminal packed entry. Each entry binds
the canonical selected ref name, rooted loose lookup label, and lowercase SHA-1
OID. The corresponding loose lookup must have a retained negative probe. Standard
refs resolve in the common directory; worktree-local refs retain their specific
worktree route. Selected names still require canonical NFC UTF-8 labels.
Supported selected packed rows contain 40 hexadecimal bytes, one space or tab,
and the exact ref name. Duplicate rows, extra fields, and trailing whitespace
refuse; this is a bounded supported grammar, not a claim of all Git syntax support.

Every workspace observation emits this operation. Its value is empty when HEAD is
detached or its selected chain terminates in a loose ref. Packed values shadowed
by a loose ref and unrelated packed names are not selected freshness inputs.
Unrelated names inside the packed file remain opaque bytes.

The projection cannot coexist with physical read/probe records for the packed
container in a retained manifest. The container's file identity, timestamps,
whole-file digest, and existence do not participate in cross-observation equality.
Unrelated valid rows, headers, same-byte replacement, and safe appearance or
disappearance therefore do not invalidate an unconsulted packed container.
A selected packed value changing, becoming missing, malformed, or ambiguous
refuses strict replay.

## Acquisition and recovery

Acquisition still uses SourceIO's complete, bounded, no-follow reader. Every
present packed file must be read completely, even when its projection is empty.
Unsafe type/ancestor/routing, unavailable reads, byte/entry limits, and deadlines
still fail closed. Physical acquisition records remain in memory to detect
changes during a read and disagreements between repeated reads in one scope;
only the explicit semantic projection is serialized. This does not claim atomic
snapshots or detection of changes reverted between observations.

The scoped engine cannot read a projected packed container as an additional
supporting input. Such consumption refuses instead of silently dropping the
engine's byte dependency. Ordinary SourceIO use without the projection retains
its complete physical evidence.

Detection, consumed-input replay, completion/certification, pre-promotion checks,
and pre/post-query observation use the same representation. Query output remains
buffered, and status/query do not repair state.

Recovery reobserves the sealed operation set. A proven missing formerly selected
packed entry produces a null value in the recovery digest only, never a valid or
complete input manifest. Read failures, unsafe routes, malformed/ambiguous selected
entries, and budget failures cannot authorize abandonment. An already-promoted
exact sync retry reports its prior result; it does not assert current freshness.

## Acceptance boundary

Use disposable repositories and external state roots. Positive controls cover
unrelated loose and packed changes with selected HEAD loose, packed, and detached;
selected shadowing; valid packed-container changes; replay and recovery equality;
and certified queries and sync boundaries. Negative controls preserve selected
OID/route drift, missing/ambiguous/malformed selection, unsafe inputs, limits,
source/policy drift, canonical labels, and old tuple/manifest refusal.

The installed-wheel proof must exercise cold queries with mutation interception
and before/after snapshots. S5 can consume the new contract after this library
change is validated; this work does not implement S5, qualify S7, or authorize
live migration. PR delivery is a separately authorized stage.

S5 could retain observer v2's whole-file refusal without changing its contract.
Accepting unrelated packed-ref drift requires this prior library contract change;
it cannot be introduced solely as a CLI exception or by reusing v2 receipts.
The checkpoint controls call `git pack-refs` explicitly. They establish no claim
that Codex automatically packs checkpoint refs.

| Input or boundary | Required result | Acceptance evidence |
| --- | --- | --- |
| Unrelated packed ref added, replaced, or removed; loose, packed, or detached HEAD | Same detection, consumed replay, and recovery digest | `test_workspace_packed_refs.py` |
| Unconsulted container replaced, header changed, removed, or created; loose ref shadows packed value | Same freshness evidence | `test_workspace_packed_refs.py` |
| Linked worktree and common packed file | Preserve routing evidence; tolerate unrelated changes | `test_workspace_packed_refs.py`, `test_workspace_s4_git_routing.py` |
| Selected OID change, same-OID symbolic retarget, loose/packed transition | Refuse sealed replay; withhold certified-query output | `test_workspace_packed_refs.py`, `test_workspace_s4_installed.py` |
| Selected packed entry missing | Refuse strict replay; recovery can retain proven absence | `test_workspace_packed_refs.py` |
| Selected row duplicated or malformed | Refuse recovery; malformed certified retry stays CERTIFIED | `test_workspace_packed_refs.py` |
| Selected uppercase OID or Git-accepted tab separator | Canonical same selected value; strict loose bytes remain bound | `test_workspace_s4_git_routing.py`, `test_workspace_packed_refs.py` |
| Symlink, FIFO, directory, unavailable read, byte/entry bound, or in-scope disagreement | Fail closed; never turn a failed read into abandonment proof | `test_workspace_packed_refs.py`, existing routing/recovery/resource tests |
| Additional engine read of projected container | Refuse instead of hiding its byte dependency | `test_workspace_packed_refs.py` |
| Old tuple or manifest; malformed projection or missing negative lookup | Refuse validation; no conversion or re-signing | Contract/schema tests and `test_workspace_packed_refs.py` |
| Sync request, completed extraction, and certification boundaries | Unrelated packed drift can promote; existing lifecycle fences remain enforced | `test_workspace_packed_refs.py`, existing sync/recovery tests |
| Installed cold query with unrelated checkpoint packing or selected route drift | Same answer for unrelated drift; no output for selected drift; audited roots unchanged | `test_workspace_s4_installed.py` |

## Local validation receipt

On 2026-10-03, the isolated `codex/issue173-selected-refs` worktree, based on
`20abd385f817eee7d3fa6060b848264cf09edc37`, passed these disjoint pytest groups:

- Remaining workspace modules plus engine seams: **1,050 tests and 235 subtests**.
- Contract, Git routing, S4 review, consumed recovery, and installed candidate:
  **99 tests**.
- The complete issue-specific matrix plus README policy: **207 tests**.

Total: **1,356 tests and 235 subtests passed**. The installed-wheel test used Git
**2.55.0** and native Darwin/APFS admission. It checked cold query output, mutation
interception, and content snapshots after explicit unrelated checkpoint packing
and selected same-OID route drift. Independent review approved the production
repairs after the malformed-row recovery finding was corrected. Expanded Ruff
checks on changed Python files, `git diff --check`, and `graphify update .` passed.

The serial workspace/engine group took **40 minutes 41 seconds**. A bounded process
sample showed ongoing Python execution and filesystem open/stat calls; it does
not establish a precise per-test or per-function performance attribution.

At that local-only milestone, these were library and installed-fixture checks.
The full pre-PR pytest gate had not run, and CI was not awaited. No commit, push,
PR creation, S5 implementation, or live-state migration had been performed.

## Pre-PR validation receipt

The operator subsequently authorized PR delivery. In the same isolated worktree,
with Python **3.14.3**, Git **2.55.0**, and native Darwin/APFS, the full local gate
passed:

```text
PYTHONDONTWRITEBYTECODE=1 uv run --frozen --all-extras pytest tests/ -q --tb=short
7591 passed, 41 skipped, 6 warnings, 235 subtests passed in 4286.83s (1:11:26)
```

Three warnings came from jieba invalid escape sequences, two from the tested
out-of-scope semantic-cache guard, and one from the tested nested workspace-state
cache-cleanup guard. The result includes the installed-wheel cold-query proof and
the complete issue-specific acceptance matrix. It does not establish passage of
skipped tests or remote CI.

The five separate skillgen checks (`--check`, `--audit-coverage`,
`--schema-singleton`, `--monolith-roundtrip`, and `--always-on-roundtrip`) passed.
The optimized-Python protected-verifier suite passed **80 tests**, with its
expected assertion warning. `python -m tools.ci_pytest_gate smoke` passed using
disposable HOME and CODEX_HOME roots. Native Leiden partition smoke passed in the
installed all-extras environment; this was not a fresh binary-only environment.

CI was not awaited. S5 implementation and live-state migration remain outside
this delivery stage.
