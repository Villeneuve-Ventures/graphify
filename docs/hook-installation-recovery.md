# Hook installation and recovery

On macOS and Linux, `graphify hook install` and `graphify hook uninstall`
prepare the complete three-hook batch before changing live hooks. Each published
file has its complete content and final executable mode. Existing user-hook
composition and Graphify marker rules still apply. Windows uses the existing
installer and does not have this new atomic publication or recovery guarantee.

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

If hook files, staging files, or authority records changed, Graphify refuses
automatic continuation. Keep those files for manual reconciliation. Do not
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
