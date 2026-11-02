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
and output, including the preallocation/control parent, must share a filesystem.
Rollback quarantine is checked against all source/policy roots and rename devices
before any journal or guard change. Bounds include 100,000 preserved entries,
128 MiB per preserved file, 2 GiB aggregate preserved bytes, 128 MiB sealed data
and serialized control objects, existing SourceIO read bounds and a 30-second
lock deadline. Preview measures the complete serialized initial journal,
including escaped inventory names, and reserves another 3 MiB + 64 KiB for
forward metadata. That allowance covers the duplicated request, container path,
a native owner bounded to 1 MiB, and fixed allocation and completion fields.
Replay checks the remaining allowance before forward effects. Every control
write checks its complete serialized size against the reader limit before
creation or replacement; oversized or unreadable existing state stays held.
A tree can be below the aggregate bound and still refuse because one file exceeds
its own bound.
The profile's exact guard namespace is excluded from
source selection; it is never graph input.

## Candidate lifecycle

The interface is `graphify replacement bind-request|build-data|preview|execute|resume|status|rollback`.
These commands require an explicit request. Data preparation and preview do not
modify the selected output. Preview binds the full opaque old-tree inventory,
current observed old-directory identity, both parents, source and payload seal.
The historical mismatched identity is not approval.

A stable no-follow operation lock is keyed by the journal slot, not the request.
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

Parent guards fence absent creation as well as existing readers and writers.
Guard markers are read relative to pinned ancestor directories and checked for
entry and parent substitution. Both lexical and resolved aliases are checked;
a changed physical target refuses admission. These checks perform no mutation
and do not enable the maintenance backend.
Archive registration is outside the preserved tree; new clients reject explicit
archive mutations and aliases. Older strict readers reject the new binding.
Maintenance exclusion, including legacy restart inhibition, remains essential:
advisory inode locks cannot fence arbitrary same-UID or root writers.

Resume needs the exact request, preview, seal and physical objects. It never
searches for matching hashes or repairs substituted directories. Native
publication interruption resumes under its exact recorded owner. Unknown or
unrecorded ownership is blocked rather than reconstructed from a token found
on disk. Completed retries validate the native receipt and retained archive.

Rollback is explicit and retains the replacement graph at a fresh external
quarantine path before restoring the original archive name. The terminal result
is `rolled-back-unrecovered`: the old identity failure still exists. It is not
permission to resume forward publication or a claim of restored service.
If installation has not occurred, the allocation stays at its recorded external
container instead, with its recorded physical identity checked before restoration.
The journal and response name that retained location, including
when rollback resumes after restoring the original tree. Rollback admits the
complete intent and terminal journal, including the current new-tree inventory,
before registering quarantine or moving either tree. Its metadata can exceed
the bound even when forward preview passed; refusal preserves the prior journal
and namespace.

## Proof and remaining qualification

Focused tests use disposable fixture roots only. They exercise fresh code and
Codex-document data, source/payload drift, provider-route refusal, opaque archive
preservation, aliases, occupied paths, allocation orphans, modeled publication,
exact completion, process interruption and rollback. No existing user graph is
a fixture. Process-kill/restart proof does not establish power-loss durability.

The production maintenance capability, native legacy-client/restart exclusion,
full namespace durability matrix, interrupted rollback and exhaustive concurrent
writer qualification remain required before enabling live use. There is no
claim that existing checkpoint A14 qualifies these new boundaries. Keep #147
open. This implementation task does not authorize live replacement, runtime
installation, PR delivery or cleanup of retained user graphs.
