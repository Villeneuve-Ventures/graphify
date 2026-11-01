## Protected changes

Use [the protected-change review policy](docs/protected-change-review.md) only when
the user, an issue, or the nearest repository instructions explicitly classify a
change as protected, or when an acceptance owner explicitly designated by one of
those sources classifies an enumerated protected surface before implementation.
Review findings and validation receipts cannot activate the workflow or widen
authority.

A protected designation must be an exact, current statement that identifies the
change or bounded surface as protected and is attributable to an authorized
source listed above. No special wording or self-citation is required. Do not
infer protection from subject matter, affected paths, policy category lists,
test scope, review findings, or similarity to earlier protected work. A stale,
mismatched, or nonexistent citation is not a designation. Re-read the live source
and quote the designation before stopping ordinary work or activating a protected
workflow.

When no exact designation exists, continue under the ordinary repository workflow.
Do not activate protected review, planning, or orchestration merely because the
policy could have covered the change if an authorized source had classified it.
This default never overrides a valid explicit designation, pinned instructions,
or other safety, validation, and delivery requirements. An unavailable source
does not revoke a designation already established for the active run; resolve
material uncertainty about an existing designation before proceeding.

The current run's instructions and permissions stay pinned. Editing this file or
the policy does not authorize its own approval, commit, publication, merge, or
cleanup. Keep the detailed policy, acceptance packet, and complete candidate
manifest available to every independent reviewer.

## Validation routing

Run `make fast-feedback` before claiming PR readiness. It inspects the complete
base-to-HEAD, staged, unstaged, and non-ignored untracked change set and emits a
versioned, snapshot-bound routing receipt. A successful `editorial-docs` or
`contract-docs` receipt is final local validation only when every selected check
passes. `make docs-check` and dry-run receipts are not final validation.

Editorial docs run complete-set whitespace/conflict checks and the existing
README naming guard. Contract docs add tests directly bound to the affected
policy or template. This does not validate prose accuracy or links, activate a
protected workflow, or replace an explicitly required protected review.
Generated/runtime skills, skill fragments, corpora and fixtures are not ordinary
docs. Any mixed, unknown, unsafe, or incompletely inspected change requires the
full `make review-ready` path. Missing base evidence cannot admit a docs lane.
The full command retains complete pytest selection, with whitespace preflights;
CI retains separate generated-skill, optimized-verifier, install and native smoke
checks for full changes. Branch pushes and manual CI dispatch always run full.

Use `BASE=<ref>` for a different PR base (default `origin/v8`). Preserve the
complete receipt; changed inputs, modes, Git identity or environment invalidate
applicable evidence. Do not use pytest lastfailed state to select docs checks.
Protected changes retain their independently applicable acceptance requirements.
Routed and direct full checks share checkout-bound environment admission. An
external environment requires its Python plus explicit `VALIDATION_ENVIRONMENT`
selection; ambient uv overrides cannot select it. See
[validation routing](docs/validation-routing.md) for admission and setup rules.

## Local resource retention

Follow [the local resource retention rule](docs/local-resource-retention.md) when
creating investigation outputs and closing local work. Keep a local retention
index outside Git. Preserve unique work, unresolved reproductions, required
evidence, and active or paused resources; record final receipts and release
conditions. The rule does not grant cleanup authority or schedule deletion.

## README language policy

Graphify's maintained README documentation is English-only. Do not create,
restore, or generate translated README files, including during upstream
syncs, merges, migrations, or documentation refreshes. Keep the historical
`translations/` and `docs/translations/` directories absent and do not introduce
locale-suffixed README files (for example, `README.fr-FR.md`).

Non-English locale codes recognized by the policy guard are reserved as README
parent-directory names, even when their text is English. Use descriptive topic
names instead of ambiguous locale names (for example, `operating-systems`
instead of `os`). English locale directories remain allowed. This naming rule
keeps enforcement deterministic without attempting to infer a document's language.
Maintained documentation under `worked/` follows this policy; corpus artifacts
under `worked/*/raw/` and test fixtures under `tests/fixtures/` are exempt.

The existing CI pytest gate checks complete README filenames and their immediate
parent names in Git's file inventory through `tests/test_readme_policy.py`.
It checks symlink names without following their targets. Filename locale markers
remain prohibited before or after dotted format suffixes, including formats not
listed by the guard. Known format tokens (such as `md`, `org`, and `tex`) take
precedence over locale syntax, so `README.tex` and `README.md.org` are allowed.
Other two- or three-letter tokens can still match the naming rule; the guard does
not claim exhaustive language or format recognition. It does not determine text
language or inspect documentation links; English README text remains a contribution
requirement.
Run the path checks locally with
`uv run --frozen pytest tests/test_readme_policy.py -q --tb=short`.
This rule does not restrict multilingual input corpora or language extraction
support. Any change to this policy requires an explicit operator request.

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)
