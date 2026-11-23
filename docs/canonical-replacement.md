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

Use the read-only `bind-request` operation once to freeze strict device/inode
identities for the canonical, archive, control and stage parents. Those
`parent_identities` stay in the original request and are checked before locks or
state reads. Binding an existing request validates it; it never refreshes changed
parents. Copying a control tree to a new parent cannot confer replay authority.

A stable no-follow operation lock is keyed by the journal slot, not the request.
It must belong to the caller's real UID, forbid group/other writes and pass the
existing safe-ACL policy. Admission is repeated after the binding read and after
work; unsafe existing lock permissions or ACLs are refused, never repaired.
Initial journal creation is exclusive under that lock. A different request cannot
replace an existing winner, including a completed operation.
The control parent requires mutating admission before lock creation, so a
registered retained archive cannot be used to create another operation's lock.

### Guard issuer policy

On supported POSIX hosts, guard preflight reads a fixed administrator-owned
issuer index before classifying markers. The directory is
`/private/etc/graphify` on macOS and `/etc/graphify` on other POSIX hosts; the
index is `replacement-guard-issuers.json`. Requests, environment variables and
marker contents cannot choose this source or enroll an issuer. This slice ships
no enrollment writer, installer, account changes or authority service.

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

The production maintenance capability, native legacy-client/restart exclusion,
full namespace durability matrix, interrupted rollback and exhaustive concurrent
writer qualification remain required before enabling live use. There is no
claim that existing checkpoint A14 qualifies these new boundaries. Keep #147
open. This implementation task does not authorize live replacement, runtime
installation, PR delivery or cleanup of retained user graphs.
