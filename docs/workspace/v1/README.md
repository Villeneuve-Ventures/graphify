# Graphify workspace contract v1

Status: P5B2a initial workspace registration, P5B1 read-only workspace
status/doctor, P5A semantic queue, P4 adapter, and observed-current library
runtime for `graphifyy 0.9.16+workspace.1`; the public v1 contract fields remain
frozen.

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
installation remains P5C work, alongside retained production query/service
authority. P5B2a adds only `graphify workspace register enroll` for initial
enrollment and `graphify workspace register adopt` for an already-enrolled
verified clone or fork. Both forms require the repo UUID, expected registry
revision, and a matching `OperatorAuthorization` JSON object on standard input.
The command discovers only the current Git top level, cross-checks its bounded
no-follow `.graphify/workspace.toml`, and never infers adoption. It emits one
canonical redacted receipt and writes only the existing P2 registry, workspace,
lock, and evidence records beneath the configured external state root. Rebind,
rotation, activation, remaining mutation/query commands, repair, watch/service,
performance certification, and candidate publication remain later P5 work.

The existing Graphify `0.9.16` extraction, cache, build, watch, export, and
query implementation remains the only graph engine. A workspace-enabled build
is one `graphifyy` distribution based on the exact upstream commit
`a0e4a1c6bd3a99edfdd84ad30927003f51face6a`; the workspace layer does not copy
or fork engine logic inside the package.

## Contract authority

- JSON Schemas under `graphify/workspace/schemas/v1/` are normative for the
  structural shape of JSON documents and the TOML-to-object representation of
  repo policy.
- `graphify.workspace` supplies dependency-free canonical reference models,
  exact v1 rejection, SHA-256 inputs, and journal frame encoding. Its
  cross-field and cross-document validation is normative where JSON Schema
  cannot express relational invariants such as keyed ordering, digest binding,
  revision binding, or exact compensation coverage.
- `graphify.workspace.persistence`, `.identity`, `.registry`, `.leases`,
  `.generations`, `.journal`, `.pointers`, and `.gc` implement the P2/P3
  runtime boundary. `.adapters` and `.freshness` implement the bounded P4
  read-only engine/query boundary. `.semantic_queue` implements the bounded
  P5A durable queue and certification boundary. `.composition` owns the
  bounded, no-follow read of installed runtime authority and wires the existing
  stores without duplicating their persistence behavior. `.cli` exposes the
  bounded P5B2a registration command and reuses `.identity` and `.registry`
  without adding another persistence path. Lifecycle mutation fails closed
  outside non-elevated macOS on local APFS; tests use an explicit injected
  capability seam and disposable external state roots.
- Fixtures under `tests/fixtures/workspace/v1/` freeze positive, negative,
  canonicalization, version-rejection, compensation, and rollback examples.
- Candidate artifacts are built by `python -m tools.workspace_artifacts build`
  with a fixed source epoch and explicit output root.

## Documents

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
