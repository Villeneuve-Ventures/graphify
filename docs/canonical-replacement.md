> Local split preparation: library contracts only. The replacement command and complete client integration are withheld until slice 5. Remaining later-layer statements describe the target candidate contract, not available entry points or validation of this prefix. Live replacement remains disabled; keep #147 open.

# Canonical replacement candidate (#147)

This local candidate builds fresh corpus-derived data and models publication of a
new generation one at the configured `graphify-out` path. It does not rebind an
old directory, approve old graph bytes, or implement filesystem continuity.
It is independent of direction-preservation #245 and R26 semantic collection.

**Live execution is unavailable.** No qualified maintenance backend ships with
this candidate. Execute, resume and rollback fail before filesystem mutation
unless an internal backend supplies the required maintenance capability. Tests
replace that private backend with an explicitly modeled fixture; a test pass is
not native qualification or proof that legacy clients cannot restart. There is
no environment variable, confirmation flag, or request field that enables it.

## Data profile

`fresh-corpus-codex-v1` / `core-v1` reads the current explicit source request
through bounded, rooted, no-follow SourceIO. Native AST extraction runs without
persistent caches. Selected document, paper and image inputs require complete
host-Codex fragments with exact source SHA-256 bindings; selected video is not
supported. The profile emits graph, manifest and source-root payloads. It does
not emit HTML, wiki or labels. It does not invoke provider labeling, hosted
deduplication, embeddings or conversion. Empty semantic results require an
explicit exclusion in this first profile rather than a fabricated concept.

IDs cannot collide across source files, including AST nodes; source coverage is
checked again after graph canonicalization. Edge confidence, finite scores and
relations follow the host extraction rubric and are validated before merging.
Edge and hyperedge records must be JSON objects. Hyperedge members must be
nonempty strings before set or topology checks. Each hyperedge requires at least
three distinct members, both in the fragment and after graph canonicalization.
Group IDs must be hashable before composition; anonymous groups and hashable JSON
scalar IDs retain their existing behavior. Construction must retain every admitted
group, including repeated and anonymous records, before the data can be sealed.
Malformed records return a structured blocked result before any seal or graph
is published.
Fragment node, edge and hyperedge containers must be lists. Node objects and
their nonempty string IDs are admitted before hashing; source ownership and
relationship identifiers are checked before membership or path operations.
Complete fragment values are limited to 128 nested containers, counting the
outer fragment list as one. Dictionary values and array members, including extra
metadata retained in the seal, are checked iteratively before recursive graph
composition or serialization. Exceeding this bound returns a structured blocked
result before sealing; the bound does not limit the number of sibling values.
Composition retains the builder's simple `Graph` or `DiGraph` representation.
Parallel relationships on one endpoint slot can collapse; undirected slots can
also collapse reciprocal relationships. The seal does not add multigraph or
lossless relationship preservation to that existing projection.

Fragment `backend=codex_host_agent` and `network_egress=false` fields describe
the approved caller's provenance claim. They do not cryptographically attest
which model produced text. The trusted host collects Codex results; the product
validates source coverage, digests and topology. A seal supplies integrity, not
independent authority to import an arbitrary old graph. Preview reconstructs
fresh data from the selected source and supplied fragments and compares the
entire seal, including consumed policy reads. The coordinator revalidates source
and sealed data before archiving the original output and again after installing
the new container. These checks do not freeze mutable source contents. Drift
detected after the namespace changes leaves publication held; exact resume with
the original seal requires matching source again, or explicit rollback is needed.

Use the read-only `bind-request` operation once to freeze strict device/inode
identities for the canonical, archive, control and stage parents. Those
`parent_identities` stay in the original request and are checked before locks or
state reads. Binding an existing request validates it; it never refreshes changed
parents. Copying a control tree to a new parent cannot confer replay authority.

Requests name schema, root, output, archive, stage, journal, include/exclude
globs, extra policy/Git roots, direction, extraction profile and artifact
profile. Output retains the `graphify-out` basename. Archive, stage and journal
are external to every source/policy root and cannot contain one another. Archive
and evidence slots, including rollback quarantine, cannot use the reserved
`.graphify_replacement_guard` basename or its exact recovery-backup names.
An evidence slot in the journal directory also cannot use the journal's
case-insensitive coordinator lock name. Admission refuses before operation effects.
Archive and output, including the preallocation/control parent, must share a filesystem.
Preflight checks existing output, archive and rollback-quarantine objects as well
as their rename parents, and refuses detected mount points before operation
effects. Detection uses Python's [`os.path.ismount`](https://docs.python.org/3.14/library/os.path.html#os.path.ismount);
it does not reliably detect same-filesystem bind mounts. Native mount/namespace
exclusion remains a qualification prerequisite.
Rollback quarantine is checked against all source/policy roots and rename devices
before any journal or guard change. Bounds include 100,000 preserved entries,
128 nested directory levels (`MAX_PRESERVATION_DEPTH`), 128 MiB per preserved
file, 2 GiB aggregate preserved bytes, 128 MiB sealed data
and serialized control objects, existing SourceIO read bounds and a 30-second
lock deadline. Preview measures the complete serialized initial journal,
including escaped inventory names, and reserves another 3 MiB + 64 KiB for
forward metadata. That allowance covers the duplicated request, container path,
a native owner bounded to 1 MiB, and fixed allocation and completion fields.
Replay checks the remaining allowance before forward effects. Every control
write checks its complete serialized size against the reader limit before
creation or replacement; oversized or unreadable existing state stays held.
A tree can be below the aggregate bound and still refuse because one file exceeds
its own bound. The inventory root has depth 0; directories at depth 128 and
files in them are admitted. A directory at depth 129 refuses with a structured
`PendingTransactionError` before it is opened. This conservative traversal cap
bounds recursive stack and descriptor use independently of the entry/byte caps;
opened descriptors still close on refusal, and identity and membership checks
still apply within the admitted tree.
Both directory-membership passes stop after the remaining child-entry budget
plus one name, before sorting. The separate `.` inventory record binds the
preserved root's device, inode, mode, UID and GID; it does not consume that child
budget. Child records also bind UID and GID, including directory ownership.
Rename-sensitive root timestamps are not persisted. Root or child metadata drift
changes the preview selector and refuses replay. Older previews or journals
without these fields are not automatically upgraded or admitted.
The profile's exact guard namespace is excluded from
source selection; it is never graph input.

A stable no-follow operation lock is keyed by the journal slot, not the request.
It must belong to the caller's real UID, forbid group/other writes and pass the
existing safe-ACL policy. Admission is repeated after the binding read and after
work; unsafe existing lock permissions or ACLs are refused, never repaired.
Initial journal creation is exclusive under that lock. A different request cannot
replace an existing winner, including a completed operation.
The control parent requires mutating admission before lock creation, so a
registered retained archive cannot be used to create another operation's lock.

The coordinator persists its request and preview before creating a private,
empty external container. Its allocation nonce and physical identity are
recorded before the original graph moves. An allocation crash leaves an external
orphan; that directory is never adopted or deleted automatically.

Under maintenance exclusion, dedicated inode locks survive a cross-parent
no-replace rename of the complete original tree to the archive. Both parents
are synchronized. The empty claimed container is installed at the absent
canonical name. Fresh transaction authority is created there. A distinct
`canonical-replacement-v1` binding hashes new receipt-bound origin metadata;
it is not a bootstrap or checkpoint binding. Normal publication and closure
produce generation one. The parent completion record is written last.
This derived profile is ineligible for ordinary checkpoint capture; it does not
extend checkpoint-store semantics.

### Guard issuer policy

On supported POSIX hosts, guard preflight reads a fixed administrator-owned
issuer index before classifying markers. The directory is
`/private/etc/graphify` on macOS and `/etc/graphify` on other POSIX hosts; the
index is `replacement-guard-issuers.json`. Requests, environment variables and
marker contents cannot choose this source or enroll an issuer. This slice ships
no enrollment writer, installer, account changes or authority service.

The index and its ancestors are pinned without following links. Every ancestor
must be root-owned and disallow group/other writes; the existing deny-only ACL
admission also applies. Files must be root-owned, regular, single-link and safe
under that same permissions/ACL policy. Reads are bounded to 64 KiB per file and
64 indexed namespaces, use canonical ASCII-escaped JSON without a trailing
newline, reject duplicate fields and revalidate identity, metadata and bytes.
These checks do not establish native custody, filesystem qualification or rollback
resistance. Compatible clients must have read access to the index, referenced
policy records, applicable markers and journals.

The exact index schema is `graphify.guard-issuer-index.v1` with a `namespaces`
list. Each entry contains `enrollment_id`, `namespace_path`,
`namespace_identity` (`device` and `inode`), `policy_generation` and
`policy_sha256`. Duplicate paths or physical identities refuse. A namespace is
one exact canonical guard-parent path and its pinned identity. Nested namespaces
name independent marker locations; issuer trust never inherits between them.
Changed or missing enrolled parent identities block even when no marker exists.
An enrolled lexical path cannot become a symlink to another canonical path, even
if it points to the old physical directory. An indexed physical identity found
under a different canonical namespace name also blocks. Legitimate aliases to
an unchanged enrolled namespace retain its issuer policy and archive fencing.

The digest selects `replacement-guard-issuers.d/<policy_sha256>.json` under the
same protected directory. Its exact schema is `graphify.guard-issuer-policy.v1`,
with the same enrollment/path/identity/generation fields, plus `mutator_uid` and
sorted unique `legacy_issuer_uids`. A missing record, wrong digest or mismatched
binding blocks the enrolled namespace. Policy construction and digests are inert
data; only the independently admitted source supplies issuer trust.

Without enrollment, UID 0 and the caller's real UID are recognized legacy issuers.
Enrollment adds the selected mutator and retained legacy issuers for that exact
namespace. Real/effective UID mismatch refuses instead of expanding trust.
POSIX creation assigns the effective UID; the coordinator's real-UID lock rule
remains unchanged. See the official [open specification](https://pubs.opengroup.org/onlinepubs/9799919799.2024edition/functions/open.html).
Windows retains the existing guard path without POSIX owner filtering; this
does not qualify cross-identity Windows enrollment.

No-follow owner metadata is classified before marker type or payload admission.
Only a proved-foreign marker may be ignored without opening its payload. On POSIX,
trusted markers must also disallow group/other writes before payload admission;
registration applies the same mode check. Trusted malformed/nonregular/unavailable
markers and unknown policy states still block. This mode check does not qualify
native ACL custody or prevent same-UID/root writers.
Parent, owner and entry bindings are rechecked. Registration refuses a foreign
collision; ordinary preflight's ignore rule grants no overwrite/adoption right.
An absent marker is published exclusively. Updating an existing marker binds its
identity, owner, mode and exact previous bytes before and after quarantine, then
publishes without replacing a concurrent winner. Restoration also uses no-replace
publication. A refused restoration can retain the predecessor as recovery
evidence; it is not authority to remove the winner or resume ordinary writers.
Existing target/archive checks, aliases and exact-operation bypass requirements
remain in force.

Bootstrap absence is admitted only at a proved-missing entry beneath pinned safe
ancestors. Every other unavailable/unsafe/changed index state refuses. A trusted
administrator must install the permanent index before any cross-UID enrollment
or marker publication and never remove enrollment evidence while its namespaces
or guards survive. Missing referenced records then remain distinguishable from
never-enrolled namespaces. Deletion or rollback of the entire protected index
cannot be distinguished from bootstrap absence by this reader; the deployment
contract must preserve that custody invariant or provide another independent
anchor. No live deployment is qualified by this slice. Legacy issuer migration,
policy changes and removal require controlled reconciliation; mutator retirement
alone never removes issuer recognition for retained guards.

Resume needs an existing journal under the coordinator lock, plus the exact
request, preview, seal and state-specific physical objects. It never initializes
a missing operation. Execute can create an operation or exactly retry one. It never
searches for matching hashes or repairs substituted directories. Native
publication interruption resumes under its exact recorded owner. Unknown or
unrecorded ownership is blocked rather than reconstructed from a token found
on disk. Completed retries validate the native receipt and retained archive.
After exact selector checks, forward dispatch admits the supported state and
required field shapes before guard registration. Rollback intent cannot enter
forward replay. This admission does not authenticate journal history or replace
the existing physical-object and native-owner checks. The coordinator lock is
already held, so a refusal can retain its lock file.

Rollback is explicit and retains the replacement graph at a fresh external
quarantine path before restoring the original archive name. The terminal result
is `rolled-back-unrecovered`: the old identity failure still exists. It is not
permission to resume forward publication or a claim of restored service.
The original request-bound target/archive registrations remain after rollback,
including an empty archive slot. Path absence supplies no release authority;
this candidate provides no automatic slot-deregistration operation. This differs
from an unused quarantine that was never registered.
If installation has not occurred, the allocation stays at its recorded external
container instead, with its recorded physical identity checked before restoration.
Only quarantine selected to retain an installed tree receives an archive guard;
a pre-install rollback leaves its unused quarantine name unregistered. Interrupted
rollback uses the persisted tree selector to keep an actual quarantine protected.
The journal and response name that retained location, including
when rollback resumes after restoring the original tree. Rollback admits the
complete intent and terminal journal, including the current new-tree inventory,
before registering quarantine or moving either tree. Its metadata can exceed
the bound even when forward preview passed; refusal preserves the prior journal
and namespace.

The production maintenance capability, native legacy-client/restart exclusion,
full namespace durability matrix, interrupted rollback and exhaustive concurrent
writer qualification remain required before enabling live use. There is no
claim that existing checkpoint A14 qualifies these new boundaries. Keep #147
open. This implementation task does not authorize live replacement, runtime
installation, PR delivery or cleanup of retained user graphs.
