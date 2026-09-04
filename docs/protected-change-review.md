# Protected-change review policy

Policy version: `graphify.protected-change-review.policy.v2`.

This policy provides a bounded, invariant-gated workflow for protected changes without allowing review and repair to expand indefinitely.

## Activation and authority

Use this workflow only when one of these sources designates a change as protected:

- the user or issue;
- the nearest repository instructions; or
- an acceptance owner explicitly designated by the user, issue, or nearest
  repository instructions, before implementation, because it crosses a security
  or trust boundary, authority decision, persistent-state or recovery path,
  installer or update path, Git lifecycle boundary, publication or release
  boundary, or supported CLI/output contract.

Reviewers may recommend protection but cannot activate it or widen scope. Policy
text, digests, verdicts, and receipts are evidence, not permission. Local
readiness never grants commit, push, GitHub mutation, publication, merge, release,
deployment, or cleanup authority.

Use one dedicated isolated worktree. Record its absolute path, repository root, branch or detached state, base, and HEAD; preserve dirty/concurrent checkouts.

## 1. Freeze acceptance

Before implementation, the acceptance owner must freeze:

- the full base OID;
- the protection-designation source and exact reference;
- the acceptance-owner designation source and exact reference;
- the designated acceptance owner and that owner's protected-surface
  classification;
- exact behavior and non-goals;
- supported platform, Git, and output matrix;
- user-visible semantic decisions;
- authority owners, trusted inputs, trust boundaries, and required invariants;
- crash-recovery and cancellation models, or a bounded `N/A` rationale;
- an allowlist of expected production, test, documentation, generated, and
  evidence paths;
- production and test churn budgets and their measurement rules.

Encode the acceptance packet with the byte-unique JSON profile and conformance
vector defined in section 5. The packet must include its schema version. Record
that schema's version and exact-byte SHA-256 plus the packet version and
exact-byte SHA-256. A change to
the frozen base, any designation field, another path, a semantic decision, or
the authority model requires acceptance-owner approval and a new freeze.
Unresolved material semantics block work.

## 2. Establish feasibility

Before production code:

- add the smallest failing real-Git regression or bounded feasibility spike;
- capture genuine red evidence;
- experimentally verify every relied-upon stock Git lifecycle fact;
- obtain read-only architecture review of authority, trusted inputs, crash
  recovery, and cancellation; and
- obtain an independent critic review for adjacent-scope expansion.

A documentation-only adoption may mark red regression and runtime models `N/A`
with a bounded rationale. Freeze the plan and invariant map by digest.

## 3. Preserve ownership

- Keep one implementation writer for the attempt.
- Keep stable, read-only correctness/security and architecture/invariant
  reviewers. Each must be distinct from the other reviewer, implementation
  writer, acceptance owner, and leader; self-approval is prohibited.
- Initial reviews are feedback-blind to each other.
- The leader adjudicates findings and sends one consolidated repair packet.
- Re-reviewers may see that packet but must inspect the complete new candidate.
- Give every reviewer the exact policy, acceptance packet, complete candidate
  manifest, validation provenance, and their digests.

## 4. Implement vertical invariants

Implement vertical milestones proving externally visible invariants. During
development, run targeted regressions, changed-file checks, and diff checks. Do
not repeatedly run the full suite, add cleanup-only work, or patch speculation.

Expected paths are an allowlist. Another path requires acceptance-owner scope
approval and a new acceptance digest.

## 5. Freeze candidate content and evidence

The immutable candidate-content manifest path set is the union of every path in
the frozen base tree, HEAD tree, index, and raw candidate-worktree inventory.
The worktree inventory recursively contains every regular file and symlink
directory entry except the exact top-level `.git` directory, including ignored
and untracked entries, whether or not Git status reports a change. This
deliberately binds raw tracked bytes and exact path spellings that a clean
filter, line-ending conversion, file-mode configuration, case- or
normalization-insensitive lookup, ignore rule, or another Git comparison rule
could otherwise hide. Moves are represented deterministically as one deletion
plus one addition.

Manifest schema `graphify.protected-change-review.candidate.v2` has exactly
these top-level keys and value types:

```text
acceptance_packet: {
  schema_version: string, schema_sha256: sha256,
  version: string, sha256: sha256
}
base_oid: full_git_oid
head_oid: full_git_oid
paths: [path_record, ...]
policy: {version: "graphify.protected-change-review.policy.v2", sha256: sha256}
schema: "graphify.protected-change-review.candidate.v2"
status_porcelain_v2_z_base64: base64_string
tracked_binary_diff_sha256: sha256

path_record: {
  base: git_content_or_null,
  head: git_content_or_null,
  index: git_content_or_null,
  path: utf8_path,
  worktree: raw_content_or_null
}

git_content: {
  bytes: nonnegative_integer,
  mode: six_digit_git_mode,
  oid: full_git_oid,
  sha256: sha256,
  type: "blob" | "symlink"
}

raw_content: {
  bytes: nonnegative_integer,
  mode: six_digit_git_mode,
  sha256: sha256,
  type: "blob" | "symlink"
}
```

Use lowercase hexadecimal for Git OIDs and SHA-256 values. A null layer means
the path is absent from that layer. A present Git layer records the Git object
mode and OID plus the object byte length and SHA-256. A present worktree layer
comes from raw filesystem operations, never Git content conversion: use
raw byte-returning directory enumeration, then `lstat` only a spelling present
in that exact inventory, hash regular-file bytes read without a Git filter, and
hash the raw symlink-target bytes returned by `readlink`. Never determine
presence by probing a Git-supplied spelling, because a case- or
normalization-insensitive filesystem can resolve it to a different directory
entry. Normalize a regular worktree mode to `100755` when its owner-execute bit
is set and `100644` otherwise; use `120000` for a symlink. Fail closed on
duplicate raw path bytes, undecodable paths, another object or filesystem type,
or an exact top-level `.git` entry that is not the selected repository
directory. Sort path records by the raw UTF-8 bytes of `path`.

Create standalone candidate and diff clones without modifying shared Git
metadata, then create a filesystem-level read-only snapshot containing both
complete roots. Candidate identity is the content observed from that snapshot,
not its mutable staging source. Define `<candidate-root>` and `<diff-root>` as
the canonical absolute paths of those roots inside the snapshot. Before and
after manifest generation, verify both that the snapshot remains mounted or
otherwise enforced read-only and that `git -C <root> rev-parse
--show-toplevel` resolves to the selected root with Git and common directories
at the frozen snapshot paths. Record the snapshot mechanism, immutable source
identity or image SHA-256, and both read-only observations in validation
provenance. Fail closed on drift or when an enforced read-only snapshot is not
available.

This empty repository-configuration profile supports only Git's `sha1` object
format without a compatibility algorithm. Before the first source observation,
create `<empty-config>` as a canonical regular zero-byte file inside a separately
enforced read-only control snapshot outside every repository worktree and
candidate inventory. Record its canonical path, snapshot mechanism and image or
source identity, device and mount identity, read-only observations, and SHA-256.
Keep that snapshot read-only through every source, clone, candidate-snapshot, and
manifest command; every use must resolve to the same file. Fail closed on drift
or a writable control. Canonicalize `<source-root>` before creating the clones.
In a fresh `env -i` environment, pass only `PATH`,
`LC_ALL=C`, `GIT_NO_REPLACE_OBJECTS=1`, `GIT_OPTIONAL_LOCKS=0`,
`GIT_CONFIG_NOSYSTEM=1`, and explicit empty `GIT_CONFIG_SYSTEM` and
`GIT_CONFIG_GLOBAL` files; do not set any repository selector or command-scope
configuration. Using `git -C <source-root>`, require the canonical top-level to
equal `<source-root>` and record the absolute Git and common directories, HEAD,
base, and the SHA-256 of the raw `git config --local --includes --show-origin
--show-scope --null --list` byte stream. Record the presence, type, size, and
SHA-256 of the source common and worktree configuration files. This source query
intentionally retains only the selected repository's local configuration,
including local includes, while the empty environment clears repository
selectors and injected configuration.

Under that same environment, record
`git rev-parse --show-object-format=storage`,
`git rev-parse --show-object-format=input`,
`git rev-parse --show-object-format=output`, and
`git rev-parse --show-object-format=compat` from the source. Require the first
three results to be exactly `sha1` and the compatibility result to be an empty
line. Before any command that may read the source index, derive
`<git-dir>/index` from the frozen Git-directory identity, open it with no-follow
operations, and require exactly one regular file. Parse the raw bytes with
session-local source whose SHA-256 is recorded. Require a valid trailing SHA-1
checksum and a `DIRC` version 2 or 3 header. Parse exactly the declared entry
count with checked bounds, the 62-byte SHA-1 prefix, NUL-terminated raw paths,
matching name-length bits, strict byte order and uniqueness, and exact
eight-byte padding. Require `(flags & 0xf000) == 0` for every entry and never
accept an extended flag word. Parse all remaining bytes as bounded
signature/size/payload extensions before the checksum; reject `FSMN`, every
lowercase required extension including `link` and `sdir`, malformed framing,
or trailing bytes. Version 4, split, sparse, skipped-checksum, and required
extension indexes are unsupported. Canonicalize each accepted entry as
`u64be(path length) || raw path || u32be(0)`, sorted by raw path, and record the
version, entry and extension counts, extension signatures, computed SHA-1, raw
index SHA-256, parser-source SHA-256, and inventory count/SHA-256 in validation
provenance. Require the raw index bytes after parsing to equal the bytes before.
Only after raw acceptance, capture separate `git ls-files -t -z`, `-v -z`, and
`-f -z` diagnostics with command-line `core.fsmonitor=false` and
`core.sparseCheckout=false`; each record must be exactly `H SP raw-path NUL`
over the raw-parser path set. Clone with
`git clone --no-local --no-checkout <source-root> <clone>` and record that exact
command. Treat clones as object-transfer starts, not candidate state. Before
cloning, derive exact NUL-delimited `mode SP full-OID SP 0 TAB raw-path NUL`
records directly from the same validated in-memory raw-index byte buffer.
Require supported modes, hash and freeze that byte stream, and reject other
stages or unsupported entries. Use only that frozen stream for candidate index
reconstruction; separately capture the complete raw source-worktree map.

### Repository verifier reference

The repository-tested reference implementation is
`graphify.protected_change_verifier`, version
`graphify.protected-change-verifier.v1`. Its embedded logical schema is
`graphify.protected-change-verifier.manifest-schemas.v1`, SHA-256
`59666f5cf42b4a8e37c0275194f955124a65a3502090502ec8d557e8c5e24c8d`.
Resolve the selected index through Git so linked worktrees are handled; never
assume that it is `.git/index`:

```sh
source_root=/absolute/path/to/source-root
empty_config=/absolute/path/to/read-only/empty-config
env_path=/absolute/path/to/pinned/env
git_path=/absolute/path/to/pinned/git
verifier_python=/absolute/path/to/pinned/python
verifier_module=/absolute/path/to/pinned/protected_change_verifier.py
isolated_path=/absolute/pinned/bin-path

# Preconditions: every value above is absolute; empty_config is the frozen
# zero-byte control file; env_path, git_path, and verifier_python are
# pre-provisioned and outside every source, candidate, diff, control, and
# evidence inventory. verifier_module is the externally pinned regular file
# described below, with a pre-invocation digest and enforced lifetime barrier.
index_path="$(
  "$env_path" -i PATH="$isolated_path" LC_ALL=C GIT_NO_REPLACE_OBJECTS=1 \
    GIT_OPTIONAL_LOCKS=0 GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_SYSTEM="$empty_config" GIT_CONFIG_GLOBAL="$empty_config" \
    "$git_path" -C "$source_root" rev-parse --path-format=absolute \
    --git-path index
)" || exit
[ -n "$index_path" ] || exit
exec "$env_path" -i PATH="$isolated_path" LC_ALL=C \
  "$verifier_python" -I -S -B "$verifier_module" \
  --index "$index_path"
```

Before invocation, bind the absolute `verifier_module` path and exact-byte SHA-256
to the approved verifier source. Require a regular file and component-wise
non-symlinked path. Keep that file and every ancestor under an enforced read-only
mutation barrier through process completion, outside every source, candidate,
diff, control, and evidence inventory. Record the barrier mechanism, identity,
and lifetime observations; mode bits or endpoint hashes alone are insufficient.
The pre-provisioned pinned interpreter and its standard library are the trusted
runtime root and must also be identity-bound, available offline, and protected
outside those inventories. Fail closed when these prerequisites cannot be proven.

The launcher executes the explicit file without package discovery. `-I -S -B`
excludes ambient import paths, global `.pth` and `sitecustomize` startup hooks,
and bytecode writes; no package installation or network access occurs. Require
the returned `source.parser_source_sha256` to equal the pre-invocation file
digest. That comparison supplements the lifetime barrier: a post-execution
self-reported hash alone does not establish which code ran. The ordinary public
`python -m graphify.protected_change_verifier --index <path>` CLI remains
supported; the protected launcher above has the stricter trust boundary.
The verifier is read-only.
It component-safely opens one regular index file,
accepts only checksum-valid SHA-1 DIRC v2/v3 with stage zero modes `100644`,
`100755`, or `120000`, rejects Git 2.55 HFS/NTFS `.gitmodules` aliases for
symlink entries to prevent dropped reconstruction records, and returns canonical source, candidate-index feeder,
and evidence records. The feeder is derived from the accepted in-memory buffer;
candidate reconstruction must consume only those returned bytes. Applying the
feeder, creating clones or snapshots, and producing the exact `candidate.v2`
manifest remain external policy orchestration responsibilities.

The v1 maxima are 32 MiB for the complete raw index, 250,000 entries, 1 MiB per
path, 128 extensions, 16 MiB per extension payload and in aggregate extension
frames, 32 MiB for decoded reconstruction records, 48 MiB for the canonical
success result, and 1 MiB per descriptor read. Unsupported flags, stages,
modes, required lowercase extensions, `FSMN`, split/sparse indexes, and DIRC v4
fail closed with stable path-redacted invariant identifiers. CI conformance is
the exact 80-case matrix in `tests/test_protected_change_verifier.py`, run in
normal and optimized Python modes; that matrix supplements rather than replaces
the complete candidate and snapshot checks in this policy.

Populate only the candidate clone before snapshotting. For each indexed blob, read
source bytes without filters, write them with `git hash-object -w --stdin`, and
require the returned full OID to match. Rebuild its index from the captured records
with `git update-index -z --index-info`; recreate the raw worktree with no-follow
byte-path operations, preserving file bytes, owner-execute state, symlink-target
bytes, and required parents. Never check out HEAD or apply filters.
Resolve and verify each clone's standalone top-level, Git, and common directories.
Atomically zero each verified clone's common config and candidate `info/exclude`;
require `config.worktree` and candidate `info/attributes` absent, record pre/post
metadata/hashes, and never mutate source or shared paths. Immediately after
candidate index reconstruction, repeat the authoritative raw-index proof and
tag diagnostics. Require candidate index/raw maps and flag inventory to equal
captured source maps; repeat source HEAD, base, config, format, raw-index,
flag-inventory, tag, index, and raw-map observations and require pre-clone
equality. Require
both clones to contain frozen base/HEAD objects and equal frozen source HEAD.
After isolation, repeat all four format checks in each snapshot and require 40-digit
lowercase hexadecimal full OIDs. Fail closed on drift, incomplete transfer,
uncertain lineage, `sha256`, compatibility format, multiple input formats, translated output, or any result outside the acceptance-bound format.

Run every Git command used to enumerate a tree or index, read an object,
generate status, or generate the tracked diff in a new allowlisted environment,
not the inherited process environment. Start it with `env -i`, pass only the
required `PATH`, `LC_ALL=C`, the explicit configuration and attribute variables
shown below, `GIT_NO_REPLACE_OBJECTS=1`, and `GIT_OPTIONAL_LOCKS=0`, then invoke
Git with `-C` and the selected root. Repository-local configuration is still
active for ordinary Git commands even when `GIT_CONFIG` names another file, so
do not use that variable as an isolation control. Inside the read-only snapshot,
require both
`$GIT_COMMON_DIR/config` and `$GIT_DIR/config.worktree` to be absent or existing
regular zero-byte files and record their presence, type, byte length, and
SHA-256 when present. The candidate and control snapshots jointly form the
mutation barrier for the complete generation interval; endpoint metadata
comparisons alone are insufficient. Pin
every required Git semantic explicitly on the command line. The isolated
environment clears every repository-local variable reported by `git rev-parse
--local-env-vars`,
including alternate index, worktree, object, namespace, graft, shallow, and
repository selectors, as well as injected `GIT_CONFIG_COUNT` entries. Fail
closed if the launcher cannot construct that environment or the selected root
or configuration state cannot be verified.

Enumerate the base and HEAD trees with `git -C <candidate-root> ls-tree -rz
--full-tree <oid> --`, the index with `git -C <candidate-root> ls-files --stage
-z`. Independently enumerate raw worktree entries from the snapshot with a
byte-returning `readdir`-equivalent, recursively descending real directories
without following symlinks and excluding only the exact top-level `.git`
directory. Decode path bytes as strict UTF-8 and fail closed on duplicates,
non-stage-zero index entries, unsupported objects, or undecodable paths. Read
Git-layer content directly from the named objects, without filters, under the
same isolated environment and configuration profile. Ignored content is
candidate content under this inventory; isolate validation artifacts from the
snapshot or refreeze after any appear.

Generate the status diagnostic stream in the dedicated candidate worktree, not a
fresh clone, so staged, unstaged, and untracked candidate state remains
observable. The candidate worktree must have no sparse checkout or
`.git/info/attributes`. Its `$GIT_COMMON_DIR/info/exclude` must be absent or an
existing regular zero-byte file, and `<empty-config>` must name an existing
regular zero-byte file. Record the exclude file's presence, type, byte length,
and SHA-256 when present. Do not truncate or replace shared Git metadata to
satisfy this requirement; if the exclude file is non-regular or non-empty,
re-establish the candidate in a standalone isolated clone and refreeze it.
Capture the raw status bytes from:

```sh
env -i PATH="$PATH" LC_ALL=C GIT_NO_REPLACE_OBJECTS=1 \
GIT_OPTIONAL_LOCKS=0 \
GIT_CONFIG_NOSYSTEM=1 \
GIT_CONFIG_SYSTEM=<empty-config> \
GIT_CONFIG_GLOBAL=<empty-config> \
GIT_ATTR_NOSYSTEM=1 GIT_ATTR_SOURCE=<head-oid> \
git -C <candidate-root> -c core.attributesFile=<empty-config> \
  -c core.excludesFile=<empty-config> -c core.filemode=true \
  -c core.fsmonitor=false -c core.ignoreCase=false \
  -c core.precomposeUnicode=false -c core.untrackedCache=false \
  -c status.renames=false status \
  --porcelain=v2 -z --untracked-files=all --no-renames
```

Canonicalize the status stream before placing it in
`status_porcelain_v2_z_base64`. Require the stream to be empty or to contain one
or more nonempty records, each followed by exactly one NUL byte; reject an
unterminated tail or an empty record between terminators. With `--no-renames`,
require every record to be self-contained and accept only this complete byte
grammar:

- `1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>` has exactly nine fields;
- `u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>` has exactly
  eleven fields; and
- `? <path>` has a nonempty path.

For a `1` record, `<XY>` is exactly one of `.M`, `.T`, `.A`, `.D`, `M.`, `MM`,
`MT`, `MD`, `T.`, `TM`, `TT`, `TD`, `A.`, `AM`, `AT`, `AD`, or `D.`. For a
`u` record, it is exactly one of `DD`, `AU`, `UD`, `UA`, `DU`, `AA`, or `UU`.
For either tracked record, `<sub>` is exactly `N...` or `S<c><m><u>` with each
state byte either its named uppercase value or `.`, every mode is six octal
digits, every object ID is 40 lowercase hexadecimal digits, and `<path>` is
nonempty. Reject headers, ignored records, `2` rename/copy records, missing or
empty fixed fields, and every other malformed record. Parse `<path>` as every
remaining byte after the fixed prefix delimiters without splitting that
remainder on spaces. It may contain any non-NUL byte, including spaces. Sort
complete validated record bytes lexicographically, append one NUL after every
record, and Base64-encode that
canonical byte stream.
Record the raw stream SHA-256 in the evidence envelope. The status stream is a
diagnostic cross-check, not the path inventory or the source of any layer's
content record. Compare the independently generated path records and status
stream and fail closed on an unexplained disagreement; ignored paths appearing
only in the raw inventory are expected.

Generate the tracked binary-diff byte stream in a fresh isolated clone with no
sparse checkout or `.git/info/attributes`, using the same kind of
`<empty-config>` file, from:

```sh
env -i PATH="$PATH" LC_ALL=C GIT_NO_REPLACE_OBJECTS=1 \
GIT_OPTIONAL_LOCKS=0 \
GIT_CONFIG_NOSYSTEM=1 \
GIT_CONFIG_SYSTEM=<empty-config> \
GIT_CONFIG_GLOBAL=<empty-config> \
GIT_ATTR_NOSYSTEM=1 GIT_ATTR_SOURCE=<head-oid> \
GIT_EXTERNAL_DIFF= GIT_DIFF_OPTS= \
git -C <diff-root> -c core.attributesFile=<empty-config> \
  -c core.quotePath=true -c diff.suppressBlankEmpty=false diff \
  --binary --full-index --no-color --no-ext-diff --no-textconv --no-renames \
  --diff-algorithm=myers --no-indent-heuristic --unified=3 \
  --src-prefix=a/ --dst-prefix=b/ --line-prefix= --ita-invisible-in-index \
  --ignore-submodules=none --no-relative -O <empty-config> \
  <base-oid> <head-oid> --
```

Record the Git version and the empty file's SHA-256 in validation provenance,
and record the binary stream's SHA-256 in the manifest.

A tracked binary-diff digest alone is incomplete because it omits untracked
files. Fail closed if the complete path inventory or any supported object cannot
be represented deterministically.

Encode the manifest as UTF-8 JSON with exactly the schema above. Sort object keys
by their raw UTF-8 bytes and use `,` and `:` with no surrounding whitespace.
Emit `true`, `false`, `null`, and nonnegative integers in their shortest JSON
forms. In strings, escape quotation mark, reverse solidus, backspace, tab,
newline, form feed, and carriage return as `\"`, `\\`, `\b`, `\t`, `\n`,
`\f`, and `\r`; escape every other U+0000--U+001F code point as a lowercase
six-byte `\u00xx` sequence; emit every other Unicode scalar value literally as
UTF-8, including solidus and non-ASCII characters. Reject unpaired surrogates.
Do not normalize Unicode, emit a BOM, or add a terminal newline. Encode binary
status bytes with RFC 4648 Base64's standard
alphabet, required `=` padding, and no whitespace or line breaks. Hash the exact
encoded manifest bytes with SHA-256; that value is the candidate-content digest
reviewers approve.

Byte-level conformance vector: hexadecimal bytes `0xfb 0x00` encode as `+wA=`.
The canonical JSON value containing path `a/é` and that status value is the exact
UTF-8 byte sequence
`7b2270617468223a22612fc3a9222c227374617475735f706f7263656c61696e5f76325f7a5f626173653634223a222b77413d227d`,
whose SHA-256 is
`2abd1782cb53952044e4239efb441e7f2c17c487ce1a9376aa433845070bf4cd`.
An encoder presented with the same logical path through a literal `é` or a
parsed `\u00e9` escape must produce those same canonical UTF-8 bytes. Every
encoder must reproduce both the Base64 and JSON/digest vectors before its output
is accepted.

Keep an append-only evidence envelope separate from content identity and outside
the candidate worktree and its Git status inventory. Evidence paths may be
allowlisted for collection, but envelope bytes and their path records are never
candidate-content records. Every validation receipt and reviewer approval binds
to the candidate-content digest. The envelope also records pre/post ignored,
generated, and local-state inventories. Encode it as zero or more canonical JSON
object records under the same JSON value rules. The first record begins at byte
zero; append every later record as exactly one LF byte followed by its canonical
JSON bytes. Records contain no literal LF, an empty record is forbidden, and the
envelope never ends with LF. Hash the complete framed byte stream and record its
final digest; new evidence changes the envelope digest, not the candidate-content
digest.

Re-pin content immediately before and after every review and validation. Any
in-scope byte, mode, type, path, status, base, or HEAD change creates a new
candidate and invalidates prior approvals. Unchanged feasibility evidence may be
reused only when its inputs and assumptions are proven unchanged.

## 6. Triage before repair

All blocking correctness or integrity findings require reproduction or
mechanical proof tied to a frozen invariant.

- Any P0 blocks delivery when reproduced or mechanically proven, whether or not
  it is in scope. An out-of-scope P0 terminates the attempt unless the acceptance
  owner approves a new freeze; it does not authorize an out-of-scope repair.
- P1 blocks when reproduced or mechanically proven and in scope.
- P2 correctness or integrity blocks only when tied to supported behavior,
  security, authority integrity, or failure atomicity.
- P2 maintainability and out-of-scope work other than P0 are deferred in the
  final report.
- Unreproduced concerns become watch items and do not authorize repair.
- Create or mutate an external ticket only with separate GitHub authority.

## 7. Bound the loop

One attempt permits:

1. initial independent review;
2. one consolidated repair batch and full independent re-review; and
3. at most one second repair batch, limited to a reproduced regression introduced
   by the first repair or one narrowly missed frozen invariant.

Only edits after initial candidate-content review or a re-review consume a repair batch;
pre-implementation review and pre-freeze implementation do not. One consolidated
packet consumes one batch regardless of internal edits and requires both reviewers
to approve the new exact candidate; final-validation failure cannot be patched outside the remaining batch limit.

Before post-review editing, freeze and hash one canonical consolidated repair
packet. After freezing the result and before re-review, append one canonical
repair-link event to the evidence envelope containing the attempt ID, predecessor
candidate digest, batch ordinal, repair-packet digest, and resulting-candidate
digest. Require a unique attempt ID, ordinal `1` or `2`, chain continuity from the
last reviewed candidate, and exact agreement with the candidate under review;
missing, repeated, skipped, or mismatched linkage blocks re-review.

A central P1 invalidates the frozen authority/invariant model or requires a
cross-cutting or out-of-packet repair. A new central P1 after the first re-review
terminates the attempt. A successor requires explicit acceptance-owner approval,
a simplified or decomposed acceptance version, and a new digest. Do not perform
an automatic same-run reset or continue review-until-clean.

## 8. Reassess complexity

Freeze path classifications and counters before implementation. Count production
churn as additions plus deletions, not algebraic net lines. Handle renames as
content changes, predeclare generated exclusions, and count logical regression
cases even when parameterized or grouped.

Architecture reassessment is required when any condition holds:

- production churn exceeds twice its frozen budget or 3,000 lines;
- a protected module doubles in size;
- more than eight production files change;
- more than 50 logical regression cases are added; or
- a durable state machine exceeds roughly ten observable phases.

File splitting, test parameterization, or undeclared generated-file exclusions
must not evade a tripwire. A tripwire requires simplification, decomposition,
explicit justification, or rejection; it is never a correctness waiver.

## 9. Stage validation by cost

- During development: targeted regressions, changed-file checks, and diff checks.
- Initial candidate: focused and security gates.
- Repaired candidate: affected tests and finding-specific reproductions.
- Review-clean final candidate: full suite and final security, CLI,
  generated-state, and graph-refresh checks required by repository instructions.
- Broad-impact edits: escalate validation earlier.

Read task-level receipts, not only process exit codes. A blocked, incomplete, or
stale receipt is not a pass. A documentation-only change does not require graph
refresh unless code bytes or repository instructions require it.

## 10. Deliver locally

Delivery means a verified local candidate or handoff unless separate publication
authority exists. Immediately before handoff or any separately authorized commit,
regenerate the complete candidate manifest and compare its digest with the
approved candidate-content digest; stop on drift. Because a commit changes HEAD
and status, re-freeze and re-approve the resulting committed candidate before
claiming delivery. Deliver only when:

- every frozen criterion maps to evidence;
- both independent reviewers approve the final candidate-content digest;
- the final evidence envelope binds all required checks and approvals to it;
- no reproduced P0 or reproduced in-scope P1 finding remains, and no reproduced
  in-scope P2 correctness or integrity finding remains;
- deferrals, watch items, and validation gaps are explicit;
- no generated, temporary, ignored runtime, or local state is staged; and
- complexity review confirms the core remains coherent and reviewable.

Stop without claiming readiness when identity drifts, required review or
validation is unavailable, the repair cap is exhausted, or a central P1
terminates the attempt.

## Per-change execution packet

```text
Goal:
- <externally observable result>
Protected activation:
- Protection-designation source and exact reference: <user, issue, nearest
  repository instructions, or designated acceptance owner>
- Acceptance-owner designation source and exact reference: <user, issue, or
  nearest repository instructions>
- Designated acceptance owner:
- Acceptance-owner protected-surface classification:
- Isolated worktree path, root, branch/detached state, base, and HEAD:
Acceptance freeze:
- Frozen base OID:
- Protected-activation fields above included in the canonical acceptance packet:
- Behavior and non-goals:
- Supported platform/Git/output matrix:
- User-visible semantic decisions:
- Authority, trusted-input, crash, cancellation, and invariant models:
- Allowed paths by classification:
- Churn budgets and measurement rules:
- Acceptance owner, version, and SHA-256:
Authority:
- Authorized local actions:
- Reserved external or destructive actions:
- Writer, leader, and stable reviewers:
Pre-implementation evidence:
- Red regression or feasibility spike, or bounded N/A rationale:
- Experimentally verified Git facts:
- Architecture and adjacent-scope review results:
Candidate manifest:
- Candidate-manifest schema version, base/HEAD, status bytes, complete path
  records and hashes:
- Policy version and exact-byte SHA-256:
- Acceptance-packet schema version and exact-byte SHA-256:
- Acceptance-packet version and exact-byte SHA-256:
- Tracked binary-diff SHA-256:
- Candidate-content SHA-256:
Evidence envelope:
- Validation and reviewer records bound to the candidate-content SHA-256:
- Repair-link events, or initial-candidate `N/A`:
- Pre/post ignored, generated, and local-state inventories:
- Final evidence-envelope SHA-256:
Done when:
- Frozen criteria map to evidence.
- Both reviewers approve the final exact candidate.
- No reproduced P0 or reproduced in-scope P1 issue remains, and no reproduced
  in-scope P2 correctness or integrity issue remains.
- Deferrals, watch items, validation gaps, and tripwire status are explicit.
- No reserved action occurred without separate authority.
```
