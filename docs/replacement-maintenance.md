# Replacement maintenance contract (#147)

This slice defines the maintenance operation and modeled authority lifecycle for
the [canonical replacement candidate](canonical-replacement.md). It implements
no privileged broker, filesystem authority store, authenticated IPC, process
containment, account setup or launcher control. Modeled evidence is not native
authority. The shipping maintenance entry point continues to refuse execute,
resume and rollback before replacement mutation. No request field, environment
variable, model instance or confirmation flag enables it.

## Broker-only candidate and first deployment profile

The selected design candidate is broker-only filesystem mutation in a cold-start,
enrolled, single-purpose macOS/APFS environment. It is not a qualified backend.
The administrator, kernel, authority controller and complete mutator runtime are
trusted. If the mutator itself must be treated as hostile, this candidate does
not resolve containment. A closed enclosure for untrusted writers remains an
alternative with an unmet complete-holder/export enforcement prerequisite.

The authority controller owns the protected registry and exclusive launch policy;
it admits one trusted, one-shot mutator under a dedicated identity. That mutator
performs all selected target, stage, archive, quarantine and journal mutation.
It runs no child processes, plugins or caller-selected code. Workers prepare or
submit bounded data and receive no protected filesystem capability. A separate
verifier must have no authority to mutate the objects it validates. Permanent
registry and launch authority remains with the controller after mutator exit.

An administrator controls the canonical parent, external archive/control/stage
parents, runtime and authority store. The authority store is outside every source
and output root. Account, launch, namespace and IPC enforcement must prevent
unrelated code from obtaining the mutator identity or protected access. A non-login
shell alone does not prove this. Native deployment and enforcement are absent.

No ordinary graph writer may have been admitted during the current boot before
maintenance starts. Admission of the first ordinary writer must be recorded
before it starts; controller restart cannot clear that history. Unknown launch
routes, retained mutation capabilities, unaccounted processes, capability export
or uncertain enrollment refuse maintenance admission. This profile does not
support online draining of an ordinary desktop's unknown process population.

Changing permissions and renaming files do not revoke existing writable file
descriptors. A process census, non-login shell, advisory lock, stopped launchd
job or disabled service is not proof of complete exclusion. Exclusive mutator
custody and absence of protected capabilities in workers remain unmet native
prerequisites. A closed enclosure instead needs a supported mechanism accounting
for every recipient and preventing export beyond its retirement domain. Every
shipping replacement path remains disabled.

The retained [#266 counterexample](qualification/issue147-app-sandbox-fd-export-20261102.md)
records a 52-byte write through an exported descriptor after sender and broker
death. Its exact profile/channel failed containment; the result is not rewritten
or rerun to obtain a pass. It does not rule out every App Sandbox construction.
The same harness calls `fchmod` through its `O_RDONLY` verifier descriptor before
transfer. Capability accounting must cover metadata and namespace effects as well
as byte writes; an open-mode label alone is insufficient.

## Worker boundary and deployment prerequisites

`MaintenanceBroker.submit` describes a future whole-operation, data-only
submission boundary, not an implemented transport. It must not yield a capability
that lets the caller run the current coordinator with temporary filesystem access.
The existing `_maintenance()` hook remains an unconditional refusal. Replacing
that refusal with a context-manager grant is not this broker design.

A native adapter must authenticate requests and bind them to independent approval,
the exact operation, broker policy and process incarnations. A signing identity or
transport connection alone grants no operation authority. The trusted mutator
must independently validate frozen selectors, source/payload bindings and all
existing guards under its actual UID; the real-UID lock check is not relaxed.
Forward payloads are bounded copied bytes bound to the seal. Rollback supplies no
forward payload. There is no arbitrary open, rename, command or handle-returning
RPC. Acknowledgment and successful publication cannot release service.

Workers receive no protected descriptor, directory handle, shared writable mapping,
access-granting bookmark or equivalent resource. Reject resource attachments and
unknown protocol fields rather than treating an attachment as data. UNIX rights
transfer, inherited handles, Mach/XPC resources, shared mappings, helper services
and persistent grants must be closed or independently shown unavailable at the
protected boundary. A transport name or sandbox entitlement is not that proof.
Apple documents [XPC descriptor duplication](https://developer.apple.com/documentation/xpc/xpc_fd_create(_:))
and [bookmark-based access sharing](https://developer.apple.com/documentation/security/accessing-files-from-the-macos-app-sandbox).

Before native implementation is usable, deployment needs all of the following:

- Exclusive administrator-controlled mutator identity and launch routes, including
  restart and on-demand routes; no unrelated code may run as that identity.
- An administrator-controlled interpreter, dependencies, imports and configuration,
  bound as the complete trusted runtime, with no writable code-loading surface.
- Protected ancestor and alias admission that denies worker mutation while leaving
  compatible clients the guard/journal read access they need after release.
- Independent cold-start enrollment and prior-holder accounting. Existing graphs,
  retained fixtures, unchanged paths or a process census cannot establish it.
- Authenticated, resource-free transport, protected persistent history and native
  evidence for failure, restart, retirement and release ordering.

These are deployment prerequisites, not account/service setup instructions or
authority to change this host. Namespace durability, rollback, power-loss limits
and the parent's other native acceptance requirements remain independently open.

## Exact operation and authority boundary

The operation descriptor freezes the graph request and action-specific
selectors. Execute/resume select the preview and seal; resume additionally binds
the selected journal observation. Rollback selects the journal digest, quarantine
path and parent identity, and applicable current-new-tree digest. Caller-owned
mutable containers must not change a captured operation after admission.

Explicit CLI resume requires `--journal-sha256`; the Python `resume` interface
requires `expected_journal_sha256`. Both recheck that read-only selection under
the journal-slot lock before replacement mutation. Execute retains its existing
exact-retry behavior. Rollback rechecks its selected quarantine parent after
maintenance admission and before registering or moving data there.

A future internal admission must bind the descriptor to enrollment, boot and
operation epochs, the complete pinned runtime, launch/namespace/transport policy,
controller and one-shot mutator incarnations, root/parent/output identities and
old-tree inventory. Worker requests must cross authenticated data-only transport;
the internal model handle is never an exported filesystem grant. Serialized data
or possession of a model object is insufficient. No native transport ships here.

There is one maintenance slot for the whole enrolled target. Two different
journals or requests cannot independently acquire overlapping graphs. Existing
journal-slot and rename-aware inode locks remain additional coordination checks;
their ownership and identity checks are not relaxed.

## Lifecycle and release

| State | Contract |
| --- | --- |
| `HELD` | Deny ordinary launches. Missing or uncertain native authority starts here. |
| `GRANTING` | Validate enrollment and exact selection; persist the new epoch before starting a mutator. |
| `ACTIVE` | Admit one trusted mutator with explicit broker-only boundary evidence; reject competing admissions. |
| `RECOVERY_HELD` | Keep exclusion after failure, timeout, channel loss or uncertain termination. |
| `RELEASE_READY` | The mutator has exited, requests/effects are quiesced, protected capabilities are retired, temporary access is withdrawn and independent terminal validation passed. |
| `RELEASED` | A separate durable release record permits approved compatible clients. |

Handle closure and context-manager exit never release service. Revocation or
expiry does not revoke an already-open descriptor or an in-flight system call.
A new epoch cannot replace exact mutator retirement and quiescence evidence. An
in-flight request may have had an effect; stop new steps and reconcile that effect
under the selected operation. If proof is unavailable, retain the hold. Do not
infer that a timeout cancels a system call.
Across reboot, invalidate grants and retain denial; stale frozen identities are
not refreshed or translated. This slice supplies no cross-boot recovery.

A persistence exception can follow a durable write. The model retains the
attempted epoch and operation selection in `RECOVERY_HELD`; it does not infer
that the attempt was absent. No successful grant is returned, and subsequent
admission still requires the prior cohort's exact retirement evidence.

Successful publication is only a completion notification. Release requires:

1. Confirm exact mutator exit, request/queue quiescence and reconciled in-flight
   effects. Confirm all temporary protected handles/mappings are closed and no
   worker holds protected content, metadata or namespace mutation authority.
2. Withdraw temporary maintenance access to archive/control ancestors.
3. Independently validate the closed native receipt, canonical identity,
   provenance, completed journal and exact preserved old-tree inventory.
4. Persist an idempotent release record, then allow approved compatible clients.

The release record is separate from replacement's exact generation-one retry.
A later ordinary generation can legitimately change that receipt. A release
retry must reconcile its own record and supported current native lineage; it
cannot re-run generation-one publication or mint a new grant.

`BrokerAdmission` and `BrokerRetirement` are caller-supplied simulation witnesses
bound to the exact operation and identity. Missing boundary facts deny modeled
admission; a reap without complete retirement evidence retains a hold. They do
not inspect accounts, processes, IPC or OS permissions.

The model's `restart` transition retains the attempted epoch, mutator history,
first-writer latch and exact prior operation/release records while recording a
fresh controller incarnation and retaining denial. It does not deserialize native
state or prove old controller exit. Failed persistence acknowledgments retain the
attempted state conservatively because the write may already have happened. No
failed acknowledgment returns admission authority. Post-restart release
reconciliation remains a native prerequisite; an old controller cannot reuse a
release to restart service. No boot identities are translated or refreshed.

A retired forward operation retains its preview and seal on resume. An
interrupted rollback retains its complete action and retention selection; it
cannot switch back to forward publication or choose a new quarantine. Terminal
validation must match the selected action: a fresh replacement has a different
directory identity, and rollback restores the selected original directory.

`ROLLED_BACK_UNRECOVERED` is terminal and service remains held. Restoring the old
name preserves its old identity failure. It never permits automatic forward
resume or ordinary writer restart, even though the generic parent guard accepts
that terminal journal state.

## Permission handoff and preservation

The protected authority registry is separate from mutator-owned journals and
locks. The existing coordinator lock requires the real UID of its caller.
Temporary access on administrator-controlled ancestors belongs only to the trusted
mutator. It must permit inventory and cross-parent rename without changing the
preserved old tree's ownership, modes or bytes. Do not grant that access to an
unprivileged planner or worker. Retire the mutator and all operation effects before
withdrawing temporary access and admitting normal clients.

Normal clients need read access to applicable target guards and journals; the
preserved archive stays inaccessible for ordinary mutation. Do not weaken the
lock ownership check or make the complete control tree inaccessible as a
substitute for a defined handoff. The final output remains `graphify-out`.

## Practical qualification plan

The contract/model checks cover selector drift, immutable capture, competing
requests, stale epochs, failure holds, persistence failure, uncertain process
termination, broker policy/identity drift, capability-free worker boundaries,
incomplete retirement, controller restart holds, explicit release ordering,
later-generation release retries and unrecovered rollback. Their witnesses and
persistence are modeled. They prove
neither OS custody nor real restart inhibition.

Before a usable native backend, qualify the following on newly created,
disposable macOS/APFS fixtures, bound to the exact controller, mutator, runtime,
OS, identities and launch policy. Existing user graphs and retained #147 VMs
are not fixtures.

| Group | Required outcome |
| --- | --- |
| Enrollment and containment | Establish exclusive trusted-mutator launch custody, no mutator children, data-only authenticated transport and no protected worker capabilities; otherwise refuse admission. |
| Grant admission | Reject altered/stale/copied selectors and epochs; different journals targeting one graph have at most one winner. |
| Legacy launch attempts | Deny every sanctioned legacy restart route during maintenance and after release. |
| Mutator/controller failure | Death, lost communication, timeout, uncertain persistence or a suspended predecessor cannot admit another writer or release service. |
| Namespace interruption | Exercise every finite archive/install/publication boundary and relevant rename/sync failure; preserve exact objects and selectors. |
| Rollback interruption | Retain both trees and quarantine; finish unrecovered with service held. |
| Successful release | New closed graph is usable at canonical `graphify-out`; complete old-tree identity/content preservation and archive mutation denial pass. |
| Retry and reboot | Release retries remain idempotent after generation two; reboot invalidates grants and stale bindings refuse without translation. |

Run each selected scenario once on the final candidate. Stop on an unexplained
failure and retain the smallest complete reproducer. Store source hashes,
commands, environment, identities, state transitions and before/after inventories
with each receipt. Reuse earlier evidence only while its relevant inputs remain
unchanged. Existing checkpoint A14 does not qualify these new boundaries.

## One bounded future boundary probe (not run)

After separate native authorization and prerequisite provisioning, use one fresh
external APFS fixture with synthetic target/sibling canaries, a trusted one-shot
mutator, an unprivileged worker, an outside receiver and an independent verifier.
Do not select an existing graph, retained VM or the #266 counterexample as a fixture.

1. Once, establish functional controls: an exact approved broker request changes
   the selected canary; worker path/metadata mutation is denied. Demonstrate the
   worker-to-receiver transport with an unrelated scratch descriptor.
2. Run one candidate operation. Attempt protected-handle requests and unauthorized
   selectors through the actual protocol. Inspect responses for resource
   attachments. Kill and reap the exact mutator; then let the surviving worker
   and receiver attempt content and metadata changes.
3. Restart the authority controller once. It must recover the hold and reject
   stale authority before more mutation. This is not a claim about all launchd
   routes, reboot recovery or native service-release qualification.
4. Retain source/runtime/policy hashes, exact identities, messages, waits,
   persistence ordering, errors and before/after byte and metadata inventories.
   Stop on the first unexplained result. Any protected post-retirement mutation
   is a counterexample; a blocked observation supports only this route/profile.

Run each selected exchange once, with bounded waits and cleanup limited to proven
fixture-owned actors. Failed controls are inconclusive. Preserve failures without
policy relaxation or retries for a passing result. Finish held; this probe grants
no release and does not qualify live replacement.

Process-exit checks make no power-loss claim. These bounded checks do not remove
the canonical candidate's existing native enablement requirements. Local focused
checks are not full PR readiness; any later readiness/delivery task must follow
the repository's current validation routing. This slice authorizes no live
replacement, runtime installation, VM launch or PR delivery.
