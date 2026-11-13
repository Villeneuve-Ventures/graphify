# Documentation validation routing

`make fast-feedback BASE=origin/v8` is the PR validation entry point. It inspects
base-to-HEAD, index, working-tree and non-ignored untracked changes together.
Use the actual PR base. Full history and a resolvable base are required to prove
docs-only eligibility. Empty or failed inspections select full validation.
Make selects its base from `BASE` (default `origin/v8`), including an explicit
empty value. Recursive checks receive the same selection as a Make argument.
`GRAPHIFY_VALIDATION_BASE` remains available as policy context but does not
override Make's base selection.

| Lane | Checks |
| --- | --- |
| `editorial-docs` | Complete-set whitespace and conflict checks, plus `tests/test_readme_policy.py` |
| `contract-docs` | Editorial checks plus tests directly mapped to changed policy/template paths |
| `full` | `make review-ready`: whitespace checks, frozen all-extras setup, complete `pytest tests/`, whitespace checks again |

The full local gate runs `uv sync --all-extras --frozen`, matching the existing
CI pytest environment, before running the complete suite. Tests require declared
optional integrations, including the MCP extra's JSON Schema validator. Dependency
setup failure stops the gate before pytest; the committed lock remains unchanged.

Only selected root documents, issue/PR template docs, and regular text documents
under `docs/` can qualify. Governance names, instructions, code blocks, commands,
and removed command content select contract docs. README naming remains a
repository-wide inventory check; it does not check prose accuracy or links.
Changes to the protected-change policy select full validation. Its verifier's
real-Git oracle requires exactly Git 2.55.0, which the full CI jobs install before
tests; the routing job does not supply that prerequisite. Local full validation
also requires that Git version. README changes also run the existing Python
installation/runtime policy tests. PR template changes run the routing tests and
a content check of the maintained template's validation-route receipt field.
Instruction filenames qualify only at the root or in maintained docs and template
directories; they do not bypass excluded or unknown path ownership.
An explicitly designated protected change still
requires its independent protected review. Classification never activates that
workflow or establishes review passage.

Generated and runtime skill Markdown, generator fragments, corpora, fixtures,
code, configuration, workflows and unknown paths remain full. Mixed changes,
cross-category renames, symlinks, executable modes, binary or non-UTF-8 content,
inspection errors and missing base evidence cannot take a docs lane. Both sides
of renames and all change surfaces are inspected, including deleted content.
Pytest cache history does not select focused tests.
Full-content scans of changed tracked files require a complete conflict-marker
set, so an unrelated edit does not reject pre-existing standalone marker-like
text. Added lines and wholly new files retain strict standalone-marker checks.
Final routing execution rejects nonempty `PYTEST_ADDOPTS` before dispatching
checks. Unset it so inherited collection or cache options cannot suppress required
tests. Dry runs and low-level whitespace helpers do not establish final readiness.

Routed checks and direct `make review-ready` share environment admission. The
default is a new local `.venv`, or an owned real `.venv` whose editable Graphify
origin is this checkout. Symlink aliases and environments bound to another
checkout are refused before setup. To reuse an existing external environment,
run its Python explicitly and pass `VALIDATION_ENVIRONMENT=/absolute/env/path`
to Make (or `--validation-environment` to the routing CLI). It must be owned,
match the running Python prefix, and have this checkout's editable binding.
Admission receipts record its path, owner, device/inode, isolated Python prefix,
editable origin and the selected interpreter's normal package lookup before
writes and again after setup before tests. Correct metadata cannot admit an
environment whose earlier `.pth` entries select a foreign Graphify package.

Ambient `UV_PROJECT_ENVIRONMENT` grants no admission. Child processes pin the
checkout and admitted environment, discard competing uv routing/configuration
and Python import controls, and disable dotenv loading. Ambient pytest plugin
selection and autoload overrides are removed; installed plugins and tracked
pytest configuration remain active. Operational `UV_OFFLINE`,
`UV_PYTHON_DOWNLOADS`, `UV_CACHE_DIR` and `UV_PYTHON_INSTALL_DIR` settings are
retained. The install directory locates preinstalled interpreters, including
setup-uv's CI inventory; it does not override the admitted exact runtime.
Custom cache and install directories do not select the checkout or environment.
Documentation setup uses frozen
inexact sync to retain optional packages, then verifies the environment before
running tests.
The full setup still uses the existing frozen all-extras sync and complete suite.
Make and CI bootstrap the router with isolated Python startup; recursive Make
uses the same admitted selection through explicit arguments. Inherited Make
flags, extra Makefiles and variable overrides cannot replace the selected checks.
Local candidate version and site-packages come from that candidate's isolated
interpreter and must satisfy this checkout's `requires-python`. Qualification
defaults to the exact final CPython X.Y.Z version in the checkout's tracked
`.python-version` (currently 3.14.10). The router reads that pin explicitly and
supplies its own `UV_PYTHON` request while keeping uv configuration discovery
disabled. A missing or malformed pin refuses validation. `PYTHON` selects the
stdlib bootstrap and may use a different minor; it does not select the test
runtime. Explicit external selection deliberately
requires `PYTHON=/absolute/env/bin/python` as well as the environment flag.

For a deliberate alternate-runtime check, pass `VALIDATION_RUNTIME=X.Y.Z` to
Make or `--validation-runtime X.Y.Z` to the routing CLI. Only one exact final
CPython version is accepted; package support remains `requires-python`, not this
qualification pin. Recursive Make preserves an explicit override. Ambient
`UV_PYTHON` and competing Make/environment controls cannot replace the selected
request. Existing local and explicitly selected environments must match the
request before any sync; mismatches are refused without replacing them. Select
a matching environment or deliberately select its supported runtime. The gate
does not delete or rebuild a mismatched environment for the operator.

Version 2 environment receipts retain the existing candidate fields and add the
bootstrap executable, implementation and version, declared pin, requested runtime
and selection source (`.python-version` or `explicit-override`). They report
`requested_pin_satisfied` and `ci_patch_parity`; both are null while a new local
environment is absent. Parity compares the observed final CPython patch version
with this checkout's declared pin. It does not establish hosted CI passage or
operating-system/dependency parity. A deliberate override can satisfy the request
while reporting false CI patch parity. Existing historical receipts stay valid
under their original contracts; these new fields do not reinterpret old runs.
The pin and candidate are checked again after setup and before tests. Offline
and disabled-download settings remain effective: an unavailable requested Python
stops setup with uv's diagnostic and cannot fall back to another patch or start
pytest. Frozen dependencies, complete full-suite selection, docs inexact sync,
checkout binding and ownership checks remain in force.

Git inspection and selected children discard repository namespace/discovery
selectors and require Git's physical top level to equal the inspected checkout.
Native linked-worktree/gitfile discovery remains supported. Git transport,
credential/configuration settings and restrictive `GIT_NO_REPLACE_OBJECTS` remain
available; this is not a general Git configuration audit.

The versioned receipt lists the lane, paths, commands, skipped checks and fallback
reasons. Its fingerprint includes content, modes, patches, HEAD and base identity.
Only `validation_route_status=complete` proves selected commands passed with the
same final snapshot. Changes during execution invalidate the receipt. A dry run
prints `validation_route_status=printed`; low-level `make docs-check` cannot
replace the final routing receipt.
Direct `make review-ready` also compares its initial and final repository snapshot
and exits with status 3 if content, modes, HEAD or base identity changes while
the checks run. Passing whitespace checks alone cannot admit changed inputs.

PR CI runs the same classifier against the event's base SHA, with complete Git
history. Eligible docs run their selected checks in the routing job. Full changes
delegate to the existing four pytest shards, generated-skill checks, locked Ruff
and Pyright checks, optimized protected-verifier conformance, install smoke,
native Leiden smoke and advisory
security scans. No full command is replaced by docs checks for mixed or uncertain
changes. The stable **Python quality gate** rejects missing routing outputs or
failed docs checks and still requires all four successful shard receipts for the
full route. Branch pushes and manual dispatch always select full validation.
The CI-only full route emits `deferred`, not `complete`; downstream jobs own proof.
