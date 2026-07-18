# Compatibility and artifact contract

## Frozen tuple

The P1 candidate is one Graphify distribution with this fixed identity:

| Field | Value |
| --- | --- |
| distribution | `graphifyy` |
| distribution version | `0.9.16+workspace.1` |
| distribution build | `git:<fork-commit>` |
| engine baseline | `0.9.16` |
| upstream commit | `a0e4a1c6bd3a99edfdd84ad30927003f51face6a` |
| extractor/cache ABI | `graphify-0.9.16` |
| state schema | `1` |
| adapter contract | `1` |
| CLI contract | `1` |

The manifest also records the fork commit, Python implementation/version,
platform identity, frozen `uv.lock`, wheel, exact generated Codex skill,
contract bundle, fixture manifest, provenance record, and CycloneDX 1.5 SBOM
hashes. It does not model the
engine as an independently replaceable component inside the distribution.

The artifact-generation and proof toolchain separately pins uv `0.11.29` and
records its full reported build string in provenance. This proof-only pin is
not part of runtime compatibility and does not constrain ordinary Graphify
usage.

## Version behavior

- Unknown contract schema versions are rejected before field interpretation.
- Unknown engine, distribution, extractor/cache ABI, adapter, state-schema, or
  CLI tuples are rejected before later implementations may stage or move state.
- Older code does not write newer schemas.
- Pre-workspace `graphify-out` state is neither importable nor promotable.
  Adoption must build and certify a new generation through the exact supported
  `0.9.16` tuple without rewriting the pre-workspace state.
- New upstream releases enter a non-promoting whole-artifact lane. A tag alone
  never widens support. The lane has probe metadata only: no executable adapter,
  staging, or promotion authority.

## Artifact set

`python -m tools.workspace_artifacts build --repo-root <absolute-repo> \
--output-root <absolute-ignored-or-external-root>` creates these deterministic
artifacts:

- `graphifyy-0.9.16+workspace.1-py3-none-any.whl`;
- `runtime-bundle.zip`, containing `uv.lock` and the locked dependency export;
- `runtime-requirements.txt`;
- `skill-bundle.zip`;
- `contract-bundle.zip`;
- `fixture-bundle.zip`;
- `fixture-manifest.json`;
- `provenance.json`;
- `sbom.cdx.json`;
- `offline-rollback.zip`;
- `compatibility.json`; and
- `trusted-manifest.json`.

ZIP members use fixed timestamps, normalized modes, sorted paths, and no host
owner metadata. Wheel builds and the normalized SBOM use the fixed upstream
commit epoch. Pass `--comparison-output-root <second-absolute-root>` to build a
second complete candidate and require identical digests for every output file.

The trusted manifest is the local trust anchor for P1 tamper tests. V1 detects
artifact mismatch relative to that frozen manifest; it does not claim safety if
both the artifact and trust anchor are maliciously replaced.
