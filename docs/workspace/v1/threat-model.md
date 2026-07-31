# V1 support and threat model

## Supported boundary

V1 targets one non-elevated user on macOS with lifecycle state on local APFS.
State, staging, generations, pointer temporaries, and recovery records must use
supported same-filesystem persistence primitives.

Detectable unsupported conditions fail closed:

- NFS, SMB, FUSE, and other network state roots;
- Windows or Linux lifecycle operation;
- root/elevated execution;
- shared multi-user state; and
- pre-login/system-daemon operation.

Watch supervision is login-scoped. Automatic online GC and historical certified
query are deferred. P3 requires an explicit validated runtime `CapacityPolicy`
for every allocation and GC operation. It has no defaults and adds no public
config field; operators must supply global/per-workspace byte and generation
limits plus a free-space reserve threshold.

## Durability claim

The P2/P3 persistence implementation handles process death,
injected short writes,
`EINTR`, `ENOSPC`, `EDQUOT`, `EIO`, failed sync, failed rename, and clean reboot
after a successful durable-write completion. Later lifecycle records must reuse
the same boundary. Sudden hardware power-loss durability is not claimed.

P3 executes generation, journal, pointer, and explicit offline-GC transitions.
P4 adds read-only adapter/freshness operations and external-output structural
builds. P5A adds only durable semantic desired-work reconciliation, fenced
claims, bounded retries/dead letters, and stable-watermark certification.
P5B2a adds only the initial enrollment and verified clone/fork adoption command.
P5B2b0 adds internal request-bound staged-build recovery and terminal stale
abandonment. P5B2b adds only provider-neutral structural
`workspace sync --code-only`; all other public workspace commands, service,
installation, route, publication, and live-cutover transitions remain absent.

Enrollment creates the durable per-workspace fence floor. Losing all initialized
workspace records is treated as corruption, never as permission to restart the
counter. Lease ownership is bound to OS-owned boot and process-start identity;
caller-provided identity cannot assert a reboot or reuse a live PID.

## Protected risks

V1 is designed to protect against accidental corruption, crashes, stale and
concurrent processes, untrusted corpus contents, path tricks, secret leakage,
and artifact mismatch/substitution relative to a locally frozen trusted
manifest.

State-root policy requires expected ownership, `0700` directories, `0600`
mutable records, exclusive creation, safe umask/ACL behavior, descriptor-relative
no-follow traversal, regular-file-only payloads, path containment, and rejection
of links and special files.

Registration requires an explicit `enroll` or `adopt` verb, a canonical repo
UUID, an expected registry revision, and action-matching operator authorization
on standard input. Source discovery scrubs ambient `GIT_*` overrides and reads
the repo policy through descriptor-relative no-follow traversal. The command
cross-checks the requested UUID before registry mutation, exposes no source or
state path, authorization detail, credential, or exception text, and maps stale
CAS, collision, authority, corruption, and runtime failures to stable codes.

Code-only sync accepts no provider, model, endpoint, credential, or path on
argv, stdin, or ambient configuration. A bounded canonical request must carry
every capacity and registry/source/lease/pointer CAS input explicitly;
duplicate, unknown, noncanonical, inferred, or stale authority fails before
sync mutation. The request digest binds the durable staged lifecycle. Engine
scratch and output remain beneath generation-owned external staging, provider
environment cannot select a backend, and network access is outside the
command's authority. Public receipts and classified failures omit exception
text, secrets, credentials, and absolute private paths.

P5B2b0 installs the exact staged request before `BUILD` lease acquisition and
treats every nonterminal staged record as a barrier to ordinary workspace
mutation. The request, lease, staged state, workspace, and generation identities
must agree before recovery consumes source payload content or commits lifecycle
state. Selected-source discovery may first read the recorded checkout and Git
metadata, but remains read-only. Each live staged lease also binds the caller
attempt SHA-256, so a second caller in the same process cannot inherit commit
authority from process-owner equality alone.

Recovery may finish exact independently durable certification. Terminal
abandonment instead requires canonical drift in the active source, migration
epoch, pointer CAS, compatibility, semantic source epoch, or trusted source
observation. Source unavailability alone is insufficient, and a durable
abandonment intent precedes destructive cleanup. Existing-only status/doctor
inspection exposes a bounded staged summary; every nonterminal lifecycle is a
visible recovery barrier with `safe_to_query=false` and an exact-resume action.
Terminal promoted or abandoned records do not create a false barrier, while
corrupt or contradictory staged state fails closed.

The public pointer-repair dry run is an existing-only, no-write inspection: it
opens no missing lock or state path and allocates no lease or fence. Its bounded
canonical output exposes only verified generation references, decision facts,
and redacted classifications; it excludes authorization, owner/fence data,
paths, raw records, environment values, and errors. Execute requires the
SHA-256 of those exact preview bytes (including the final newline), canonical
`REPAIR_EXECUTE` authorization, fresh CAS-bound `REPAIR` authority, and a
second exact decision under the required locks before mutation. This blocks a
stale preview, substituted candidate, authority drift, or post-preview pointer
change from selecting a repair target. `PointerStore` verifies receipts and
payloads, preserves monotonic revision evidence, and quarantines only corrupt
generations not referenced by the repaired pointer.

Repair is not recovery authority for every external-state fault. A valid GC
intent remains an explicit GC-reconcile barrier. Nonterminal/corrupt staged
state, semantic-queue corruption, registry or lease corruption, and unsafe
paths fail closed with their corresponding status action. A commit-unknown
repair does not permit replay: status inspection plus a fresh preview/request
pair is required. Doctor remains a read-only observer and cannot turn an
inspection action into a mutation.

Operational corpus processing will use a cleaned allowlisted environment,
network denial by default, bounded CPU/memory/file/time resources, read-only
source, staging-only writes, and host-agent instruction/data separation.
Backend endpoints and credentials are operator configuration, never repo policy;
secrets are excluded from argv and persisted state.

The contract-only host-agent semantic-worker child narrows that future boundary
further. Its only frozen argv is `graphify workspace semantic-worker --stdio`.
The caller must state an already-active host agent explicitly; the transport
passes no named backend and never imports or calls `graphify.llm` provider
discovery or dispatch. Credentials, provider files, ambient backend variables,
network availability, and an absent API key are neither capability nor
authority. Source bytes are read-only untrusted data and are never returned in
public result frames.

One long-lived process retains one OS-derived semantic owner and fence across
claim, bounded checkpoints, and terminal completion or failure. This prevents
separate subprocesses, a replaced caller, source activation, migration, lease
expiry, or a successor attempt from inheriting semantic commit authority.
Successful `UPSERT` output is treated as untrusted until the existing semantic
fragment validator and sanitizer accept it; `DELETE` accepts only the exact
fieldless tombstone. An immutable canonical result envelope is then
installed under private external workspace staging, its exact bytes and digest
are reopened and verified, and that digest is persisted in and revalidated
against the live claim checkpoint. Queue completion before those checks is
forbidden.

The result envelope and public receipt omit credentials, source content,
private absolute paths, raw exceptions, provider/model data, and lease owner or
fence details. Same-path different-byte installation, unsafe paths, links,
special files, oversized fragments, identifier/path tricks, unknown fields,
stale claims, and epoch drift fail closed. Orphan result staging is not cleanup
authority. If queue completion may have committed before its terminal public
frame, the absence of a durable queue/result association is reported as
commit-unknown rather than inferred success.

P5A treats semantic work and its outputs as untrusted until exact reconciliation
and generation sealing. A worker cannot claim work without an accepted
`SEMANTIC_CLAIM` lease and an explicit live capability decision. At the claim
mutation boundary, the queue resolves the registry-selected active source,
safely reads and validates its workspace configuration, and requires the
canonically revalidated caller configuration to match it exactly. The decision
is then derived from that active-source policy and the caller-stated live host-
agent/named-backend inputs. A foreign or same-UUID relabeled policy and an
arbitrary decision object are never authority. Host-agent use must be stated by
the caller; a headless backend must be explicitly named, policy-allowlisted, and
permitted network egress. Ambient provider or credential environment variables
are not capability or authority. Semantic-grant mutations retain the current
registry lock while nesting the workspace lock. Lifecycle queue mutations use
the normal stable-registry-snapshot then workspace-lock path, where the live
lifecycle lease excludes activation. An activation before claim validation
therefore makes the old lease stale instead of authorizing work from retired
policy. The active-source revision that produced desired work is persisted with
the queue, and retained work requires a new exact reconciliation after activation.

Claim IDs bind the workspace UUID, desired work, owner, fence, source revision,
and operation and migration epochs. This prevents a stale worker, expired
claim, cross-workspace collision, or replaced desired revision from committing
semantic completion. Deterministic operation
rotation prevents one operation class from starving the other. Explicit queue
item/byte limits, retry budgets, stable error classifications, and durable dead-
letter state bound poison-work and capacity amplification. Compaction retains
the reconciliation and watermark proof, so tombstone deletion cannot turn an
empty queue into false completeness.

Certification defends against a queue/build time-of-check/time-of-use race by
requiring two equal typed source observations, durably binding the completed
watermark to the exact staged-payload manifest, capturing the queue revision and
canonical-state hash, then revalidating both under the workspace lock before
generation sealing. A changed queue, mismatched observation pair, or different
staged manifest blocks certification. Before any receipt is accepted, the store
installs an immutable internal binding from the generation and request to that
revalidated queue view and manifest. Request and staged-manifest validation run
before that immutable boundary, so malformed input cannot poison a reserved
generation. A caller-controlled staged receipt without the binding cannot
bypass queue authority or cause the binding to be created. The binding remains
recoverable after later queue advancement, while the receipt separately recovers
generation installation and journaling. The claim remains local crash durability
and stale-process fencing; it does not authenticate an uncompromised same-UID
worker or semantic backend.

## Explicit non-claims

V1 does not resist a compromised source-control or CI system, or a malicious
same-UID actor able to replace both artifacts and the trusted manifest. The
journal is corruption-evident, not cryptographically authenticated against such
an actor. Cross-platform support, network filesystems, pre-login service,
automatic online GC, strict source-linearizable query, and inter-observation ABA
detection are deferred.

V1 does not claim automatic, online, service, arbitrary-history, registry,
lease, path, staged-build, semantic-queue, or GC repair from the public pointer
repair lifecycle. It adds no durable repair completion index and makes no
governance or receipt-acceptance claim.

The READY host-agent semantic-worker document is likewise not runtime or
acceptance authority. It does not claim named/headless backend execution,
network access, API-key handling, provider fallback, durable post-completion
receipt recovery, staging cleanup, sealed-input finalization, generation
certification or promotion, pointer mutation, retained service/watch behavior,
or full semantic sync.

Release channels are `dev`, `shadow`, `candidate`, `stable`, and `rollback` and
must promote identical digests. Later P5 work implements candidate publication
and login-service integration; P9 alone owns the real stable switch. P5A alone
does not make P5 complete.
