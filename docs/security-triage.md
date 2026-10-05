# Issue #113: security triage and proposed baseline

This is a local report and **unaccepted policy proposal**, observed on
2026-10-05. It covers [issue #113](https://github.com/Villeneuve-Ventures/graphify/issues/113)
at clean v8 revision `a558766ba5a2958ea96695878429a76189b94323`.
The receipt pipeline delivered by PR #116 remains in place. This report does
not complete issue #113's enforcement acceptance criterion or authorize repairs.

**D1 security enforcement remains deferred**, as recorded in the
[consolidation status](plans/graphify-product-consolidation-status.md).
Both scanner steps still have `continue-on-error: true`. No runtime code,
dependency declaration, lock, suppression, hook, or installation policy changes
are part of this report. The only proposed repository changes are this file and
the navigation/status text in [security-scans.md](security-scans.md).

## Fresh evidence and its coverage

A task-owned detached checkout at the revision above used its own `.venv` and
uv cache. The final scans also used a new task-owned HTTP cache selected through
`XDG_CACHE_HOME` and `PIP_CACHE_DIR`; the installed platformdirs selection was
checked to resolve inside the task root. Setup ran `uv sync --frozen` with Python downloads disabled; it used
the existing CPython 3.14.3 interpreter, Git 2.55.0, and uv 0.11.30.
No existing project environment or global installation was updated. The primary
checkout and both issue105 worktrees were left in place.

| Scanner | Started (UTC) | Version | Completion | Coverage | Reportable records | Process exit |
| --- | --- | --- | --- | --- | ---: | ---: |
| Bandit | 2026-10-05 07:53:38 | 1.9.4 | Complete, findings | All 111 `graphify/**/*.py` files; 88,098 counted lines | 11: 6 high, 5 medium | 1 |
| pip-audit | 2026-10-05 07:53:39 | 2.10.1 | Complete, clean | 90 selected and installed third-party packages, frozen default plus dev | 0 | 0 |

The unchanged `tools/security_receipts.py` finalizer revalidated coverage,
provenance, raw-output hashes, scanner exits, and repository identity. Both
receipts have `dirty: false`, no completion errors, and the same revision and
input hashes. Scanner subprocess durations were 3.108 seconds and 2.006 seconds,
respectively. Local Actions outcomes, tolerated conclusions, final job status,
and uploaded artifact URL are **unavailable**, not passing CI evidence.

An earlier helper run at 07:41 UTC used pip-audit's default HTTP cache. Its
receipts are retained as preliminary evidence, but do not establish an empty-cache
query. The final pair above supersedes that pair; its new cache contains 90
response files after the audit. Earlier effects on the host's default cache were
not snapshotted. This is an isolation limitation of the preliminary run, not a
global package installation change or a claim that those cache files were preserved.

Reproduce the final scanner invocation from a task-owned frozen checkout, with
fresh output/cache directories outside it (paths here are task-relative):

```sh
export UV_CACHE_DIR=../uv-cache
export UV_PYTHON_DOWNLOADS=never
export XDG_CACHE_HOME=../audit-cache
export PIP_CACHE_DIR=../pip-cache
uv sync --frozen
uv run --frozen python -m tools.security_receipts scan --scanner bandit --output ../isolated-receipts
uv run --frozen python -m tools.security_receipts scan --scanner pip-audit --output ../isolated-receipts
.venv/bin/python -m tools.security_receipts finalize --output ../isolated-receipts
```

Run each scanner command separately: Bandit's exit 1 denotes completed findings
here. Verify the resolved HTTP cache path on the target host before scanning;
environment-variable support is a property of the installed platformdirs build.
This does not add a flag or change scanning rules in the receipt helper.

Bandit's command was `python -m bandit -r graphify -ll -f json -o <output>`.
The reported 11 records come from filtered `results`; metrics also count 106
low-severity alerts, which this batch does not disposition. Existing `nosec`
comments and scanner defaults remain effective. The JSON metrics report one
`nosec` and seven skipped tests; these are suppression metrics, not skipped
source files. The JSON `errors` list is empty and every expected source file has
metrics. Stderr retains warnings about existing suppression comments, but no
error/internal-plugin diagnostics. Complete coverage means this configured scan
finished, not that suppressed rules or lower-severity findings were reviewed.

pip-audit used the helper's exact marker-selected requirements with
`--strict --no-deps --disable-pip --progress-spinner off
--vulnerability-service pypi --format json --requirement <requirements>`.
The only excluded distribution is the proven editable root `graphifyy 0.10.0`.
There are zero current advisory records, zero distinct advisory IDs, and zero
duplicate advisory records. The historical 12 records/nine distinct advisories
in issue #151 are a different snapshot; #151 was closed when inspected for this
report. Historical duplicates are not current remaining findings.

This audit does not cover optional extras, an independently resolved isolated
build backend, other platform marker selections, installed package byte
integrity, unpublished vulnerabilities, or vulnerabilities in Python/Expat/Git.
Public PyPI advisory data changes; a clean receipt is time- and inventory-bound.
pip-audit's warning about unhashed requirements is retained: lock/inventory
reconciliation proves names and versions, not installed-byte provenance.

### Receipt identities and local visibility

Raw evidence is retained outside the repository in this task's receipt directory,
identified in the local completion report. It has not been uploaded or committed.
The following digests allow an operator with those files to check the exact
snapshot; hashes alone do not make private/local raw evidence available to a
future reviewer.

| Input or evidence | SHA-256 |
| --- | --- |
| `uv.lock` | `aee9be6b67874f287f822d584329abacdd2032a12165879bebe61dbeb40393fa` |
| `pyproject.toml` | `a20ddc8451bb262c6e84949b083f471f802413fd307e45440447c590b55809ab` |
| `tools/security_receipts.py` | `28336937d942cf8b8d738cf95183bbba8341cb039d26534c59b0ea4617e1fe2b` |
| Bandit source coverage map | `01ee24a60c5db0831c93746c38f123655ba3b4140301c6855c253af63d4c5f54` |
| Bandit `receipt.json` | `b24a4e7f7bc857dba5167c602b208176a5724b30c285f70d354c672acb91c0ed` |
| Bandit `scanner.json` | `dca973874f310249f400e80fd3ee0c7c2502f54130acda2905fb469ca18d28de` |
| pip-audit coverage map | `e1f86ea358de1b43981325bdde8c989feeb80e400b17b58d49f5a4a8b10dc91a` |
| pip-audit `receipt.json` | `ba5346e85c3be72f16bea06a0c784229a245da501fa001fe7d2218e31bc0d349` |
| pip-audit JSON (`scanner.stdout`) | `8a9540a2df859a5a965a779e8ec27b20a8e023e75177f10a8d69248dd2853382` |
| Revalidated `summary.json` | `cc2d7a821cd8156cee51d6e4ffcdbe3dea782822626d089683ec8b70058b0a41` |

## Disposition of every reportable Bandit record

Line numbers and zero-based columns below identify this exact source snapshot.
Record IDs are report-local labels, not suppressions or accepted exceptions.
[Bandit B324](https://bandit.readthedocs.io/en/latest/plugins/b324_hashlib.html)
checks hash calls and `usedforsecurity`; its high severity does not resolve the
caller's threat model. [B104](https://bandit.readthedocs.io/en/latest/plugins/b104_hardcoded_bind_all_interfaces.html)
matches wildcard-address strings, including comparisons and warning text.

| Record | Rule; severity/confidence | Location | Reachable trigger and consequence | Disposition at this revision |
| --- | --- | --- | --- | --- |
| B01 | B324; high/high | `graphify/_minhash.py:47`, column 43 | Corpus-label shingles enter `MinHash.update`; SHA-1's first four bytes feed a probabilistic candidate sketch. LSH candidates still undergo label/variant/numeric/Jaro checks in `dedup.py`; this is not credential or approval verification. | Non-security statistical hash; no collision-induced wrongful merge demonstrated. Preserve compatibility. A sketch collision alone is not a proven dedup defect. |
| B02 | B324; high/high | `graphify/extractors/engine.py:18`, column 13 | C# namespace text enters `_csharp_namespace_id`, producing a 64-bit truncated deterministic ID. A collision could conflate namespace nodes, which are intentionally exempt from path disambiguation. | Graph identity risk remains, but no concrete namespace collision or bad graph was demonstrated. Do not claim mathematical uniqueness or replace stable IDs just to remove the alert. |
| B03 | B324; high/high | `graphify/extractors/resolution.py:652`, column 23 | Distinct paths that normalize to the same node ID enter `_disambiguate_colliding_node_ids`; the fallback uses a 24-bit SHA-1 prefix without a second uniqueness check. | **Demonstrated defect RC1:** chosen source paths still collide and graph construction loses a distinct function/file. This is a truncation/uniqueness failure, not a break of full SHA-1 or a demonstrated authorization bypass. |
| B04 | B324; high/high | `graphify/protected_change_equivalence.py:149`, column 15 | `_tree_oid` reconstructs the SHA-1 Git tree identity for externally pinned candidate manifests. | Format-required Git object hash; no equivalence bypass demonstrated. Full inputs and content records have separate SHA-256 binding. Retain the SHA-1-format contract and its acquisition/authenticity assumptions. |
| B05 | B324; high/high | `graphify/protected_change_equivalence.py:216`, column 10 | `verify_commit_equivalence` checks the actual commit-object identity, tree, parent and transition against pinned inputs. | Format-required Git commit hash; no bypass demonstrated. The helper returns evidence with `approval_granted: false`; it does not authenticate receipts or grant approval. |
| B06 | B324; high/high | `graphify/protected_change_verifier.py:453`, column 7 | Immutable DIRC v2/v3 index bytes enter `_parse_index_bytes`; the trailer is checked before parsing entries. A corrupted trailer is rejected. | Format-required SHA-1 checksum; no parser/admission bypass demonstrated. Candidate/raw-byte identities use SHA-256 separately. Replacing the trailer hash would violate the supported Git format. |
| B07 | B104; medium/medium | `graphify/serve.py:1773`, column 16 | An operator explicitly chooses `0.0.0.0`, `::`, or an empty host; `_build_http_app` disables DNS-rebinding protection for that wildcard mode. | Intentional exposure branch with material residual risk, not a hidden default bind. Public requests are permitted without a configured key; this deserves a separate product/security decision, not a silent baseline exemption. SDK integration was not executed here. |
| B08 | B104; medium/medium | `graphify/serve.py:1858`, column 16 | `serve_http` detects wildcard binding without a nonblank API key and enters the warning branch. Defaults are stdio transport and loopback for HTTP. | Warning condition, not the binding call; related to B07. Explicit unauthenticated exposure is allowed by current documented behavior. No default-exposure defect demonstrated. |
| B09 | B104; medium/medium | `graphify/serve.py:1860`, column 40 | The warning renders `0.0.0.0` when the selected host is blank. `uvicorn.run` later receives the selected host. | Warning text, not another listener or another vulnerability. Keep the warning. This is another scanner record for the B07/B08 exposure concern. |
| B10 | B314; medium/high | `graphify/xml_admission.py:28`, column 11 | Five project/Maven readers pass bounded XML to `ET.fromstring` with the custom rejecting parser. Untrusted DOCTYPE/ENTITY input is the relevant trigger. | Contextually mitigated declaration-expansion/DTD path after PR #176; retain the alert and runtime limits. Fresh regressions reject declarations before expansion and intercept external I/O. Not a general resource-safety certification. |
| B11 | B314; medium/high | `graphify/xml_admission.py:28`, column 41 | The same expression constructs `ET.XMLParser(target=_RejectingTreeBuilder())`; its `doctype` callback raises. | Second API-rule record for the same admission boundary, not proof of a second exploit. Same bounded, tested-runtime disposition as B10. |

For B04–B06, collision resistance of Git SHA-1 is not asserted. These checks
serve a fixed SHA-1 format within separately pinned SHA-256 evidence; authenticity,
complete acquisition and acceptance remain caller/policy obligations. A future
SHA-256 Git-format feature needs its own contract, not a scanner-only replacement.
FIPS/non-security-hash annotation compatibility was not tested on a FIPS build.

For B07–B09, the API-key middleware rejects missing/wrong keys with 401 and
accepts valid keys using a constant-time comparison. This is a shared-secret
gate, not TLS, OAuth, or per-client authorization. A wildcard choice also relaxes
Host protection even when a key exists. No real network listener was opened in
this batch; firewall, browser, TLS, and hostile-client behavior remain untested.

For B10/B11, the five readers are Lazarus LPI, SLNX, CSPROJ, XAML and Maven POM.
Project reads cap at 2 MiB; Maven caps at 2,000,000 bytes. SourceIO retains tighter
limits, consumed-byte evidence and failure latching. `parse_xml` itself has no
byte cap: callers own bounded admission. The tested runtime is CPython 3.14.3 /
Expat 2.6.3. Byte caps do not provide general CPU, depth, or tree-memory limits,
and reads are not atomic snapshots. Current [Python XML security guidance](https://docs.python.org/3.14/library/xml.html#xml-security)
warns about Expat builds below 2.7.2. Declaration rejection does not certify
protection from every parser-level issue; no such separate exploit was demonstrated.

## Demonstrated defect: separate repair candidate RC1

**Contract and impact:** the collision pass promises to separate distinct
same-ID source symbols. Two real Python files with these repo-relative paths
normalize to the same stem and both have SHA-1 prefix `ecad21`:

```text
a_a.a.a_a.a_a.a.a_a_a.a_a.a.a.a.a.a.a.a.a.py
a.a_a_a_a_a.a.a.a_a.a_a_a_a.a.a.a.a.a.a.a.py
```

Both contain `def marker():\n    return 1\n`. The ordinary strict, uncached
extractor returns four records (two files and two `marker()` functions), but
only two distinct IDs. `build_from_json` retains two graph nodes and only one
`marker()` function, with the later source's attributes. An attacker who can
choose corpus paths can induce graph conflation; accidental collisions are also
possible. No privilege escalation, protected-state mutation, or exploit against
a remote service was shown. The deterministic search found the pair after
7,455 candidate paths, but the fixed pair suffices to reproduce the defect.

Reproduce from the frozen task environment; all writes stay in a disposable
directory and extraction disables ambient output/cache:

```sh
.venv/bin/python - <<'PY'
import hashlib
import tempfile
from pathlib import Path
from graphify.extract import extract
from graphify.build import build_from_json

names = [
    'a_a.a.a_a.a_a.a.a_a_a.a_a.a.a.a.a.a.a.a.a.py',
    'a.a_a_a_a_a.a.a.a_a.a_a_a_a.a.a.a.a.a.a.a.py',
]
assert {hashlib.sha1(n.encode()).hexdigest()[:6] for n in names} == {'ecad21'}
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    paths = [root / name for name in names]
    for path in paths:
        path.write_text('def marker():\n    return 1\n')
    result = extract(paths, strict=True, parallel=False,
                     ambient_output=False, quiet=True)
    graph = build_from_json(result, root=root)
    before = sum(n.get('label') == 'marker()' for n in result['nodes'])
    after = sum(a.get('label') == 'marker()' for _, a in graph.nodes(data=True))
    print('records:', len(result['nodes']), 'graph nodes:', len(graph))
    print('marker functions before/after:', before, after)
    assert before == 2 and after == 1  # Demonstrates current failure.
PY
```

Candidate repair scope: collision disambiguation and affected edge/raw-call
remapping, with a deterministic postcondition that distinct source owners stay
distinct even when fallback digests collide. Changing SHA-1 to another hash while
keeping a six-hex-character suffix does not solve that postcondition. Preserve
ordinary noncolliding IDs and cross-producer compatibility. A separate repair
should turn the fixed pair into a regression, verify both files/functions and
their source-specific edges survive through graph construction, and test input
order/determinism and unsalted existing IDs. No repair was made here.

## Proposed baseline and new-finding policy — pending D1

Recommended initial scope is the existing medium/high Bandit scan and frozen
default-plus-dev dependency audit. Any expanded severity, extra, platform or
build-backend matrix needs a separately named coverage decision. This proposal
does not accept the 11 records as a security allowlist or make scans blocking.

1. **Freeze evidence before accepting a baseline.** Bind revision, scanner/rule
   versions and configuration, source/inventory and input hashes, runtime,
   advisory query time, raw receipts, and completion. An incomplete scan has
   an unknown finding count and cannot initialize, shrink, or refresh a baseline.
   D1 must decide enforcement of collection failures separately from findings.
2. **Record individual dispositions.** Keep each rule occurrence and its trigger,
   threat model, impact, evidence, reviewer/acceptance owner, rationale and
   revalidation condition. Distinguish contextual non-defect, intentional risk,
   mitigated-with-limits, demonstrated defect awaiting repair, and unresolved.
   RC1 cannot become a permanent exemption merely because it exists at baseline.
   Any temporary risk acceptance needs an explicit owner, expiry and repair link.
3. **Match occurrences, not totals or line numbers.** A candidate Bandit key should
   combine rule ID, repo-relative file, enclosing symbol, called API or literal
   role, normalized AST context digest and occurrence identity/multiplicity.
   Preserve B10 versus B11 and B07–B09 even when grouping their common cause for
   humans. A moved line may preserve context; an edited call, altered guard,
   extra occurrence, severity increase, or ambiguous match needs fresh triage.
   A source-only contextual fingerprint must not hide changes in caller guards,
   bounded readers, runtime or authentication. These are revalidation inputs.
4. **Treat new findings as visible deltas.** New/unmatched occurrences, expired
   exceptions, changed assumptions and reopened findings should create explicit
   review-required output. A proven new defect should fail a future accepted
   gate; contextual rule noise needs a supported disposition before exemption.
   Do not suppress entire rules, use `nosec` broadly, auto-rebaseline changes,
   or accept a report solely because its total stayed constant or decreased.
5. **Deduplicate dependency advisories without hiding packages.** Keep raw
   records and raw count. Report unique `(normalized package, version, canonical
   advisory)` findings and distinct advisory IDs separately; reconcile CVE/GHSA
   aliases from explicit advisory metadata. The same advisory on two packages
   remains two affected-package findings. Newly published advisories on unchanged
   lock inputs are new evidence, not automatically grandfathered findings.
   An alias-only duplicate is not another defect. Inventory/skips or service
   failures invalidate completion instead of yielding a clean result.
6. **Remove resolved entries only with proof.** Preserve history and link the
   repaired revision, focused regression and complete replacement receipt.
   A missing result caused by filtering, suppression, missing files, inventory
   drift or collection failure does not prove resolution. Scanner/rule upgrades
   require a reviewed migration rather than an automatic baseline replacement.
7. **Prove visibility before enforcement.** A separately authorized implementation
   should test a real new weak-security-hash occurrence, a guard change at an
   existing call, two XML calls at one line, an added duplicate occurrence,
   line-only movement, equal-total replacement of an old alert by a new one,
   expired exceptions, alias duplicates across packages, a new advisory on an
   unchanged lock, missing files/packages, suppressed or malformed results,
   plugin/network failure, reused/tampered receipts and changed scanner versions.
   Check diagnostics and protected-state preservation as well as exit status.

Open D1 decisions: acceptance owner and exception lifetime; whether all
untriaged medium/high deltas or only proven defects block; treatment of scanner
unavailability; severity/extra/platform coverage; and public-binding risk
acceptance. None is selected by this local proposal. Advisory reporting continues.

## Validation and explicit gaps

- Fresh focused pytest run: **505 passed, one module skipped, 15.98 seconds** for
  XML admission, MinHash, protected index/equivalence and scanner receipts, with
  `tests/test_serve_http.py` skipped because `mcp` is absent. This is focused
  conformance evidence, not full-suite or release qualification.
- Two existing extraction collision/noncollision controls passed (143 tests
  deselected). They cover ordinary separator collisions, not the demonstrated
  same-salt pair. The separate fixed-pair probe reproduced the failure.
- A socket-free `serve_http` probe stubbed uvicorn and the app factory: loopback
  selected loopback; wildcard/no-key choices emitted warnings; a configured key
  omitted the unauthenticated warning. Direct ASGI middleware probes returned
  401 for missing/wrong keys and delegated valid X-API-Key/Bearer credentials.
  These probes do not exercise installed MCP/Starlette wiring, DNS-rebinding
  behavior, TLS, or real network exposure.
- No real FIPS build, namespace-hash collision, MinHash collision-induced bad
  merge, Git chosen-collision bypass, or general XML resource-exhaustion attack
  was demonstrated. The report does not label those hypothetical defects proven.
- The existing knowledge graph was read for navigation; its reported commit
  `d1aefd9b` differs from this baseline. Source and fresh receipts, rather than
  stale graph relationships, support current dispositions. No code was edited,
  so no graph regeneration was required or performed.
- Raw receipts and probes are session-local. No hosted artifacts, CI run,
  optional-runtime audit, isolated build-backend audit, full suite, accepted D1
  policy, baseline implementation, or enforcement regressions were produced.
  Issue #113 remains open. Local report completion is not issue closure.

## RC1 fix status — 2026-10-05

The audit above remains bound to
`a558766ba5a2958ea96695878429a76189b94323`. Its scan counts, dispositions,
validation results and fixed-pair reproduction describe that historical
revision. In particular, the reproduction's “current failure” assertion refers
only to that revision; it is expected to fail after the RC1 repair because both
marker functions now survive. This status append is not a new security scan or
a replacement receipt.

[PR #192](https://github.com/Villeneuve-Ventures/graphify/pull/192) delivered RC1
in source commit `547db70172e85fa31c488c1133477f5689d40285` and merged into
`v8` on 2026-10-05 at `a511df7089c4c7bb271c2e30d4bdcc2043bc4058`.
The repair changes only `graphify/extractors/resolution.py` and
`tests/test_node_id_disambiguation.py`.

The [merged allocator](https://github.com/Villeneuve-Ventures/graphify/blob/a511df7089c4c7bb271c2e30d4bdcc2043bc4058/graphify/extractors/resolution.py#L599-L694)
reserves all original IDs, allocates available preferred IDs first, then checks
numeric suffixes against reserved and newly allocated IDs. Allocation order is
deterministic by original ID and raw source path. Existing source-qualified
edge and raw-call remapping use the final allocation. The
[merged regressions](https://github.com/Villeneuve-Ventures/graphify/blob/a511df7089c4c7bb271c2e30d4bdcc2043bc4058/tests/test_node_id_disambiguation.py)
check that the fixed pair retains both file nodes, both marker functions and
correctly attributed edges through graph construction, including reversed input
order. They also cover equal full digests, occupied IDs, raw callers, stable
noncolliding and existing distinct IDs, and AST/semantic ID compatibility.
Saved graphs that already lost an owner need re-extraction; the repair does not
recover missing nodes in existing graph data.

Raw scanner receipts remain locally retained with a durable local copy. The
two scanner receipts, their JSON outputs and the revalidated summary in that
copy match the five corresponding hashes above. They have not been committed
or uploaded, so they are not PR-visible or shared proof. Public source and test
links support the RC1 implementation status; they do not publish the historical
scanner evidence or establish full security qualification.

RC1 is repaired at the merged revision; B03 remains a historical finding at the
audit revision. This does not accept the proposed baseline, change scanner
suppression or enforcement, or complete issue #113. **D1 security enforcement
remains deferred** under the [consolidation status](plans/graphify-product-consolidation-status.md#preserved-decisions-and-limits).
The proposal and unanswered owner decisions above remain unaccepted.
