# Hook installation and recovery

## Installer authority lifecycle

New authority slots use `graphify.hook-installation.v2`. Existing v1 slots are
not converted: supported pending batches retain their exact-request retry path,
and completed older history remains usable under its original checks.

V2 constructs a complete unpublished authority slot beneath admitted private
storage before publishing the canonical slot without replacement. Interrupted
initialization candidates remain intact and are not adopted. An occupied,
incomplete or substituted canonical slot still requires manual reconciliation.
New candidate names include a hooks-slot hint solely to scope diagnostics.
Names do not establish ownership; older candidates without that hint stay
preserved and are not attributed to another repository's absent slot.
Matching unpublished candidates remain visible after a fresh canonical slot is
published, alongside active or retained diagnostics. With completed authority,
status shows the candidate warning and visible hook/driver observations. Neither
the hint nor the warning authenticates or adopts those candidates.
The canonical journal binds its own filesystem identity as well as the
authority parent, slot, capability and hooks identities; copying its bytes into
a replacement file does not restore authority.

V2 preparation requires private authority on the same filesystem as hooks.
It constructs staging inside that trusted storage, records PREPARING ownership,
then writes exclusion, preimages and successors. Interrupted PREPARING attempts
are preserved. Exact retry checks all original hook signatures before retaining
the incomplete attempt and constructing a fresh one; uncertain children are
never reused or removed. A different request or changed original hooks refuse.
An unrecorded private preparation directory remains unclaimed and preserved.

Only complete prepared evidence permits EXPOSING. A no-replace rename exposes
the recorded directory in hooks, both directories are flushed, then PREPARED
permits existing per-file publication. Interrupted exposure checks both legal
locations and requires the exact recorded object at one location. Conflicts,
substitution and missing objects refuse without adoption, copying or cleanup.
Prepared evidence is checked before exposure. Exclusion and allowed members
are checked before each live publication, including mixed-state exact retries.
There is no cross-filesystem fallback. V1 retains its existing support boundary.

New v2 stage directories inherit their private authority parent's native mode
and group; they do not inherit a hook parent's setgid mode. Existing hooks retain
their captured UID/GID. New files receive the selected hooks directory's native
creation owner/group, applied and checked before exposure; inability to preserve
that ownership refuses preparation. Directory modes and ownership are not
normalized. Complete v2 history includes a closed final stage inventory,
including its exclusion file, preimages and displaced originals.
The inventory is persisted in the APPLYING journal before the completion guard
is written, so interruption after guard creation leaves a matching authenticated
predecessor for exact retry. This does not reconcile older conflicting guards or
add authority to missing, torn or substituted completion evidence.
On Linux, creating new hooks beneath a hooks directory with a default POSIX ACL
refuses rather than losing inherited access restrictions. On macOS, creation
refuses when the hooks directory has an extended ACL; this conservative boundary
does not distinguish inheritable entries from other entries. The same checks run
before replacing an interrupted PREPARING attempt, exposure and each publication
on an exact pending retry. This does not provide ACL-inheritance reconstruction
or change existing v1 behavior.
On Linux, creating new hooks also refuses when the target directory exposes a
`security.selinux` label. Private preparation does not reproduce SELinux
creation transitions or relabel published hooks; pending retries use the same
refusal. This conservative limit is not enforcing-policy qualification.

Public status now resolves existing paths only. Missing hooks directories and
authority paths stay absent. It reports authenticated pending phases, unavailable
or unverified authority, and retained incomplete evidence without repairing,
creating, compacting or deleting state. Retained preparation is not pending
publication, and its diagnostic does not establish ownership of its children.
Git hooks-path lookup failures return an unverified diagnostic. Active v2
stages must match their recorded identity at the phase-appropriate location;
EXPOSING checks both legal locations and reports the one actually observed.
Missing, substituted or conflicting locations are damaged/unverified evidence,
not a recoverable pending location. These observations grant no repair authority.
With no active batch, unclaimed private preparation artifacts take precedence
over the retained-only diagnostic. Known retained evidence does not authenticate
additional artifacts, and status preserves both.

The separate [#198 acceptance extension](plans/graphify-hook-authority-198.md)
records this local contract and its native qualification requirements.

## Explicit archival of completed v2 stages

`graphify hook status` lists authenticated completed batch IDs and reports
the original stages still present, total completed history records, archived
references, archive payload bytes, retained incomplete evidence and unclaimed
private artifacts. An archived record remains in history if its original stage
is absent; retirement results then report `physical_stage: absent`.
Those counts do not attest unverified content or authorize physical cleanup.
Each admission by this #199 reader validates present completed v2 stages against
their recorded closed inventories, including read-only status and ordinary
install/uninstall. Changed or missing preimages, damaged exclusion, unknown
members or changed preservation metadata report unverified evidence and refuse
mutation. Missing v2 manifests are preserved for manual reconciliation; they
are not reconstructed. V1 keeps its original checks and remains ineligible for
retirement. These observations do not freeze files against later editor changes.

Unpublished candidates and unclaimed private artifacts remain visible beside
retained-history summaries. Authenticated pending requests keep their binding;
summary warnings confer no publication, adoption or cleanup authority.

Preview a selected completed v2 batch without writes:

```sh
graphify hook retire --batch .graphify-hook-batch-EXACT_ID --preview
```

Use the exact ID shown by status, not the placeholder above. Preview returns a
token bound to the current maintenance request, journal and completed evidence.
Execute only that preview with `graphify hook retire --batch
.graphify-hook-batch-EXACT_ID --execute TOKEN`. A stale token refuses without
mutation. Execution holds the physical
hooks and Git common-directory locks. Active hook work, a pending completion
obligation, unsupported old evidence, missing or substituted stages, changed
exclusion or unknown members block archival.

Archival copies the full stage payload and reachable private completion envelopes
to authenticated private storage. Original bytes, modes, UID/GID, ACL/xattrs,
flags and identities are retained as evidence; copies have their own identities
and private current-user permissions. They are not replacement recovery objects,
and this operation grants no restoration or hook-publication authority. The
archive has its own persisted self-ignore file for ordinary Git exclusion.

Only after the complete archive is verified and flushed does an authenticated
retirement record end the original stage's presence dependency. Every original
stage stays untouched. Removing an original beforehand still blocks recovery;
after verified retirement, its absence is allowed. A substituted or changed
original or archive refuses further mutation and preserves all evidence.
Original private completion records remain required; archival does not compact
or remove them. It preserves their complete reference closure.

Interrupted archival is visible as pending and blocks install/uninstall.
Repeat its exact original batch and token after resolving the reported cause.
A pending preview requires that same batch and request context; a different
selection refuses rather than returning another batch's token.
Status and preview check each bound partial archive directory's identity and
private metadata. Missing, substituted or unsafe directories report unverified
evidence and refuse preview or execution while preserving all artifacts.
An authenticated partial archive is retained before a fresh copy is constructed;
unrecorded artifacts are preserved without adoption. Changed sources or request
context refuse. Status and preview never finish this operation automatically.

V2 issuer UID binding prevents account transfer by changing file ownership.
Another authority root or account cannot import copied journals or archives.
Existing v1 history remains usable but is ineligible for this maintenance path;
no upgrade, migration or transfer is provided. Native multi-account access is
not inferred from injected UID observations.

Journal and archive-manifest formats have a 16 MiB record limit. This is not a
total-storage limit. There is no expiration, eviction, preimage purge, stage
movement or physical cleanup. Archival can increase disk use. Ordinary Git
exclusion does not protect originals or archives from force-add, already-tracked
files, removal of ignore files, or `git clean -x`.
The [#199 acceptance extension](plans/graphify-hook-authority-199.md) records the
separate maintenance boundary. It does not activate S6 GC or merge finalization.

V2 journal and completion envelopes have a 16 MiB record limit. This is not a total-storage limit.
Journal capacity is checked before creating a temporary file, reserving the
maximum native inode width for its identity binding. A capacity-blocked save
creates no temporary journal; existing interrupted files remain preserved.

The v2 journal serializer also enforces the record limit on completion
obligations before creating them. Capacity refusal preserves pending authority
and all hook evidence; it does not write a completion record its reader cannot
accept. Resolving an actual capacity limit may require a separately reviewed
operator contract; an exact retry does not evict earlier evidence to make room.

## Optional ordinary merge guards

`graphify hook install --merge-guard` opts into two additional prehooks on
macOS/Linux. They read the effective Git index and refuse a configured, tracked
`graph.json` containing `merge_pending` at an ordinary automatic or manual merge
commit boundary. An unreadable, malformed, oversized, unresolved, or non-regular
selected graph also refuses. Graph JSON must use UTF-8 without a byte-order mark,
matching ordinary graph readers. Valid active and legacy graphs are not required to
have a new receipt format. Passing the guard is not receipt, source-freshness,
or fresh-clone qualification.
The guard shares the readers' 512 MiB default size cap and the
`GRAPHIFY_MAX_GRAPH_BYTES` override (plain bytes or `MB`/`GB` suffixes).
A graph exactly at the effective limit is allowed; a larger graph is refused
before its blob is loaded.

Inspection uses literal index paths and object IDs, ignoring replacement refs
and inherited pathspec switches. It refuses missing local objects without
fetching them. The qualified runtime is Git 2.55.0; a Git version that lacks the
required inspection switches refuses the selected merge instead of weakening
the check.

This is containment for issue #105, not a finalizer. Refusal does not rebuild,
stage, write receipts, or change Graphify transaction state. The merge stays
uncommitted. Repeating `git commit` alone does not repair a pending graph; retain
the merge for an explicit repair or use normal Git merge cancellation. Git's own
merge changes can already be present when the hook refuses.

The automatic guard runs at hook entry without requiring `MERGE_HEAD`. The
qualified Git 2.55.0 implementation selects its automatic commit tree before the
pre-merge hook; staging different output in that hook does not repair that tree.
The hook captures the selected graph's mode and object ID before interpreter
discovery and validates that captured object. Later index replacement or removal
cannot hide a pending captured graph. Git does not expose its already-selected
tree to this hook: index changes before the first capture remain outside this
guarantee. Do not run concurrent index writers during a merge. Reinstall opted-in
hooks after upgrading to receive the current generated guard script.
The manual guard requires ordinary merge state. Rebase, cherry-pick, revert,
sequencers, ordinary non-merge commits, fast-forwards, and commits after
`git merge --quit` are outside this guard's coverage. An untracked graph is not
staged or inspected. Intent-to-add entries are also omitted, as they are from
Git's candidate commit tree; a genuinely staged empty graph still refuses.
The output path is bound at installation. This first opt-in
requires a canonical repository-relative literal selector without whitespace or
Git attribute pattern characters. Spellings such as `./graphify-out`,
`out//nested`, and `out/./nested` are refused because the emitted attribute
pattern would not match the canonical graph path. Absolute selectors (including in-repository
absolute paths) are refused because the existing driver would select a different
path. Shared absolute output behavior in the original post-hooks is unchanged.

`--no-verify`, disabled hooks, and `GRAPHIFY_SKIP_HOOK=1` bypass protection. They
can record a pending graph that readers still reject. If a selected tracked
merge graph needs checking but no trusted interpreter can be found, the hook
fails rather than silently permitting the commit. Missing hooks in a fresh
clone supply no protection.

Installation admits only absent prehooks or standalone Graphify-owned shell
guards. Existing user prehooks, other interpreters, and added content outside
Graphify's guard interval are refused before live hook publication. Graphify
does not move those programs or change their interpreter or `$0`. Existing
post-hook composition retains its current behavior.

Use `graphify hook status --merge-guard` to inspect the optional guards and
`graphify hook uninstall --merge-guard` to remove all five Graphify hooks.
Status applies the same standalone-hook shape check as installation; foreign
content outside the markers or a different interpreter is reported as
unsupported. It does not attest the owned script body or runtime availability.
Guards without execute permission are reported as not installed.
Default `graphify hook install` and `graphify hook uninstall` still manage only
the original three post-hooks; default hook uninstall leaves opted-in guards in
place, which status displays. On macOS/Linux, the broader `graphify uninstall`
selects all five hooks when a regular prehook contains either Graphify guard
marker, including a malformed owned section. Otherwise it removes only the
original three hooks, preserving unrelated prehooks such as user symlinks and
hard links. An authenticated pending five-hook uninstall retains that selection
on retry even after the live guards have been removed. Cleanup failures are
reported before advising package removal.
The five-hook batch has the same individually atomic file publication and
retained recovery limits described below, not a whole-batch atomic guarantee.

Finish an interrupted original three-hook operation with its original default
command before opting in. A different five-hook request cannot resume it.
Interrupted opt-in operations require the same `--merge-guard` request and
context. Completed original history remains usable; no recovery history is
converted or deleted.

The [merge lifecycle contract](plans/graphify-merge-lifecycle-contract.md) keeps
portable reading, successful commit finalization, sibling admission, and
sequencer deliveries separate.

On macOS and Linux, `graphify hook install` and `graphify hook uninstall`
prepare the complete three-hook batch before changing live hooks. Each published
file has its complete content and final executable mode. Existing user-hook
composition and Graphify marker rules still apply. Windows uses the existing
installer and does not have this new atomic publication or recovery guarantee.

Changing hooks requires permission to create and rename entries in the selected
hooks directory. Writable hook files alone are not sufficient. If staging
creation is denied, the error identifies the directory and explains this
requirement. Ask the directory administrator for suitable directory access, then
retry the exact original command. This failure leaves live hooks and registration
unchanged; private authority initialization can occur before it. Graphify does not
change directory permissions or fall back to editing live hooks in place. A no-op
hook batch does not need staging; other authority and registration checks still
apply.

Existing hard-linked hooks are refused before staging or publication, including
no-op install and uninstall. Atomic replacement would separate their shared
inode and leave another alias unchanged. Graphify retains every alias; it does
not edit the shared inode or remove links. Private authority initialization can
precede this refusal. Active hook and recovery-file reads also reject a newly
observed hard link before further publication. A link created after a check can
still race publication; later detection retains recovery evidence and reports
failure, without rolling back an already published hook.

Existing hooks' UID/GID are preserved on staged replacements and preimages for
install and uninstall, including content and executable-mode replacements.
Ownership is applied and verified before mode and extended metadata, since a
native ownership change can clear special mode bits or capabilities. If the
installer cannot preserve ownership, preparation refuses before any live hook
in the selected three- or five-hook batch changes, and registration does not
run. This can require privileges even when directory permissions permit
replacement. Graphify does not fall back to the installer's owner/group. New
hooks use the filesystem's normal creation ownership.

Replacement hooks preserve extended attributes exposed by the native file APIs
and access-control lists (ACLs), including macOS deny entries and Linux named-user
entries. The existing executable-mode update still applies; on Linux this can
change the ACL mask just as the previous in-place chmod did. Metadata is copied
and verified on staging files before publication. During preparation, an
inspection or preservation failure stops the batch before any live hook changes.
Changed metadata also blocks recovery, just as changed hook bytes or modes do.
Hook snapshots bind UID/GID as well. Ownership drift on a live hook (including
an unchanged hook), staged successor, displaced original, or preimage blocks
pending recovery. Rechecks observe non-cooperating edits; they do not exclude
an edit between observation and publication.

On macOS, replacements and preimages also preserve the BSD flags `UF_HIDDEN`
and `UF_NODUMP`. A changed hook with any other flag is refused during preparation,
before any live hook or registration change. Graphify does not clear unsupported
flags or promise to preserve compressed, immutable, append-only or synthetic
file state. Unchanged no-op hooks keep their existing flags. Changed flags on
live hooks, staged successors or preimages block pending recovery.

The three hooks are not one atomic transaction. An interruption can leave a mix
of old and new, individually complete hooks. Git configuration and
`.gitattributes` registration are separate steps. On macOS and Linux, a
registration failure returns an error even if hook publication completed.

## Retry an interrupted operation

Run `graphify hook status` from the original repository. A pending result means
installation or removal needs attention; status does not repair it. Retain the
recovery files named in the error. Resolve the reported I/O or lock problem,
then repeat the original command from the same repository and Python
environment, with the same output configuration. A different operation,
interpreter, output, repository sharing the hooks directory, or registration
configuration cannot resume that pending batch.
The request includes the physical identities of the repository root and Git
directory. Recreating either directory at the same pathname does not preserve
the original request.

On macOS and Linux, Graphify observes the request context again immediately
after acquiring the hooks lock, before creating or changing private authority.
It also rechecks before preparation and staging, and at publication, no-op and
completion checks. An observed change or an unreadable context stops the
operation without adopting a new request or overwriting the observed edit.
Later refusal retains earlier, individually complete hook publications and
pending recovery evidence; it does not roll them back.

These observations are not an atomic transaction with non-cooperating editors.
An edit between checks can still race publication. Merge-driver registration
runs after hook completion while the public operation still holds nonblocking
locks on the physical hooks directory and the common Git directory. The locks
cover both default three-hook and opt-in five-hook operations, including hook
no-ops, through driver registration or removal. A competing operation that
shares either directory refuses without changing hooks, registration, or
recovery evidence; retry it after the active operation finishes. Directory
identity, rather than a config file inode or pathname spelling, coordinates
shared hooks paths and linked worktrees. Registration writes and removal use
local Git configuration in the common directory, even when linked worktrees
select different hooks through worktree configuration.

Successful cooperating operations therefore run in lock-acquisition order
through hook publication, local driver configuration, and the invoking
worktree's `.gitattributes`. This serializes operations; it does not reconcile
registrations or attributes in other repositories or worktrees that share
hooks. Their last requested state can differ, just as with sequential commands.
External Git config writers, hook editors, and older Graphify installers do
not participate in this whole-operation coordination. The locks do not make
read-only status observations atomic or prevent non-cooperating edits.

Registration is still outside the hook recovery journal. An interruption or
registration failure can leave complete hooks with incomplete configuration
or attributes. This is not a crash-atomic transaction across the three
resources and does not change the per-file durability contract or exact-request
recovery requirements.

An `unverified authority` warning means Graphify could not safely inspect the
private store. It does not prove that an installation was attempted. Any hook
and driver observations shown below that warning describe visible files and
configuration only; they do not prove completion or authorize recovery.

For v1, interruption before a sealed batch exists can leave an empty authority slot,
an incomplete authority record, or an unrecognized stage. No live hook has been
published at that point, but an exact retry cannot establish ownership of that
state. Retain it for manual reconciliation; automatic initialization recovery
is not provided for that existing format.

After hook publication, a missing or torn completion record can also prevent an
exact retry, even though individually complete hooks are already live. Retain
the staging and authority evidence for manual reconciliation; Graphify does not
reconstruct completion authority from those hooks.

A pending batch created by an older installer without UID/GID, metadata, macOS
BSD flag, or physical repository identity binding cannot be resumed by this version. Retain its
staging and authority records for manual reconciliation. Completed older history
remains usable; it is not migrated or deleted. Private authority signatures keep
their existing format so older authenticated completion evidence can still be
checked. Loading that history does not infer the ownership of its hooks or
upgrade its preservation claim; a new batch captures current hook ownership.

If a pending batch's recorded hooks, staged successors, preimages, or authority
records changed, Graphify refuses automatic continuation. Keep those files for
manual reconciliation. Do not
remove a staging directory or copy a journal to make a retry pass. Matching
filenames, Graphify markers, or content hashes do not establish ownership.

A concurrent editor can replace a hook at the instant of publication. Graphify
retains the actual displaced object and a separate captured preimage, stops,
and reports recovery information. It does not automatically restore either
file over a later edit. The foreign file can be in staging rather than at its
original hook pathname.

## Recovery storage and limits

Hook-local `.graphify-hook-batch-*` directories hold staged files and preimages.
A private per-user store authenticates the batch and binds it to the selected
hooks directory:

- macOS: `~/Library/Application Support/graphify/hook-installations`
- Linux: `${XDG_STATE_HOME:-$HOME/.local/state}/graphify/hook-installations`

The authority path must use trusted, non-symlink ancestors. Private directories
must be owned by the current user, with exactly `0700` on macOS or exactly
`0700` or `02700` on Linux. Linux setgid is allowed without group or other
access; sticky and setuid bits remain forbidden. Unexpected ACLs or unsafe
permissions cause refusal. A lost or changed authority store cannot be rebuilt
from hook-local files alone.
Completed staging is retained; this installer does not perform automatic
historical-stage cleanup. Preimages can contain user-hook content, so protect
these directories as you protect the hooks themselves.

New private directories request mode `0700` through an isolated child process
with umask `0077`; Linux can inherit setgid from the parent and produce `02700`.
Otherwise safe existing `02700` Linux private directories are also allowed:
mode bits do not show whether setgid was inherited or set later. This mode
allowance does not authenticate unknown stages. Graphify does not normalize
directory modes, change directory ownership, or change the caller's umask.
Preexisting and substituted directory permissions are not repaired.
Failed or uncertain child creation retains any artifacts
under the same pre-batch reconciliation rules.

New stages contain a private `.gitignore` that excludes their contents from
ordinary Git staging, including stages under `.githooks` or Husky user-hook
directories. The ignore file is persisted before any staged hook or preimage
is written. It is not ownership evidence. Existing stages are not modified,
already tracked recovery files remain tracked, and explicit force-add can
still add ignored content. Check for recovery artifacts before committing.

Do not run cleanup commands such as `git clean -xdf` over retained stages.
Ignoring files does not protect them from `git clean -x`. Removing or changing
the identity of a historical staging directory makes later operations refuse;
no automatic history cleanup or reconstruction is provided. Later operations
check retained directory identities and private completion records, but do not
revalidate v1 completed-stage contents. This #199 reader additionally validates
each present v2 completed manifest at admission, including archived originals.
A valid archive permits an absent original; a foreign replacement still refuses.
Earlier #198 readers keep their original contract until maintenance advances the
record contract beyond what they support. Preserve preimages and the stage's
`.gitignore`: changing or removing the ignore file can expose recovery contents
to ordinary Git staging. Independently check retained files before using them
for manual reconciliation.

A sealed completion record stays pending until terminal journal persistence is
acknowledged. It is then moved to a retained private name. If persistence of
that final move is uncertain, Graphify reports a metadata warning; the hook set
and terminal journal are already durable. The pending record can reappear after
a restart. Status then reports pending, and an exact retry must validate the
original request. If separate merge-driver registration changed that context,
automatic recovery refuses; retain the evidence for manual reconciliation.

Recovery covers process interruption and restart, conditional on the filesystem
honoring atomic rename operations and acknowledged persistence operations.
Native qualification used macOS APFS and Linux ext4. Injected I/O failures and
process-kill tests are not physical power-loss tests. Filesystems without the
required primitives fail installation rather than falling back to live writes.

The issue #196 ownership repair was reproduced and checked on native macOS
with Python 3.14.3 and Git 2.55.0. Before the repair, content replacement changed
an existing hook from UID/GID `501:12` to `501:20`. Disposable fixtures now cover
differing permitted groups, content and mode replacements, install/uninstall,
three- and five-hook batches, shared hooks, extended metadata, preparation
failure, ownership drift during recovery, and older evidence compatibility.
UID-drift observations and ineffective ownership calls include injected cases.
A separate task-local Ubuntu 24.04 aarch64 VM checked the same source and test
hashes on Linux 6.8 with Python 3.14.6 and Git 2.43.0. With umask `022`, the
installer suite passed 260 tests with 16 platform skips. Native permission
refusal on a root-owned last hook kept the complete three-hook batch and
registration unchanged. A root-run five-hook mode replacement and post-hook
content uninstall preserved UID/GID `501:999`, with read access checked as
the non-root owner before and after. This Linux check covers hook installation;
it does not qualify merge-guard execution on Git 2.43.0.

Foreign-owner replacement on macOS and a broader multi-account access matrix
were not executed. These bounded proofs do not supply those missing checks.
Issue #196 remains open for qualification.
