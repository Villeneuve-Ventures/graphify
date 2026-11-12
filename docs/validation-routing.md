# Validation routing

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
`UV_PYTHON_DOWNLOADS` and `UV_CACHE_DIR` settings are retained; a custom cache
does not select the checkout or environment. Documentation setup uses frozen
inexact sync to retain optional packages, then verifies the environment before
running tests.
The full setup still uses the existing frozen all-extras sync and complete suite.
Make and CI bootstrap the router with isolated Python startup; recursive Make
uses the same admitted selection through explicit arguments. Inherited Make
flags, extra Makefiles and variable overrides cannot replace the selected checks.
Local candidate version and site-packages come from that candidate's isolated
interpreter and must satisfy this checkout's `requires-python`; the stdlib
bootstrap may use a different minor. Explicit external selection deliberately
requires `PYTHON=/absolute/env/bin/python` as well as the environment flag.

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

## Local import-cleanup pilot

An explicitly approved retained case can use `make import-cleanup-check
APPROVAL=/absolute/external/approval.json APPROVAL_SHA256=<reviewed-sha256>`.
This separate local route does not alter automatic docs/full selection, the
explicit full command, or CI. Use a trusted installed Python as `PYTHON` when
needed. It has no dependency setup or test subprocesses. It verifies retained
evidence and reports `complete`, `printed` (CLI dry run), or `blocked` as JSON.
Missing or invalid evidence never starts an automatic full suite.

The external approval uses `graphify-import-cleanup-approval-v2`. It must name
`Villeneuve-Ventures/graphify`, `scope=local-only`, `enabled=true`, reviewed full
and focused producer file digests, and one to eight ordered prefix records.
Each record binds the exact original head/base, candidate head/stack parent,
reviewed cleanup and parent-delta digests, original full result/log, candidate
focused result/log/JUnit, common interpreter digest and installed dependency
fingerprint. Evidence files are absolute paths with SHA-256 bindings. Select
the manifest digest from a separate review of those producers, import side
effects and first-consumer restoration; computing a digest is not that review.
Candidate-supplied JSON alone is insufficient authority. This is an operator
trust input, not independent producer authentication or a general receipt cache.

Each prefix also requires `environment_configs`, with `full` and `focused`
bindings naming the exact respective checkout's `.venv/pyvenv.cfg` path and
SHA-256. These are separately reviewed approval inputs. Computing a hash of the
current file does not prove its historical configuration. Missing adequate
configuration evidence blocks the case; do not invent it, modify retained
receipts or automatically re-pin the approval. V1 approvals and receipts keep
their historical meaning and are not accepted or upgraded by this V2 verifier.
New output uses `graphify-import-cleanup-receipt-v2`.

The pilot admits only reviewed removal of top-level import bindings from its
seven replacement test modules. Other test AST nodes, source, configuration,
fixtures, dependency locks and modes must be unchanged against each original
head. Both complete PR diffs and the cleanup delta are recorded; the last
commit or top-prefix pass cannot hide earlier unqualified work. Every prior
prefix must qualify before its descendant. Reordered, incomplete, wildcard,
new-import, mixed or dirty candidates are refused. Unlisted cases retain the
ordinary router and any explicit stage requirements.

At both observations, the verifier compares the recorded tree with the index and
the actual tracked bytes, symlink targets and executable modes. Hidden/sparse
index states and aliased source parents are refused. Git status is not used as
source proof, and working-tree clean/process filters are not executed. Staged
changes and non-ignored untracked files still block acceptance.

Each original head needs its own complete admitted full-route receipt. Each
candidate needs successful seven-module execution with nonempty, unique JUnit
identities and no failures, errors or skips. Normal and isolated package origins
must name that retained checkout. The pilot requires CPython 3.14.10 and equal
frozen lock and installed dependency payloads. Installed launcher shebangs may
name their respective private environments; launcher bodies must match. The
verifier reads current RECORD payload hashes and compares retained physical,
editable-source and interpreter bindings without executing historical Python.
These local case requirements do not resolve general runtime selection (#275).
The bound environment configuration is rechecked at both observations. Duplicate
keys are refused; effective runtime and system-site settings must unambiguously
select 3.14.10 without system packages. Configuration bindings are per checkout,
so legitimate environment-specific paths need not have identical file hashes.
An executable-adjacent `.venv/bin/pyvenv.cfg`, including an alias, is refused
because Python can prefer it over the bound parent file. Parsing follows Python's
universal-newline file iteration; other Unicode line separators cannot fabricate
a setting that the interpreter would ignore.

A complete receipt means **baseline plus reviewed import delta**, with original
and reused evidence origins. It is not a new-head full-suite result, a validation
of the verifier's own implementation diff, or hosted readiness. Independent
implementation checks and review remain separate. Git/evidence endpoints are
rechecked; this is not continuous isolation against concurrent hostile writers.
No other chat's fixtures, runtime locks, processes, graph output or shared Git
settings are modified. The verifier has a fifteen-minute observation budget and
starts zero workers. Missing focused evidence is a blocked prerequisite, not a
reason to expand execution silently. Turning off `enabled` refuses the pilot;
omitting this explicit entry point keeps the existing automatic routing.

## Hosted routing

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
