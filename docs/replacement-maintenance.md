# Replacement maintenance contract (#147)

This slice defines the maintenance operation and modeled authority lifecycle for
the [canonical replacement candidate](canonical-replacement.md). It implements
no privileged broker, filesystem authority store, authenticated IPC, process
containment, account setup or launcher control. Modeled evidence is not native
authority. The shipping maintenance entry point continues to refuse execute,
resume and rollback before replacement mutation. No request field, environment
variable, model instance or confirmation flag enables it.

## First deployment profile

The first proposed native profile is a cold-start, enrolled, single-purpose
macOS/APFS environment. An administrator controls the enclosure, canonical
parent, external archive/control parents, installed runtime and authority store.
The authority store is outside every source and output root. Only the broker
may launch graph processes under the dedicated non-login writer identity.
Administrator and kernel integrity are assumptions, not properties supplied by
the model.

No ordinary graph writer may have been admitted during the current boot before
maintenance starts. Admission of the first ordinary writer must be recorded
before it starts; restarting a broker cannot clear that history. Unknown launch
routes, retained writable descriptors, unaccounted descendants, descriptor
export or uncertain enrollment refuse maintenance admission. This profile does
not support online draining of an ordinary desktop's unknown process population.

Changing permissions and renaming files do not revoke existing writable file
descriptors. A process census, non-login shell, advisory lock, stopped launchd
job or disabled service is not proof of complete exclusion. A native mechanism
establishing the complete process family and absence of descriptor export is
still an unmet feasibility prerequisite. If it cannot be established, every
shipping replacement path must remain disabled.

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

A future broker grant must bind the descriptor to enrollment, boot and operation
epochs, the pinned runtime and coordinator incarnation, root/parent/output
identities, old-tree inventory and its supervised process family. It must travel
through authenticated broker-created IPC. Serialized data or possession of a
model object is insufficient. None of that native transport is implemented here.

There is one maintenance slot for the whole enrolled target. Two different
journals or requests cannot independently acquire overlapping graphs. Existing
journal-slot and rename-aware inode locks remain additional coordination checks;
their ownership and identity checks are not relaxed.

## Lifecycle and release

| State | Contract |
| --- | --- |
| `HELD` | Deny ordinary launches. Missing or uncertain native authority starts here. |
| `GRANTING` | Validate enrollment and exact selection; persist the new epoch before starting a coordinator. |
| `ACTIVE` | Admit one supervised coordinator and reject competing grants. |
| `RECOVERY_HELD` | Keep exclusion after failure, timeout, channel loss or uncertain termination. |
| `RELEASE_READY` | All admitted processes have exited, temporary access is withdrawn and independent terminal validation passed. |
| `RELEASED` | A separate durable release record permits approved compatible clients. |

Handle closure and context-manager exit never release service. Revocation or
expiry does not revoke an already-open descriptor or an in-flight system call.
A new epoch cannot replace proof that the old coordinator and all permitted
descendants have terminated. If that proof is unavailable, retain the hold.
Across reboot, invalidate grants and retain denial; stale frozen identities are
not refreshed or translated. This slice supplies no cross-boot recovery.

Successful publication is only a completion notification. Release requires:

1. Confirm that the coordinator and every permitted descendant have exited and
   no writable descriptor remains exported.
2. Withdraw temporary maintenance access to archive/control ancestors.
3. Independently validate the closed native receipt, canonical identity,
   provenance, completed journal and exact preserved old-tree inventory.
4. Persist an idempotent release record, then allow approved compatible clients.

The release record is separate from replacement's exact generation-one retry.
A later ordinary generation can legitimately change that receipt. A release
retry must reconcile its own record and supported current native lineage; it
cannot re-run generation-one publication or mint a new grant.

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

The protected authority registry is separate from coordinator-owned journals and
locks. The existing coordinator lock requires the real UID of its caller.
Temporary access on administrator-controlled ancestors must permit the
coordinator's inventory and cross-parent rename operations without changing the
preserved old tree's ownership, modes or bytes. Close and reap that process
family before withdrawing access and admitting normal clients.

Normal clients need read access to applicable target guards and journals; the
preserved archive stays inaccessible for ordinary mutation. Do not weaken the
lock ownership check or make the complete control tree inaccessible as a
substitute for a defined handoff. The final output remains `graphify-out`.

## Practical qualification plan

The contract/model checks cover selector drift, immutable capture, competing
requests, stale epochs, failure holds, persistence failure, uncertain process
termination, explicit release ordering, later-generation release retries and
unrecovered rollback. Their witnesses and persistence are modeled. They prove
neither OS custody nor real restart inhibition.

Before a usable native backend, qualify the following on newly created,
disposable macOS/APFS fixtures, bound to the exact coordinator, broker, runtime,
OS, identities and launch policy. Existing user graphs and retained #147 VMs
are not fixtures.

| Group | Required outcome |
| --- | --- |
| Enrollment and containment | Establish exclusive launch routes, complete process-family accounting and no writable-descriptor export; otherwise refuse admission. |
| Grant admission | Reject altered/stale/copied selectors and epochs; different journals targeting one graph have at most one winner. |
| Legacy launch attempts | Deny every sanctioned legacy restart route during maintenance and after release. |
| Coordinator/broker failure | Death, lost communication, timeout or a suspended predecessor cannot admit another writer or release service. |
| Namespace interruption | Exercise every finite archive/install/publication boundary and relevant rename/sync failure; preserve exact objects and selectors. |
| Rollback interruption | Retain both trees and quarantine; finish unrecovered with service held. |
| Successful release | New closed graph is usable at canonical `graphify-out`; complete old-tree identity/content preservation and archive mutation denial pass. |
| Retry and reboot | Release retries remain idempotent after generation two; reboot invalidates grants and stale bindings refuse without translation. |

Run each selected scenario once on the final candidate. Stop on an unexplained
failure and retain the smallest complete reproducer. Store source hashes,
commands, environment, identities, state transitions and before/after inventories
with each receipt. Reuse earlier evidence only while its relevant inputs remain
unchanged. Existing checkpoint A14 does not qualify these new boundaries.

Process-exit checks make no power-loss claim. These bounded checks do not remove
the canonical candidate's existing native enablement requirements. Local focused
checks are not full PR readiness; any later readiness/delivery task must follow
the repository's current validation routing. This slice authorizes no live
replacement, runtime installation, VM launch or PR delivery.
