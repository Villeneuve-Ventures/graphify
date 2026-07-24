# Graphify workspace contract v1

Implemented contract scope through P5B2b provider-neutral code-only structural
sync, P5B2b0 staged
structural-build recovery, P5B2a initial workspace registration, P5B1 read-only
workspace status/doctor, P5A semantic queue, P4 adapter, and observed-current
library runtime for `graphifyy 0.9.16+workspace.1`. Durable state schema v1 and
runtime-manifest format v1 remain frozen; public status JSON is schema v2.

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
repairing state. P5B1 only consumes that file. Its candidate-backed atomic
installation and compensation proof remain P5C1 work. Later P5C retains
production query/service authority. P5B2a adds only
`graphify workspace register enroll` for initial
enrollment and `graphify workspace register adopt` for an already-enrolled
verified clone or fork whose retained history includes a root recorded at
enrollment. A shallow clone that omits that root fails the shared-history proof
until sufficient history is fetched. Both forms require the repo UUID, expected
registry revision, and a matching `OperatorAuthorization` JSON object on
standard input. The command requires the current working directory itself to be
the Git top level, ignores local Git replacement refs and legacy graft files,
cross-checks its bounded no-follow `.graphify/workspace.toml`, and never infers
adoption. It emits one canonical redacted receipt and writes only the existing
P2 registry, workspace, lock, and evidence records beneath the configured
external state root. The receipt's normative machine-readable schema is
`graphify/workspace/schemas/cli/v1/registration.schema.json`. Rebind, rotation,
activation, remaining mutation/query commands, repair, watch/service,
performance certification, and candidate publication remain later P5 work.
P5B2b0 adds the internal request-bound staged-build and stale-abandonment
recovery contract described in [State contracts](state-contract.md). P5B2b
exposes only `graphify workspace sync --code-only --request-stdin`, using a
bounded canonical JSON request, external generation-owned staging, the existing
fenced lifecycle, and one canonical redacted receipt. Status and doctor now
surface nonterminal staged-build recovery barriers through status schema v2.
Provider selection, networking, semantic execution, and every other workspace
command remain deferred.

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

| Area | Future owner | Stable boundary |
|---|---|---|
| Additional sync modes | Remaining P5B2 | Only provider-neutral structural `sync --code-only` is public. Semantic sync and any broader mode require separately reviewed authority, provider, redaction, and recovery contracts. |
| Remaining workspace commands | Remaining P5B2 | Query, migrate, rollback, GC, repair, rebind, rotation, activation, and other operator mutations require separately reviewed contracts and explicit operator intent. |
| Candidate runtime authority | P5C1 | Generate canonical `runtime-manifest.json` from the existing compatibility manifest plus explicit `SemanticQueuePolicy`, bind its exact bytes/hash to the immutable candidate, install it atomically only in isolated external-state fixtures, prove deterministic-failure compensation, and preserve P5B1's read-only loader unchanged. |
| Service, release, and resource proof | Remaining P5C | Watch/service supervision, publication, representative-corpus performance and resource accounting, record admission budgets, retained production query/service authority, and any shared workspace read-lock optimization remain waiting outside P5C1. |
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

For `register adopt`, replace `ENROLL` with `ADOPT`. `issued_at` must be a real
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
  and the TOML-to-object representation of repo policy. Sync request and receipt
  contracts remain CLI v1; status JSON is schema v2.
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
  P5B2a registration command with bounded authority, authorization, policy, and
  Git-discovery inputs, plus the bounded sync request/receipt transport; it
  reuses `.identity`, `.registry`, and `.sync` without adding another
  persistence path. Lifecycle mutation
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
