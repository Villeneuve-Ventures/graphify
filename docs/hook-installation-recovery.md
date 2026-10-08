# Hook installation and recovery

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

Replacement hooks preserve extended attributes exposed by the native file APIs
and access-control lists (ACLs), including macOS deny entries and Linux named-user
entries. The existing executable-mode update still applies; on Linux this can
change the ACL mask just as the previous in-place chmod did. Metadata is copied
and verified on staging files before publication. During preparation, an
inspection or preservation failure stops the batch before any live hook changes.
Changed metadata also blocks recovery, just as changed hook bytes or modes do.

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
remains a separate step after the hooks lock is released; this does not add
registration locking or a whole-operation atomicity guarantee.

An `unverified authority` warning means Graphify could not safely inspect the
private store. It does not prove that an installation was attempted. Any hook
and driver observations shown below that warning describe visible files and
configuration only; they do not prove completion or authorize recovery.

Interruption before a sealed batch exists can leave an empty authority slot,
an incomplete authority record, or an unrecognized stage. No live hook has been
published at that point, but an exact retry cannot establish ownership of that
state. Retain it for manual reconciliation; automatic initialization recovery
is not provided.

After hook publication, a missing or torn completion record can also prevent an
exact retry, even though individually complete hooks are already live. Retain
the staging and authority evidence for manual reconciliation; Graphify does not
reconstruct completion authority from those hooks.

A pending batch created by an older installer without metadata, macOS BSD flag,
or physical repository identity binding cannot be resumed by this version. Retain its
staging and authority records for manual reconciliation. Completed older history
remains usable; it is not migrated or deleted.

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
revalidate completed-stage contents. Preserve preimages and the stage's
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
