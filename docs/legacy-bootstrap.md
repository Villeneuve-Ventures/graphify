# Trusted legacy bootstrap

`bootstrap-legacy` builds a fresh structural graph from an explicit source request
and publishes generation one into an existing receiptless legacy output. Use it
when that output has a shaped `graph.json` with **no** `_graphify_protocol` key and
no coordination, portable, or managed-workspace authority. A null watermark is
not an absent watermark. Merge-pending graphs, inconsistent current generations,
and copied local identities need their own recovery paths.

The command does not use legacy nodes, edges, labels, manifests, saved roots, or
caches to build the successor. It preserves the old managed bytes as opaque
evidence before replacing them. This is local generation admission; it does not
create a portable bundle or grant authority to a fresh clone.

## Explicit request and preview

Create a JSON request outside the selected corpus, for example:

```json
{
  "schema": 1,
  "root": "/absolute/canonical/source",
  "output": "/absolute/canonical/source/graphify-out",
  "include": ["**/*.py", "**/*.ts"],
  "exclude": ["vendor/**"],
  "extra_roots": [],
  "extraction_profile": "structural-v1",
  "artifact_profile": "core-v1"
}
```

Both directories must already exist. Output cannot equal or contain the source
root. Its contents are always outside the source corpus, including when a custom
output is nested below the source root. Globs are anchored relative to `root`:
`*.py` selects root files, while `**/*.py` also selects descendants. The complete
request is required. An explicit `extra_roots` entry has `label` (`git` or
`policy`) and an absolute canonical directory `path`. For a linked Git worktree,
declare its external Git common directory if detection needs to read it.

```sh
PYTHONDONTWRITEBYTECODE=1 graphify bootstrap-legacy --request /absolute/request.json --preview
```

Keep bytecode disabled for a no-write preview, including a cold Python process.
`python -B -m graphify bootstrap-legacy ...` is equivalent. Preview performs
strict detection, extraction and reobservation in memory. It creates no output,
temporary build, provider request, or persistent cache. It returns JSON with a
`preview_sha256`, selected inputs, per-input outcomes, uncovered inputs, and the
managed artifacts to preserve, replace or remove.

The existing detector's ignore, sensitive-file and noise policies still apply.
It enumerates before applying the request's selection globs. Detection safety
failures, such as symlinks or unreadable inputs, cannot be waived by an exclusion
that would only apply later. Selected unclassified files and unsupported or
incomplete code extraction refuse the build. Selected documents and media are
reported as uncovered; `structural-v1` does not provide semantic extraction.

Limits are fixed: 64 MiB per source read, 512 MiB of source reads, 100,000 evidence
units, 128 MiB of fresh core payloads, and the transaction layer's 1 MiB limit for
each evidence/state document. The normalized preview includes the source bounds.
Legacy preservation and publication also use the existing transaction inventory
and byte bounds. Exceeding a bound refuses the operation.

## Execute and resume

Use the digest returned by that exact preview:

```sh
PYTHONDONTWRITEBYTECODE=1 graphify bootstrap-legacy --request /absolute/request.json --execute PREVIEW_SHA256
```

Execute holds the output lock throughout the operation. Before claiming output,
it repeats the source request and checks the consumed evidence, legacy inventory,
and output identity against the preview. A stale preview refuses without claiming
the output. The first durable claim binds the command version, profiles and
preview digest, even if the process stops before it creates a live owner or token.

The fresh publication contains `graph.json`, `manifest.json`, `.graphify_root`,
`.graphify_build.json` and receipt-bound `.graphify_bootstrap.json` provenance.
The manifest's change-detection hashes and timestamps come from consumed source
bytes and evidence. Semantic hashes are empty. Managed reports, labels, analysis,
wiki pages, HTML, callflow and owned Obsidian exports that are outside `core-v1`
are removed from the active inventory after their original bytes are preserved.
Unmanaged siblings, caches and external exports are not published or removed.

The prepared publication starts empty. Legacy evidence is stored in its separate
`legacy/` directory, outside the publication child. A durable seal binds the exact
publication inventory and deletions before any public artifact changes. The
receipt is written last. This prevents readers from admitting a partial generation;
it is not an atomic filesystem-wide replacement.

After an interruption, repeat the original execute command or select its token:

```sh
PYTHONDONTWRITEBYTECODE=1 graphify bootstrap-legacy --request /absolute/request.json --resume /absolute/output/.graphify_transaction_token.ID
```

| Interruption state | Behavior |
| --- | --- |
| Before the first claim | Output remains unchanged; preview can be repeated. |
| Claimed, before a token exists | Repeat execute with the original request/digest. Existing lease and bounded recovery rules apply. |
| Token exists, before sealing | The exact token resumes preparation after the original source and legacy checks. |
| Sealed, including partial public writes | Only the retained original token can replay the exact sealed bytes. Live source changes or unavailability do not trigger extraction. |
| Receipt and COMPLETE protocol durable | Repeat execute validates the receipt, identity, generation and operation provenance, then returns read-only `already_complete`. |
| Closure or retirement remains after COMPLETE | Explicit resume validates the receipt and original closure journal, finishes that closure, and retires the prepared workspace. Token absence is accepted only where the durable retirement journal permits it. |

After sealing, expiry does not permit owner rotation. Generic transaction recovery,
token runners and finalization cannot replace the bootstrap owner or publication.
A lost or altered token before COMPLETE, changed sealed bytes, replaced output
identity, changed original request, or later generation refuses replay and retains
the remaining evidence. After normal token retirement, completed repeat still
works from the receipt-bound provenance. Later ordinary generations use the normal
transaction recovery rules.

Source observations include supporting reads, probes and directory membership.
They are checked again before sealing. Only the exact identity-validated private
workspace created by this operation is omitted from its own source enumeration.
Source writers are not locked: reobservation establishes observed-current inputs,
not an atomic source snapshot or a promise that sources will remain unchanged.

An interrupt after claiming leaves an incomplete, resumable operation. There is no
receipt-backed predecessor to roll back to. The completed result reports the
retired workspace that retains the legacy evidence and consumed-input manifest;
this command does not garbage-collect it. Apply the repository's
[local retention rule](local-resource-retention.md) before any later cleanup.

The persisted build metadata records the explicit selection. Current ordinary
`graphify update` reads persisted excludes but does not enforce the stored include
list. The bootstrap contract covers this fresh generation; it does not change
ordinary incremental selection or claim semantic parity with a prior full build.
