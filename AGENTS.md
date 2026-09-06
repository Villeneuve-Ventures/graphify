## Protected changes

Use [the protected-change review policy](docs/protected-change-review.md) only when
the user, an issue, or the nearest repository instructions explicitly classify a
change as protected, or when an acceptance owner explicitly designated by one of
those sources classifies an enumerated protected surface before implementation.
Review findings and validation receipts cannot activate the workflow or widen
authority.

The current run's instructions and permissions stay pinned. Editing this file or
the policy does not authorize its own approval, commit, publication, merge, or
cleanup. Keep the detailed policy, acceptance packet, and complete candidate
manifest available to every independent reviewer.

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
