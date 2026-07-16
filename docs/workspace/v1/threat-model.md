# V1 support and threat model

## Supported boundary

V1 targets one non-elevated user on macOS with lifecycle state on local APFS.
State, staging, generations, pointer temporaries, and recovery records must use
supported same-filesystem persistence primitives.

Detectable unsupported conditions fail closed:

- NFS, SMB, FUSE, and other network state roots;
- Windows or Linux lifecycle operation;
- root/elevated execution;
- shared multi-user state; and
- pre-login/system-daemon operation.

Watch supervision is login-scoped. Automatic online GC and historical certified
query are deferred. V1 capacity policy requires preflight reserve and configured
global/per-workspace byte and generation limits; P3/P5 will freeze quantitative
ceilings before operational approval.

## Durability claim

The planned implementation must handle process death, injected short writes,
`EINTR`, `ENOSPC`, `EDQUOT`, `EIO`, failed sync, failed rename, and clean reboot
after a successful durable-write completion. Sudden hardware power-loss
durability is not claimed.

P1 schemas describe the records and ordering but execute no durable state
transition.

## Protected risks

V1 is designed to protect against accidental corruption, crashes, stale and
concurrent processes, untrusted corpus contents, path tricks, secret leakage,
and artifact mismatch/substitution relative to a locally frozen trusted
manifest.

State-root policy requires expected ownership, `0700` directories, `0600`
mutable records, exclusive creation, safe umask/ACL behavior, descriptor-relative
no-follow traversal, regular-file-only payloads, path containment, and rejection
of links and special files.

Operational corpus processing will use a cleaned allowlisted environment,
network denial by default, bounded CPU/memory/file/time resources, read-only
source, staging-only writes, and host-agent instruction/data separation.
Backend endpoints and credentials are operator configuration, never repo policy;
secrets are excluded from argv and persisted state.

## Explicit non-claims

V1 does not resist a compromised source-control or CI system, or a malicious
same-UID actor able to replace both artifacts and the trusted manifest. The
journal is corruption-evident, not cryptographically authenticated against such
an actor. Cross-platform support, network filesystems, pre-login service,
automatic online GC, strict source-linearizable query, and inter-observation ABA
detection are deferred.

Release channels are `dev`, `shadow`, `candidate`, `stable`, and `rollback` and
must promote identical digests. P5 implements candidate publication and
login-service integration; P9 alone owns the real stable switch.
