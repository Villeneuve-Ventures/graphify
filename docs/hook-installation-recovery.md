# Hook installation and recovery

On macOS and Linux, `graphify hook install` and `graphify hook uninstall`
prepare the complete three-hook batch before changing live hooks. Each published
file has its complete content and final executable mode. Existing user-hook
composition and Graphify marker rules still apply. Windows uses the existing
installer and does not have this new atomic publication or recovery guarantee.

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

The three hooks are not one atomic transaction. An interruption can leave a mix
of old and new, individually complete hooks. Git configuration and
`.gitattributes` registration are separate steps. A registration failure returns
an error even if hook publication completed.

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

An `unverified authority` warning means Graphify could not safely inspect the
private store. It does not prove that an installation was attempted. Any hook
and driver observations shown below that warning describe visible files and
configuration only; they do not prove completion or authorize recovery.

Interruption before a sealed batch exists can leave an empty authority slot,
an incomplete authority record, or an unrecognized stage. No live hook has been
published at that point, but an exact retry cannot establish ownership of that
state. Retain it for manual reconciliation; automatic initialization recovery
is not provided.

A pending batch created by an older installer without metadata or physical
repository identity binding cannot be resumed by this version. Retain its
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

The authority path must use trusted, non-symlink ancestors and a private owner
and permissions. Unexpected ACLs or unsafe permissions cause refusal. A lost
or changed authority store cannot be rebuilt from hook-local files alone.
Completed staging is retained; this installer does not perform automatic
historical-stage cleanup. Preimages can contain user-hook content, so protect
these directories as you protect the hooks themselves.

New private directories receive mode `0700` at creation through an isolated
child process. This does not change the caller's umask or repair permissions on
preexisting directories. Failed or uncertain child creation retains any artifacts
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
