# Graphify workspace contract v1

Implemented contract scope through the unnumbered P5B2 active-source activation
CLI, the unnumbered P5B2 identity-maintenance CLI, P5B2c one-shot certified
workspace query,
P5C1 candidate-bound canonical runtime authority generation and isolated atomic
installation/compensation proof, P5B2b provider-neutral code-only structural
sync, P5B2b0 staged structural-build recovery, P5B2a initial workspace
registration, P5B1 read-only workspace status/doctor, P5A semantic queue, P4
adapter, and observed-current library runtime for
`graphifyy 0.9.16+workspace.1`. Durable state schema v1 and runtime-manifest
format v1 remain frozen; public status JSON is schema v2.

This directory defines the first version of Graphify's workspace control-plane
contracts. P2 provides a library surface for external durable registry state,
operator-authorized UUID/source binding, explicit active-source selection, and
fenced lease allocation. P3 adds caller-supplied generation staging and
certification, a framed segmented journal, atomic pointer movement and repair,
retained coordination locks, explicit capacity preflight, and offline GC. P4
adds the sole `0.9.16` engine adapter, the no-write comparison seam, and
two-sided observed-current release. P5A adds only the durable semantic desired-
work queue, fenced worker claims, exact reconciliation evidence, and the
generation-certification binding to one stable queue watermark. It does not
provide a retained-state import path. P5B1 adds only the production composition
root plus versioned read-only `graphify workspace status --json` and
`graphify workspace doctor` inspection. With no explicit library inputs, those
commands read the canonical, versioned `runtime-manifest.json` authority from
the external state root selected by `XDG_STATE_HOME` or `HOME`; missing,
malformed, unsafe, or unsupported authority fails closed without creating or
repairing state. P5B1 only consumes that file. P5C1 now generates its canonical
candidate-bound bytes, binds them to the immutable candidate trust set, and
proves atomic installation and compensation only beneath disposable
external-state roots while preserving P5B1's loader unchanged. It supplies no
production installation, publication, service/watch, provider, or performance
authority.
P5C retains long-lived service/watch and broader query authority. P5B2a adds only
`graphify workspace register enroll` for initial
enrollment and `graphify workspace register adopt` for an already-enrolled
verified clone or fork whose retained history includes a root recorded at
enrollment. A shallow clone that omits that root fails the shared-history proof
until sufficient history is fetched. Both forms require the repo UUID, expected
registry revision, and a matching `OperatorAuthorization` JSON object on
standard input. The command requires the current working directory itself to be
the Git top level, ignores local Git replacement refs and legacy graft files,
cross-checks its bounded no-follow `.graphify/workspace.toml`, and never infers
adoption. ADOPT also rejects a source whose persisted Git common-directory
device/inode identity belongs to another repo UUID before new source or
identity-action evidence is persisted or the requested registry mutation is
committed, while same-UUID retained-inode adoption remains allowed. It emits
one canonical redacted receipt and writes only the existing P2 registry,
workspace, lock, and evidence records beneath the configured external state
root. The receipt's normative machine-readable schema is
`graphify/workspace/schemas/cli/v1/registration.schema.json`.
The narrow identity-maintenance slice extends the same argv family with
`graphify workspace register rebind` and
`graphify workspace register rotate`. Both require the same explicit UUID,
expected registry revision, installed authority, Git-top-level source proof,
and bounded matching authorization. Rebind delegates shared-history or enrolled
Git-common-directory policy to `RegistryStore.rebind()`; rotate delegates the
explicitly-bound-source check to `RegistryStore.rotate_enrollment_evidence()`.
Rebind rejects a source identity persisted under a different UUID before
new source or identity-action evidence is persisted or the requested registry
mutation is committed. Neither operation changes `active_source` or
`active_source_revision`, and both emit the separate CLI-v1 receipt defined by
`graphify/workspace/schemas/cli/v1/identity-maintenance.schema.json`.
The separate active-source activation slice exposes only standalone
`graphify workspace activate` with the explicit repo UUID and registry,
active-source, operation, and migration CAS values. It loads and composes
installed runtime authority before consuming one canonical, duplicate-free,
16 KiB `ACTIVATE` authorization object; rediscovers and exactly revalidates the
current Git-top-level checkout; derives the trusted lease owner, UTC timestamp,
monotonic timestamp, and 30-second TTL internally; and delegates exactly once
to the existing `RegistryStore.activate_source()` policy. A target already
selected as the active source is rejected under the registry lock before lease,
evidence, or revision mutation. The canonical redacted receipt is defined by
`graphify/workspace/schemas/cli/v1/activation.schema.json` and never exposes
authorization, source paths, lease-owner identity, or raw errors. The existing
`register activate` remains invalid. Remaining mutation/query commands, repair,
watch/service, performance certification, and candidate publication remain
later P5 work.
P5B2b0 adds the internal request-bound staged-build and stale-abandonment
recovery contract described in [State contracts](state-contract.md). P5B2b
exposes only `graphify workspace sync --code-only --request-stdin`, using a
bounded canonical JSON request, external generation-owned staging, the existing
fenced lifecycle, and one canonical redacted receipt. Status and doctor now
surface nonterminal staged-build recovery barriers through status schema v2.
P5B2c exposes only the one-shot certified
`graphify workspace query --request-stdin` transport described below. It loads
and composes installed runtime authority before consuming standard input, then
calls the existing `WorkspaceRuntime.freshness.query()` authority once; it does
not perform an advisory status probe. Provider selection, networking, semantic
execution, mutation, retained service/watch, and every broader query authority
remain deferred.

## P5B2c one-shot certified query

The exact and only P5B2c argv is `graphify workspace query --request-stdin`.
It accepts one CLI-v1 canonical JSON object on standard input. The object is
duplicate-free UTF-8, at most 32 KiB, and contains exactly `contract`,
`schema_version`, `cli_contract_version`, `repo_uuid`, the existing
`QueryRequest` fields (`question`, `mode`, `depth`, `token_budget`, and
`context_filters`), and `timeout_ms`. `repo_uuid` is explicit; `timeout_ms` is
an integer from 1 through 60000. The command reuses `QueryRequest` for runtime
enforcement of all query bounds. Because JSON Schema `maxLength` counts Unicode
code points rather than encoded bytes, the CLI-v1 schema publishes the frozen
question and context-filter byte ceilings as non-enforcing
`x-graphify-utf8-max-bytes` and `x-graphify-utf8-total-max-bytes` annotations.
Focused parity tests bind those annotations to the existing `QueryRequest`
UTF-8 behavior; changing the CLI-v1 bounds requires a new CLI contract version.
Malformed, noncanonical, extra-field, untrimmed, oversized, out-of-bound, or
unsupported-version input is rejected before freshness locking or query
execution.

The command writes raw native query output, encoded as UTF-8 without a wrapper,
to standard output only when freshness returns `decision=release` and
`reason=observed_current`. It also writes one canonical redacted
`graphify.workspace.query_result` v1 control record to standard error. On
release that record binds the explicit repo UUID and nested `output` metadata:
`stream`, `encoding`, `bytes`, and `sha256`. Every other result leaves standard
output empty and omits the repo UUID and `output` metadata from the control
record.

Consumers must treat captured standard output as committed only when the
process exits 0, standard error contains exactly one canonical schema-valid
CLI-v1 control record reporting `release` / `observed_current` output on
UTF-8 standard output, and that record's byte count and SHA-256 digest match the
captured standard-output bytes. If any condition fails, the captured output is
uncertified and must be discarded. This commit rule verifies the deliberately
split streams; it does not make their delivery atomic.

| Exit | Result | Standard output |
|---:|---|---|
| 0 | `released` | Exact native output only |
| 10 | `drifted`, `timed_out`, or other retryable `withheld` result | Empty |
| 20 | `unsupported` or `invalid` | Empty |
| 64 | Any argv other than the exact invocation | Empty |

Existing freshness behavior classifies `LockTimeout` contention as `timeout`;
the CLI reports that truthfully as `timed_out` rather than inventing a separate
contention result. The path creates no query log and writes nothing to the
source checkout, Git metadata, workspace state, `HOME`, or `CODEX_HOME`.

## Identity-maintenance CLI

The exact identity-maintenance forms are:

```text
graphify workspace register rebind --repo-uuid UUID --expected-registry-revision N --authorization-stdin
graphify workspace register rotate --repo-uuid UUID --expected-registry-revision N --authorization-stdin
```

Malformed argv exits 64 with the deterministic workspace usage text on standard
error before authority loading, source discovery, or standard-input reads. For
valid argv, installed runtime authority is loaded and composed before the
authorization object is consumed. Authorization is duplicate-free UTF-8 JSON,
bounded to 16 KiB, canonically encodable, and must name exactly `REBIND` or
`ROTATE` for the selected lowercase verb. Source discovery, exact revalidation,
external-state checks, and expected-revision CAS are identical to initial
registration; policy remains solely in the existing registry methods.

Success writes one canonical `graphify.workspace.identity_maintenance` v1
receipt to standard output and exits 0. A revision or identity-policy conflict
writes one redacted receipt to standard error and exits 10; an invalid authority,
authorization, source, state, runtime, or uncertain commit writes one redacted
receipt to standard error and exits 20. Failure receipts omit the repo UUID and
include the observed registry revision only for a safe deterministic revision
conflict. Registration v1 remains limited to `enroll` and `adopt` and does not
admit either maintenance action. Rebind's cross-UUID persisted-source-identity
check runs before new source or identity-action evidence is persisted or the
requested registry mutation is committed; rotate retains the existing
explicitly-bound-source requirement. Both preserve `active_source` and
`active_source_revision`.

## Active-source activation CLI

The exact activation form is:

```text
graphify workspace activate --repo-uuid UUID --expected-registry-revision N --expected-active-source-revision N --expected-operation-epoch N --expected-migration-epoch N --authorization-stdin
```

Malformed argv exits 64 before authority loading, standard-input reads, source
discovery, or state access. Valid argv causes installed runtime authority to be
loaded and composed before standard input is consumed. The authorization bytes
must be canonical JSON, duplicate-free UTF-8, no larger than 16 KiB, and contain
exactly the five existing operator-authorization fields with `action` set to
`ACTIVATE`. Owner identity, wall time, monotonic time, and the bounded 30-second
activation lease TTL are trusted runtime inputs and cannot be supplied by the
caller.

The current working directory must be the exact Git top level, its configured
UUID must match `--repo-uuid`, and its source identity must remain stable across
the existing two discovery passes and checkout revalidation. The source must
already be explicitly bound in the registry and must still match immutable
enrollment identity by sharing a recorded history root or retaining the
enrolled Git common-directory device/inode. It must differ from the selected
active source. The CLI calls `RegistryStore.activate_source()` once with all
four caller-supplied CAS values; that method rejects active-source reselection
under the registry lock before lease, evidence, or revision mutation and retains
fencing, reservation and recovery barriers, alias normalization, and semantic
active-source authority.

Success writes one canonical `graphify.workspace.activation` v1 receipt to
standard output and exits 0. A stale CAS, already active, unbound or mismatched
source, live lease, or recovery barrier writes one redacted conflict receipt to
standard error and exits 10. Invalid authority, authorization, source, runtime,
state, or uncertain commit writes one redacted invalid receipt to standard error
and exits 20. Only success includes the repo UUID and resulting active-source
and epoch values; a safely observed stale registry revision may appear in its
dedicated conflict result. No receipt includes authorization, paths, owner
identity, or a raw exception. `InjectedFault` remains an internal test signal
and is re-raised.

## Governance and deferred work ownership

The publication gate in [Workspace governance](governance.md) requires one
published `workspace/v1` commit to add the governance ledger and receipts and
update this ownership map. Until that gate activates, the external execution
checklist and global plan retain Graphify-local status, readiness, and receipt
authority, and these repository files are proposed migration records. After
activation, Workspace governance owns Graphify-local live phase/readiness
state, its [accepted receipts](receipts/) own completion evidence, and the
external plans retain only cross-repository dependencies and P6-P12 portfolio
sequencing. Direct operator instruction alone owns execution authorization.

| Area | Owner/status | Stable boundary |
|---|---|---|
| Additional sync modes | Remaining P5B2 | Only provider-neutral structural `sync --code-only` is public. Semantic sync and any broader mode require separately reviewed authority, provider, redaction, and recovery contracts. |
| Certified one-shot query | P5B2c (`COMPLETE`) | Only `workspace query --request-stdin` is public: installed authority precedes input, one freshness query can release exact output after `observed_current`, and every other path withholds it. |
| Identity maintenance | P5B2 identity maintenance (`COMPLETE`) | Accepted receipt: [`P5B2 identity maintenance`](receipts/p5b2-identity-maintenance.md). `workspace register rebind` and `rotate` expose only the existing registry policy with explicit UUID, revision CAS, matching authorization, cross-UUID rebind rejection before new source or identity-action evidence and the requested registry commit, unchanged active-source state, and a dedicated receipt schema. |
| Active-source activation | Unnumbered P5B2 activation slice | `workspace activate` alone exposes the existing fenced active-source CAS with explicit UUID and four-part CAS, canonical `ACTIVATE` authorization, internally derived lease inputs, exact bound-source proof, and one redacted CLI-v1 receipt. This implementation does not change governance status or accept a completion receipt. |
| Remaining workspace commands | Remaining P5B2/P5C | Migrate, rollback, GC, repair, every other mutation, and all query authority beyond P5B2c's one-shot certified transport require separately reviewed contracts and explicit operator intent. |
| Candidate runtime authority | P5C1 (`COMPLETE`) | Generates canonical `runtime-manifest.json` from the existing compatibility manifest plus explicit `SemanticQueuePolicy`, binds its exact bytes/hash to the immutable candidate, installs it atomically only in isolated external-state fixtures, proves deterministic-failure compensation, and preserves P5B1's read-only loader unchanged. |
| Service, release, and resource proof | Remaining P5C | Watch/service supervision, publication, representative-corpus performance and resource accounting, record admission budgets, retained production query/service authority beyond the P5B2c one-shot transport, and any shared workspace read-lock optimization remain waiting outside P5C1. |
| Static-analysis baseline | H3 | Inherited full-repository Pyright and medium-severity Bandit debt remains deferred and non-blocking after H2 established blocking high-severity and dependency-audit gates. |
| Portfolio migration and cutover | P6-P12 | Shadow migrations precede the P9 global installation and stable-route activation; legacy pruning remains separately authorized after the observation window. |
| Semantic capability selection | Unranked cross-cutting follow-up | With no Gemini key, an interactive Graphify skill can use its active host agent; in Codex that means the current Codex-authenticated session. A direct headless fallback from `graphify extract` to Codex OAuth is desired but is not implemented or promised by v1. Any such backend requires a separate explicit authority, selection-precedence, opt-in, redaction, offline/failure, and test contract; provider choice must never be inferred from an absent credential. |
| Contract and support horizon | Unranked future contract decisions | Possible v2 sorted-array admission, broader host/filesystem support, sudden-power-loss claims, automatic online GC, historical-generation query, and upstreaming are retained decisions or nonclaims, not current phase gates. |
| Extraction diagnostics | Outside workspace phase gates | Zero-node fixture notices and missing optional SQL/DM parser extras remain non-blocking diagnostics unless a later requirement explicitly promotes their corpus coverage. |

Historical deferrals for the labeling-order test, candidate packaging warnings,
vulnerable optional/development dependencies, high-severity Bandit findings,
and Gemini model-override test isolation are closed by later merged work and are
not carried forward as debt. Deliberately rejected review suggestions likewise
do not become backlog merely because their GitHub threads remain unresolved.

Authorization standard input is one JSON object with exactly the five string
fields shown here; `action` is the uppercase operator intent, not the lowercase
CLI verb:

```json
{"action":"ENROLL","issued_at":"2026-07-16T15:00:00Z","nonce":"example-nonce","operator_id":"operator:example","reason":"initial workspace enrollment"}
```

For `register adopt`, `rebind`, or `rotate`, replace `ENROLL` with `ADOPT`,
`REBIND`, or `ROTATE` respectively. For standalone `workspace activate`, use
`ACTIVATE` and canonicalize the complete JSON bytes. `issued_at` must be a real
RFC 3339 UTC timestamp ending in `Z`; the other values must be non-empty and
trimmed. Extra fields, duplicate fields, non-string values, and an action that
does not match the explicit CLI verb are rejected before mutation.

The existing Graphify `0.9.16` extraction, cache, build, watch, export, and
query implementation remains the only graph engine. A workspace-enabled build
is one `graphifyy` distribution based on the exact upstream commit
`a0e4a1c6bd3a99edfdd84ad30927003f51face6a`; the workspace layer does not copy
or fork engine logic inside the package.

## Contract authority

- JSON Schemas under `graphify/workspace/schemas/v1/`,
  `graphify/workspace/schemas/cli/v1/`, and
  `graphify/workspace/schemas/cli/v2/` are normative for the structural shape
  of durable documents, CLI requests and receipts, versioned status output,
  and the TOML-to-object representation of repo policy. Registration, identity-
  maintenance, active-source activation, sync request/receipt, and one-shot
  query request/result contracts are CLI v1; status JSON is schema v2.
- `graphify.workspace` supplies dependency-free canonical reference models,
  exact v1 rejection, SHA-256 inputs, and journal frame encoding. Its
  cross-field and cross-document validation is normative where JSON Schema
  cannot express relational invariants such as keyed ordering, digest binding,
  revision binding, or exact compensation coverage.
- `graphify.workspace.persistence`, `.identity`, `.registry`, `.leases`,
  `.generations`, `.journal`, `.pointers`, and `.gc` implement the P2/P3
  runtime boundary. Within that boundary, `.generations` and `.leases` also
  own P5B2b0's bounded internal staged-build, successor-lease, and stale-
  abandonment recovery. `.sync` composes those existing APIs for the sole
  public code-only structural sync path. `.adapters` and `.freshness` implement
  the bounded P4 read-only engine/query boundary. `.semantic_queue` implements the bounded
  P5A durable queue and certification boundary. `.composition` owns the
  bounded, no-follow read of installed runtime authority and wires the existing
  stores without duplicating their persistence behavior. `.cli` exposes the
  P5B2a registration command plus the bounded rebind/rotation identity-
  maintenance slice and standalone active-source activation with bounded
  authority, authorization, policy, Git-discovery, and four-part CAS inputs,
  the P5B2b sync request/receipt transport, and the P5B2c
  one-shot query transport; it reuses `.identity`, `.registry`, `.sync`, and
  `.freshness` without adding another persistence path. Lifecycle mutation
  fails closed outside
  non-elevated macOS on local APFS; tests use an explicit injected capability
  seam and disposable external state roots.
- Fixtures under `tests/fixtures/workspace/v1/` freeze positive, negative,
  canonicalization, version-rejection, compensation, and rollback examples.
- Candidate artifacts are built by `python -m tools.workspace_artifacts build`
  with a fixed source epoch and explicit output root.

## Documents

- [Workspace governance](governance.md)
- [Accepted governance receipts](receipts/)
- [Architecture](architecture.md)
- [State contracts](state-contract.md)
- [P3 runtime](p3-runtime.md)
- [P4 adapter and freshness](p4-adapter-freshness.md)
- [Compatibility and artifacts](compatibility.md)
- [Installation and rollback](installation.md)
- [Migration boundary](migration.md)
- [Threat and support model](threat-model.md)
- [Verification](verification.md)

Any change to a v1 field, enum, canonical encoding, or invariant requires a new
schema version or an explicitly compatible additive revision. Unknown versions
fail before a state-changing operation. P3 adds internal canonical envelopes
for capacity reservations, journal heads, and GC recovery without adding a
public v1 schema member or field.
