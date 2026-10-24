# #147 recovery design: independent checkpoint restoration

Status: accepted for implementation by the operator; candidate implemented,
[native A14 requalified after the root lifecycle fixes](../qualification/issue147-root-lifecycle-20261024.md).
Base: v8 commit `53becfa9819dda39c660553794d03132b9a0e7c8`.
Design authority: the operator's 2026-10-23 request to define recovery authority
and acceptance criteria. This proposal does not approve itself, implement a
recovery command, authorize a privileged installation, or admit an existing
output. [Current recovery rules](../transaction-recovery.md#output-identity-mismatch)
remain in force. The [candidate operator guide](../checkpoint-restoration.md)
distinguishes implemented interfaces from outstanding qualification gates.

## Decision and useful first result

Select **restoration of an independently approved checkpoint into an absent,
separately named output** for the first implementation. Preserve the failed
output. Keep automatic and in-place identity rebinding out of that PR.

The restored output receives generation 1, new ownership and receipt authority,
and explicit restoration provenance. It can preserve the checkpoint's semantic
graph without another provider call. It represents the approved checkpoint,
not a fresh extraction of today's corpus or continuity of the original output.
It cannot recover changes made after that checkpoint.

This is a narrower recovery result than resuming the original output. It avoids
making copied coordination records valid at another identity. If the required
product result is in-place continuation, this proposal is insufficient; the
separate continuity gate below must be resolved first.

[Certain] Current protocol, drainer, receipt and preserved-predecessor checks
bind the observed output identity. Local agreement or matching bytes supplies
no independent authority. See [#147](https://github.com/Villeneuve-Ventures/graphify/issues/147)
and the source map below. The completed image cycle observed no device change;
it does not establish reboot/remount causation or qualify recovery.

No independent approved checkpoint has been established for either historical
output from #145 or #219. They remain ineligible under this design. Do not
enroll a failed output by treating its internally consistent records as proof.
Without an eligible checkpoint, retain the current separately selected
trusted-corpus rebuild fallback.

## Authority model

### Principals and trust boundary

| Principal or object | Authority |
| --- | --- |
| Recovery administrator | Independently selects and approves a healthy, fully closed checkpoint; controls the protected store and its current head. |
| Normal Graphify process | Reads approved data, previews a restore and publishes only to its new output. Cannot approve a checkpoint, change the trusted head or obtain privilege. |
| Failed output, filesystem IDs, paths and local hashes | Diagnostic or integrity evidence only. Cannot approve recovery. |
| Protected checkpoint | Approved data for a new publication. Old tokens and receipts are inert provenance, never live authority at the destination. |
| New destination | A new identity and owner. Acquires authority through the restore transaction, never through the original output's identity. |

The first backend uses local OS access control, not a signing service. The
administrator's store, configuration and installed approval utility must be
owned by UID 0 and unavailable for writes by the normal Graphify UID or its
groups. The restore process must run unprivileged. Store paths come only from
that protected configuration; an output, environment variable, project config
or command-line store override cannot select a new trust root.

The store is outside both the original output tree and the new destination's
writable tree. Its separate administrator approval and write control provide
the authority boundary. An external drive is not required for device-ID
recovery; it also would not supply that authority by itself. Provisioning must
record custody and failure domain. A store on the same physical storage does
not protect against loss of that storage. This backend assumes a trusted
administrator, kernel and local storage administration. It supplies no protection
against their compromise or
administrator-authorized rollback of the whole host/store. Such a requirement
would need a separately anchored remote or hardware authority.

Reject symlinks, hard-linked record files, unsafe ancestors, write-granting ACLs,
disabled ownership enforcement, network filesystems and unqualified permission
semantics. A mode-bit check alone is insufficient. Native ACL and ownership
verification is a mandatory backend gate; absence of that check means refusal,
not a permissive fallback. Readers get only the minimum read/traverse access
needed; checkpoint contents need not be world-readable.

The approval utility must be installed from reviewed, trusted code and must not
import code or startup configuration from the selected project. Running an
editable checkout with elevated privileges is not a provisioning procedure.
No privileged daemon, automatic elevation, network access or new dependency is
selected by this proposal. Provisioning is an explicit administrator operation
outside ordinary update, watch, status and restore.

Qualify the initial authority backend on native macOS/APFS only. Locate its
configuration at the fixed system path
`/private/etc/graphify/recovery-authority.json`; pin and validate its protected
ancestor chain. The configured store location must be an absolute canonical
path. Missing configuration is a bounded unsupported-authority result, not an
installation prompt or a search through user directories. Other hosts refuse
authority capture/restore until their native permission and durability backend
is separately qualified; shared format/transaction tests may still run there.

### Checkpoint creation and freshness

Checkpoint approval happens **before identity failure**, while normal strict
admission succeeds. The administrator selects the source and holds its ordinary
output lock while validating closed protocol/drainer/receipt agreement and the
entire receipted inventory. Require no live transaction, token transition,
prepared work, pending predecessor, queued intent or recovery journal. A
lease timeout does not substitute for closure. Version 1 accepts only ordinary,
non-restored outputs with graph.json and manifest.json at the selected root.

Implementation correction: normal closure retains a preserved-complete
predecessor. Capture accepts this inert duplicate only when its validated
protocol, drainer and receipt digest exactly equal the current closed records;
all other predecessor states refuse. Its bytes join the checkpoint evidence.

Capture approved artifact bytes into an administrator-owned temporary slot;
validate the copied bytes against the admitted source and revalidate its pinned
identity and records before releasing the source lock. Do not accept arbitrary
imported JSON, a receipt-only backup, or a snapshot of the failed directory.

After releasing the source lock, acquire the store's exclusive lock. Commit a
new immutable checkpoint slot, then replace the current head using an exact
expected-head comparison. Concurrent approval losing that comparison refuses;
it must not overwrite the winner. Sync files and directories before the head
commit. A crash before the head commit leaves an unselected slot, not authority.
The authority publisher must never replace the stable lock object.

The protected current head records a strictly increasing approval sequence,
checkpoint digest and source ID. Revocation writes a new disabled head; it does
not delete history. Approval must refuse an older or duplicate generation for
the same source lineage. A new output lineage needs a new source ID. Backdated
approval or automatic fallback to a previous checkpoint is unsupported.

Freshness means **latest approved checkpoint**, not latest source generation.
Graphify does not mirror every normal publication into the store. Preview must
show the checkpoint's capture time, original generation and this possible data
loss. Restoration is an explicit choice to use that checkpoint. Claims of
zero data loss or a current source snapshot would be false.

### Bound records

Use strict, versioned records with exact fields, canonical encoding, bounded
strings/integers and SHA-256 domain separation. The implementation PR must
freeze byte encoding and schemas and supply golden vectors. Hashes bind the
approved data; OS-enforced control of the head grants authority.

| Record | Required binding |
| --- | --- |
| Protected configuration | Format/backend version, administrator UID, reader policy, exact store location and supported filesystem policy. |
| Checkpoint | Source ID and canonical root; capture time; original output identity, generation and transaction ID; exact protocol/drainer/receipt bytes and their digests; complete artifact names, sizes and digests; snapshot digest. |
| Current head | Source ID, approval sequence, enabled/disabled state, selected checkpoint digest and prior-head digest. |
| Preview plan | Request version, caller UID, source ID, exact head/checkpoint digests, original generation, selected root, destination parent identity and basename, transform version, complete publication inventory, limits and plan digest. |
| Restore claim/seal | Preview plan digest, new output identity, operation nonce, new owner/token/drainer binding and immutable staged inventory digest. |
| Published provenance | Source/head/checkpoint digests and approval sequence, original identity/generation/receipt digest, plan and transform versions, new output identity, new generation and before/after artifact digests. |

Original coordination files remain in the protected checkpoint as inert bytes.
They are not copied into the new output's active coordination namespace.
The protected head is authoritative for starting or resuming a restore. A
preview document, confirmation flag or claim copied from another output cannot
replace it. Checkpoints are reusable data approvals, not single-use permits;
separate explicit restores may create independent outputs. They never share an
owner token or claim that either output continues the old writer.

The preview binds a provenance recipe because the new output identity does not
exist yet. Preflight charges its largest supported encoding, claim, seal and
complete publication inventory before directory creation. After allocation,
the claim/seal bind the exact filled provenance bytes and new identity. Claim
and seal filenames are reserved coordination names, including case variants
and nested components; provenance is a receipted artifact.

## Eligibility and publication contract

1. Preview is read-only. Pin the protected store, take its shared lock, validate
   its current head and selected immutable slot, and hash every selected byte.
   Pin the canonical source root and destination parent without following
   symlinks. Require the original root path, a single new output basename and
   proven destination absence. Do not open the failed output. Return the plan
   digest and exact inventory, data-loss warning and rejection reason.
2. Execute takes the exact preview digest and reobserves every binding before
   creating anything. Any changed head, checkpoint, root, parent, request or
   destination absence refuses. No automatic re-preview or changed selection.
   Execute never adopts an existing destination. A distinct resume operation
   takes the original request and exact new-output/owner selectors; it follows
   the interruption table rather than repeating absent-output admission.
3. Hold the store's shared lock, then the destination-parent reservation lock,
   then the new output lock, in that order. Use descriptor-relative creation,
   exclusive absence checks and existing capability checks. Refuse an occupied,
   replaced or symlinked destination, including an empty existing directory.
   Require a private, caller-owned parent with qualified access control. Other
   same-UID processes must cooperate with the publication protocol.
4. Persist the exact restore owner/binding before publishing payloads. Pin that
   new identity at every reopen. Stage and seal the complete plan before active
   publication; a retry never restages from changed checkpoint data. During a
   partial restore, all normal reader/writer entry points reject the output.
5. Preserve graph nodes, edges and their attributes from the checkpoint. The
   only graph transformation is a versioned replacement of the managed graph
   watermark with the new generation-1 watermark. JSON encoding changes must
   preserve every other parsed graph value and be described by the transform
   version. Keep manifest bytes and all other approved artifacts exact. Do not
   execute templates, links, scripts or
   commands found in artifacts. Reject unsupported graph/manifest formats,
   ambiguous names and a collision with the reserved provenance filename.
6. Publish a receipt-bound restore-provenance artifact with the new payloads.
   Use new transaction ID, token, drainer, protocol and receipt records, all
   bound to the destination identity. The old receipt remains unchanged in the
   checkpoint; no validator is called with a fabricated old capability.
7. Reuse the existing staged publication, receipt-last, protocol completion and
   closure/retirement machinery where its invariants fit. Public admission
   requires complete validated receipt/protocol/drainer agreement and closure;
   a present graph or receipt alone is not a commit. Add exact restore fencing
   rather than treating this as receiptless legacy bootstrap.
8. A successful closed output may be queried as the restored checkpoint.
   Incremental updates use normal source-freshness and transaction admission;
   restore itself makes no source-freshness promise. No source relocation,
   provider invocation, corpus extraction, canonical-output replacement or
   background watch launch occurs as part of restore.

First-format limit: reuse the existing inventory/path/per-artifact bounds and
the 4,096-artifact / 1 GiB aggregate ceilings. Count provenance and retained
evidence against the relevant budgets; fail before claiming an unpersistable
plan. Apply a finite deadline and nonblocking/bounded lock acquisition. No
unbounded scan, retry loop or whole-store history traversal is permitted.

## Compatibility, migration and interruption

Add an explicit restore binding understood by all current transaction readers
and writers. The strict protocol field parser must make older readers refuse
that new field. Keep the original outputs and all four old identity guards
unchanged. This is new-output publication, not migration of old identities.

The restore binding is present in the new protocol/owner before payload
publication, requires the dedicated exact-plan finalizer while incomplete, and
binds the provenance artifact after completion. Subsequent generations must
carry the origin binding and immutable provenance forward under their normal
receipts. Generic takeover, cancellation, legacy bootstrap and prepared-owner
recovery must not consume an incomplete restore. A dedicated exact-request
resume path is required. First-version checkpoint capture rejects restored
lineages; recursive authority import is deferred.

Authority capture acquires source then releases it before locking the store.
Restore never locks the old source output. This prevents an inverse source /
store lock order. A shared store lock is held through destination closure so a
cooperating administrator cannot revoke or replace its head during publication.
Permission checks supply the write boundary; advisory locks only coordinate
cooperating processes ([Apple flock reference](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/flock.2.html)).

| Interruption point | Required restart behavior |
| --- | --- |
| Before destination creation | No destination mutation; preview again if any binding changed. |
| Creation before durable restore claim | Leave the unclaimed directory. Refuse adoption or deletion; use a different absent destination after review. No restored graph is admitted. |
| Claim before seal | Refuse generic recovery. Exact resume may use only its original owner and unchanged trusted head; if a complete sealed plan cannot be proved, preserve and stop. |
| Seal or partial artifacts before receipt | Readers refuse. Resume only the same pinned output, owner, seal and approved head; replay the fixed plan. |
| Receipt before protocol completion | Readers refuse. Resume validates staged/public bytes and exact new receipt before finishing the same protocol. |
| Protocol complete before closure/retirement | Resume completes validated closure only; no new generation or reimport. |
| Fully closed exact retry | Return the existing receipt after full validation. This reports prior completion, not renewed approval or present source freshness. |
| Head revoked/advanced before incomplete resume | Refuse further writes; preserve partial output and evidence. A fresh restore needs a new absent destination. |
| Output/parent replaced, identity unavailable or source data changed | Refuse; never follow the new object, rebind, roll back another writer or erase evidence. |

The fully closed case is a read-only result of the exact resume/status path;
it neither starts a restore nor needs the head to remain selected. All
incomplete resume paths revalidate the current trusted head before any write.
Checkpoint revocation stops new imports, not use of a previously closed output;
revoking already published data is a different product contract.

Each durable boundary uses the qualified file-and-directory synchronization
primitives, including the destination-parent creation entry. An fsync error is
a failed operation with retained state. Process-kill tests establish process
interruption behavior, not power-loss durability. Storage/power-loss claims need
separate native evidence. Python's descriptor and no-follow capabilities are
platform-dependent ([Python OS reference](https://docs.python.org/3.14/library/os.html));
unsupported semantics must refuse.

Same-UID malicious writes to the new output, changes reverted between
observations, host-root compromise and physical provenance of a substituted
volume are outside this backend's guarantees. The implementation must not
claim that stat checks or advisory locks fence arbitrary writers. Race tests
still must demonstrate refusal of observed replacement and cooperative writers.

## Acceptance criteria for the implementation PR

All rows are requirements, **not recorded passing tests**. Use disposable
fixtures and intercepted external calls. Snapshot contents and entry identities,
not only file counts or exit codes. Negative preflight cases create no output;
post-claim failures may leave only their documented partial new destination.
The failed output and protected checkpoint/head must remain unchanged by restore.

| ID | Scenario | Required proof |
| --- | --- | --- |
| A1 | Capture healthy, fully closed ordinary generation | Strict normal admission under source lock; copied artifact bytes equal the admitted inventory; protected approval commits after durable data. |
| A2 | Failed identity; malformed/mismatched artifacts; live, queued, pending predecessor or journal state | Capture refuses before approving a slot/head. An exact preserved-complete duplicate is inert evidence. No synthetic identity or missing-record exception admits it. |
| A3 | Valid approved checkpoint, absent destination, exact preview | Generation 1 closes under new identity; ordinary query works; graph content equality except watermark; other artifacts exact; provenance receipt-bound. |
| A4 | No checkpoint; self-declared local manifest; copied store; unsafe UID/mode/ACL/ancestor; same writable tree | Refuse authority before destination creation. Explicit user confirmation does not change the result. |
| A5 | UUID/inode/path/hash agreement; copied protocol/receipt/drainer/predecessor; replacement volume | Cannot enroll or rebind the failed output. Restore reads approved checkpoint data only and does not open the failed output. |
| A6 | Superseded/revoked head, previous checkpoint, changed preview or tampered slot | Refuse start/resume before further writes. Latest approved checkpoint is selected explicitly; no claim of latest source state. |
| A7 | Two administrator approvals or a capture crash | Exact-head comparison gives one winner; incomplete slot is never selected; approval sequence cannot regress through normal APIs. |
| A8 | Two restores to one destination; ordinary writer; parent/name retarget at every reopen | One owner; other requests refuse or report only the exact completed result. Never write through substituted directory or symlink. |
| A9 | Symlink, hard link, traversal, casefold collision, FIFO/device, reserved name, oversized record/inventory | Bounded refusal with no traversal or execution of supplied content. Exercise every limit at and beyond its boundary. |
| A10 | Preview with protected state and failed output snapshotted | Correct plan and reasons; no lifecycle, token, queue, provider, cache or evidence writes. Execute repeats all material checks. |
| A11 | Fault at every claim, seal, payload, fsync, receipt, protocol, close and retirement boundary | Fresh-process replay follows the interruption table; malformed/partial state is never admitted; repeat interruption does not duplicate publication. |
| A12 | Generic recover/update/watch/cancel during incomplete restore; wrong request/token/head on resume | Refuse before mutation. No legacy bootstrap, ordinary owner takeover or preserved-predecessor bypass. |
| A13 | Next normal generation; older supported binary; no restore feature installed | Origin/provenance survives under new receipt; older parser refuses new binding; current mismatches still refuse without the feature. |
| A14 | Installed unprivileged candidate, real separate administrator store, private data | Actual permission/ACL fence prevents the candidate changing authority. No elevated project imports, network/provider calls, automatic install or secret disclosure. Mocks alone do not qualify this row. |
| A15 | Process-kill/restart on native macOS/APFS; synthetic old/new device mismatch fixture | Confirm actual entry points, diagnostics and preservation. Label synthetic identity injection as synthetic; native no-change remount does not count as observed renumbering. |

Keep #147 open after this narrower implementation. Observed-renumbering and
reboot evidence, historical-output recovery, and in-place continuity remain
unresolved unless separately established. Do not require a host reboot to begin
unit work or represent a synthetic mismatch as filesystem qualification.

## Implementation boundary and delivery gate

Implement one end-to-end checkpoint/preview/restore slice: a narrowly scoped
authority-store module and administrator capture utility; unprivileged restore
module and explicit CLI; shared transaction binding/finalizer/provenance guards;
schema/golden vectors, adversarial fixtures, native installed proof and operator
guidance. Keep all runtime logic out of this design-only change. Do not create
a diagnostics-only PR and call recovery implemented; #221 already delivered
the diagnostic portion.

The operator accepted the selected new-output result, UID-separated administrator
backend and its prospective checkpoint requirement in the implementation request.
These are product/trust decisions. If that
backend is not acceptable, return to authority selection; do not substitute a
same-user registry, unprovisioned signing key or confirmation flag.

The implementation PR must include frozen schemas and exact API/error/CLI
behavior, every acceptance row mapped to tests or a stated blocking gap, and
the actual backend provisioning procedure. Missing native ACL/custody evidence
blocks backend qualification and any supported-recovery claim. A draft may
expose such a gap; it cannot report the feature ready. Implementation and a draft
PR are authorized by the subsequent operator request; native provisioning or
live recovery is not performed by product commands. A later operator request
authorizes the [A14 qualification harness](../checkpoint-qualification.md) in a
fresh disposable macOS/APFS environment. That trial does not authorize recovery
of retained historical outputs.

Run focused acceptance tests first, then the repository's applicable local
pre-push gates at the implementation revision; inspect the live gate composition
instead of copying a historical command list. Include ordinary recovery,
receipt, queue, bootstrap and portable-output regressions affected by the shared
transaction change, installed entry points, and graph refresh after code edits.
Delivery requires separate active-task authority. New PRs are drafts; verify
remote commit and PR once, report local results and CI not awaited, then stop.
No monitoring, merging, live recovery or cleanup follows from PR creation.

## Separate in-place continuity gate

In-place continuation would need an independent pre-failure enrollment and
freshness authority for the logical output, a trusted way to distinguish the
original mount/object from an indistinguishable clone, and one-writer fencing
across aliases. A protected content checkpoint solves none of those physical
continuity questions. An operator may instead authorize a new restoration, as
above; that is not evidence of the old object's continuity.

A future proposal must define revocation/replay and rollback behavior, migration
of every embedded identity/token/journal/predecessor binding, immutable old
receipt preservation, cross-record commit ordering, older-reader refusal and
crash replay. Path, APFS UUID, inode, birthtime, ctime, hashes, signatures without
a provisioned trust/freshness root, and `--force` are insufficient individually
or by local agreement. Do not introduce an identity-translation exception into
ordinary admission before that separate contract is accepted.

## Revision-bound source map

These links describe the base implementation, not proposed passage of tests:

- [OutputCapability and identity model](../../graphify/transaction.py)
  (`OutputIdentity`, `OutputCapability.validate`, `_require_output_identity`).
- [Receipt and publication](../../graphify/transaction.py)
  (`commit_generation`, `_validate_receipt_locked`, `commit_publication_plan`).
- [Predecessor and recovery guards](../../graphify/transaction.py)
  (`_predecessor_authority_from_json`, `recover_transaction`, `inspect_recovery`).
- [Existing bootstrap implementation](../../graphify/legacy_bootstrap.py) and
  [its separate contract](../legacy-bootstrap.md): reuse qualified primitives,
  not its receiptless admission predicate.
- [Local retention rule](../local-resource-retention.md): retain the original,
  checkpoints, partial destinations and final receipts; no deletion schedule.

The base contains no independent recovery authority or restore interface.
This design makes no claim that implementation tests, native backend checks,
independent design review or CI have passed.
