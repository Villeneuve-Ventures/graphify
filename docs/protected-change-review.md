# Protected-change review policy

Policy version: `graphify.protected-change-review.policy.v1`.

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

Encode the acceptance packet as UTF-8 JSON with sorted keys, compact separators,
standard JSON escaping, no Unicode normalization, and no terminal newline. The
packet must include its schema version. Record that schema's version and
exact-byte SHA-256 plus the packet version and exact-byte SHA-256. A change to
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

The immutable candidate-content manifest path set is the union of every path
that differs between the frozen base and HEAD and every staged, unstaged,
deleted, renamed, mode-changed, type-changed, or untracked status path. A clean
committed candidate therefore still inventories the complete base-to-HEAD
change. Base-to-HEAD comparison and worktree status both disable rename and copy
detection; every move is represented deterministically as one deletion plus one
addition.

Manifest schema `graphify.protected-change-review.candidate.v1` has exactly
these top-level keys and value types:

```text
acceptance_packet: {
  schema_version: string, schema_sha256: sha256,
  version: string, sha256: sha256
}
base_oid: full_git_oid
head_oid: full_git_oid
paths: [path_record, ...]
policy: {version: "graphify.protected-change-review.policy.v1", sha256: sha256}
schema: "graphify.protected-change-review.candidate.v1"
status_porcelain_v2_z_base64: base64_string
tracked_binary_diff_sha256: sha256

path_record: {
  bytes: nonnegative_integer_or_null,
  mode: six_digit_git_mode_or_null,
  path: utf8_path,
  sha256: sha256_or_null,
  status: [status_token, ...],
  type: "blob" | "symlink" | "absent"
}

status_token: "added" | "deleted" | "modified" | "mode-changed" |
              "type-changed" | "untracked"
```

Use lowercase hexadecimal for Git OIDs and SHA-256 values. Present paths must
record type, mode, byte length, and exact-byte SHA-256; hash symlink-target bytes
for a symlink. An absent path uses type `absent` and null mode, byte length, and
SHA-256. Sort status tokens in the order shown above. Sort path records by the
raw UTF-8 bytes of `path`.

Generate the status identity stream in the dedicated candidate worktree, not a
fresh clone, so staged, unstaged, and untracked candidate state remains
observable. The candidate worktree must have no sparse checkout or
`.git/info/attributes`. Its `$GIT_COMMON_DIR/info/exclude` must be absent or an
existing regular zero-byte file, and `<empty-config>` must name an existing
regular zero-byte file. Record the exclude file's presence, type, byte length,
and SHA-256 when present. Do not truncate or replace shared Git metadata to
satisfy this requirement; if the exclude file is non-regular or non-empty,
re-establish the candidate in a standalone isolated clone and refreeze it.
Record base64 of the exact status bytes from:

```sh
GIT_CONFIG_NOSYSTEM=1 \
GIT_CONFIG_SYSTEM=<empty-config> \
GIT_CONFIG_GLOBAL=<empty-config> \
GIT_ATTR_NOSYSTEM=1 GIT_ATTR_SOURCE=<head-oid> \
git -c core.attributesFile=<empty-config> \
  -c core.excludesFile=<empty-config> -c status.renames=false status \
  --porcelain=v2 -z --untracked-files=all --no-renames
```

Generate the tracked binary-diff byte stream in a fresh isolated clone with no
sparse checkout or `.git/info/attributes`, using the same kind of
`<empty-config>` file, from:

```sh
GIT_CONFIG_NOSYSTEM=1 \
GIT_CONFIG_SYSTEM=<empty-config> \
GIT_CONFIG_GLOBAL=<empty-config> \
GIT_ATTR_NOSYSTEM=1 GIT_ATTR_SOURCE=<head-oid> \
GIT_EXTERNAL_DIFF= GIT_DIFF_OPTS= \
git -c core.attributesFile=<empty-config> -c core.quotePath=true diff \
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

Encode the manifest as UTF-8 JSON with exactly the schema above, sorted object
keys, compact separators, standard JSON escaping, no Unicode normalization, and
no terminal newline. Hash the exact encoded bytes with SHA-256; that value is
the candidate-content digest reviewers approve.

Keep an append-only evidence envelope separate from content identity and outside
the candidate worktree and its Git status inventory. Evidence paths may be
allowlisted for collection, but envelope bytes and their path records are never
candidate-content records. Every validation receipt and reviewer approval binds
to the candidate-content digest. The envelope also records pre/post ignored,
generated, and local-state inventories. Canonicalize it with the same JSON rules
and record its final digest; new evidence changes the envelope digest, not the
candidate-content digest.

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

Every edit after review or approval consumes an available repair batch and
requires both stable reviewers to approve the complete new exact candidate. A
failure in final validation is a finding against that candidate; it cannot be
patched outside the same remaining batch limit.

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
