# Dependency validation and tool ownership

The repository has no Makefile. Its dependency-validation entry points are
`uv sync --all-extras --frozen`, `uv run --frozen ruff check .`,
`uv run --frozen pyright`, and the pytest commands in the two CI workflows.
The proposed quality-job additions run Ruff and Pyright after installing the
PR's locked dependencies. Neither check permits failure. The existing pytest
matrix, execution receipts, admission jobs, and final gate retain their behavior.

This candidate needs a passing typecheck baseline before delivery. On the
initial base, the configured Pyright command failed. Do not suppress errors,
reduce the configured scope, or make the step advisory to accept this candidate.
The complete local report is retained outside Git. Full-suite validation and
integration with concurrent workflow changes must also finish before delivery.

## Tool pins

| Tool or surface | Source and updater | Disposition |
| --- | --- | --- |
| Python packages, including Ruff, Pyright and pytest-xdist | `pyproject.toml` and `uv.lock`; existing Dependabot `uv` configuration | Keep the existing constraints and review updates against the lock. Ruff's pre-commit hook now invokes `uv run --frozen ruff check`, removing the separate hook revision. Local lint and CI use the same package lock. |
| GitHub Actions | Workflow `uses` entries; existing Dependabot `github-actions` configuration | Keep reviewable refs and SHA/version comments together. Concurrent Action-update PRs own their version changes and tests. This candidate does not duplicate those changes. |
| CI Git 2.55.0 | Versioned kernel.org source URL, extraction path, exact-version assertion and SHA-256 in both CI workflows | Hold. This is the acceptance baseline documented in `pytest-shards.md`, not an ordinary stale package. No configured updater maintains arbitrary shell URL/checksum pairs. An upgrade needs new acceptance evidence and must change all copies and checksum pairs together. Do not replace it with the runner's Git or a floating download. |
| Corrected PR-Agent image | `pr-agent.yml` digest and corrected v0.48.0 source annotation | Hold. The repository-specific corrected build owns this image. The Actions updater does not rebuild or qualify that correction. A successor needs the corrected source/build provenance and command-routing evidence before digest replacement. |
| Python runtime | `.python-version` pins local Python 3.14.10; workflow inputs select 3.14; `requires-python` defines the supported window | Preserve these distinct purposes. Dependabot's configured package ecosystems do not maintain the runtime file or runner Python input. An operator upgrade must remain inside the supported final CPython window and carry validation evidence. |
| uv executable | `astral-sh/setup-uv` provisions CI; the action ref is maintained by Dependabot | The executable has no explicit version pin here. Do not invent a package dependency or introduce an updater service to manage an absent pin. |

## Merge-protection rollout

The 2026-10-30 administrative API inspection found no classic protection for
`v8`, no applicable active rules, and no organization rulesets. Repository
ruleset `26232527` already describes `Dependabot CI gate` from GitHub Actions
(integration ID `15368`), but is disabled and has no bypass actors. Thus the
validation gate is not currently enforced at merge time. The disabled Copilot
review ruleset adds no enforcement either.

The additive proposal is to activate only ruleset `26232527`, retaining its
default-branch condition, expected integration, non-strict policy and empty
bypass list. A proposed full update payload is retained outside Git for the
separate settings review. This candidate does not activate settings.

Before activation, establish that the final gate is present on the live default
branch and covers Dependabot admission, current head, draft transitions,
label revocation, API failures, partial reruns, and cancelled/skipped dependencies.
Preserve the other chat's approval repairs and tests. Integrate this candidate's
quality checks only after their baseline and required local validation pass.
Confirm the final context is produced on ordinary PRs too, since the rule targets
the default branch rather than a specific author. Review the combined result with
the pending docs-routing changes before the settings step.

GitHub accepts `success`, `skipped`, and `neutral` check conclusions for required
status checks. Requiring each draft-skipped matrix child is therefore insufficient;
the final gate must execute and reject incomplete validation for Dependabot.
Cancelled checks do not satisfy the requirement. A draft PR cannot merge, but
its ready-for-review transition must still produce fresh applicable validation.
See [GitHub's required-check semantics](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks).
