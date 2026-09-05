# Advisory security scan receipts

The `security-scan` CI job audits the **frozen default-plus-dev environment**
installed by `uv sync --frozen`. Optional extras are outside this batch's scope.
Both scanners remain advisory (`continue-on-error: true`). A green job does not
mean that either scanner completed or found nothing.

## Dependency coverage

`tools/security_receipts.py` exports the frozen default dependencies and explicit
`dev` group, evaluates platform markers in the audited interpreter, and compares
every selected name/version with installed site-packages metadata and `uv.lock`.
The installed inventory includes the scanners and their dependencies. Extra,
missing, duplicate, non-registry, or otherwise unauditable packages invalidate
coverage. Export pins must match the public PyPI registry lock entries.

Only the root project distribution (`graphifyy`) is excluded, after matching its
name/version, editable source directory, and local lock entry. Broad editable
exclusion is not used. The selected requirements are passed to pip-audit with
`--strict --no-deps --disable-pip`; it does not resolve or install a different
environment. The result must cover exactly those requirements with no skips.
This proves advisory coverage of the installed names/versions, not installed
package byte integrity. `--frozen` preserves the lock; it does not certify that
all project requirements are current with that lock.

## Receipts and failure semantics

Each scanner directory contains `receipt.json`, raw `scanner.stdout` and
`scanner.stderr`. Bandit writes JSON to `scanner.json` separately from its
stdout progress display; pip-audit JSON is in `scanner.stdout`. Dependency audits also retain the frozen export, its stderr,
and the exact `requirements.txt`. Receipts bind revision and dirty-state
information, lock/project/helper hashes, coverage and raw-output hashes, Python
and scanner versions, command arguments, duration, and raw process exit code.
Dependency receipts include uv version, installed and selected inventories,
marker environment, and the explicit local exclusion.

`summary.json` holds the revalidated results and the actual Actions step
`outcome` (before tolerance) and `conclusion` (after tolerance). Original scanner
receipts remain available if revalidation fails. The finalizer writes
`summary.md` before artifact upload; the subsequent job summary adds the
returned artifact URL. These uploaded receipts cannot know the final job
conclusion: they label job status as an observation at reporting time. Read
final job status from the linked Actions run after it ends.

- **Complete, clean:** valid full coverage, valid output, no reportable finding
  records, and scanner exit 0.
- **Complete, findings:** valid full coverage and finding records with scanner
  exit 1. This is a completed scan with findings, not a tool failure.
- **Incomplete:** failed preparation, a missing/malformed result, skipped or
  missing packages/files, error diagnostics, launch/network/timeout failure,
  changed evidence, or inconsistent exit/result. Available raw evidence remains
  in the artifact. Every incomplete result has `finding_count: null`, rendered
  as `unknown` in the summary; decoded finding records may still be retained
  as observations, but they do not establish a complete total.

Bandit retains `-r graphify -ll` (medium and high findings), with no new
suppression. Its metrics include lower severities even though `results` is
filtered; reportable counts come from `results`. Errors on stderr, including
swallowed internal plugin exceptions, invalidate completion. Counts describe
scanner records, not proven exploits or necessarily distinct vulnerabilities;
pip-audit can emit multiple records sharing one advisory ID.

Open the security job summary or download its
`security-receipts-<run-id>-<attempt>` artifact. PR descriptions link to the exact
run/artifact used as evidence; artifacts require repository access and expire
after 30 days. If reporting or upload fails, the logs remain evidence of an
incomplete reporting pipeline. No new blocking security baseline is introduced.
Existing dependency-install failures still fail the job.

CI provisions reporting Python independently of uv and invokes it directly,
without site-packages, so uv setup or dependency-install failures can still
produce incomplete receipts and summaries. Reporting still requires a successful
checkout and reporting-interpreter setup. The security job grants its repository
token only `contents: read` access.

## Reproduce in an isolated checkout

Use Python 3.14.2 through final 3.14.x, the committed lock, and Git 2.55.0.
Keep output outside the checkout and use a fresh directory on every run:

```sh
uv sync --frozen
receipts=$(mktemp -d)
uv run --frozen python -m tools.security_receipts scan --scanner bandit --output "$receipts"
uv run --frozen python -m tools.security_receipts scan --scanner pip-audit --output "$receipts"
uv run --no-project --no-sync --python 3.14 python tools/security_receipts.py finalize --output "$receipts"
```

Run the commands separately when your shell stops after nonzero status. Local
runs label Actions outcomes unavailable; they do not fabricate tolerated CI
conclusions. Each scanner refuses to reuse its output directory. A current invocation identifier
is recorded before this check, so rejected reuse invalidates the older receipt
without overwriting its raw evidence. Advisory
queries contact PyPI; service availability and advisory data can change.

## Remaining issue #113 work

This batch does not resolve the whole issue. Remaining acceptance work includes
contextual disposition of each Bandit alert against its threat model, an
explicit baseline/new-finding policy before making scans blocking, and narrowly
justified repairs with focused regressions. Do not replace format-required
checksums, graph identities, parsers, or dependency versions merely to clear
these reports.

## Command references

- [uv sync defaults and frozen behavior](https://docs.astral.sh/uv/concepts/projects/sync/)
- [uv export options](https://docs.astral.sh/uv/reference/cli/#uv-export)
- [pip-audit command semantics](https://github.com/pypa/pip-audit#usage)
- [Bandit CLI](https://bandit.readthedocs.io/en/latest/man/bandit.html)
- [Actions step outcome and conclusion](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#steps-context)
