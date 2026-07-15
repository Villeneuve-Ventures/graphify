from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.workspace_artifacts import verify_trusted_manifest
from tools.workspace_artifacts.candidate import (
    build_and_compare_candidates,
    build_candidate,
    prove_candidate,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.workspace_artifacts",
        description="Build and prove the isolated Graphify workspace P1 candidate.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build the reproducible committed candidate")
    build.add_argument("--repo-root", type=Path, required=True)
    build.add_argument("--output-root", type=Path, required=True)
    build.add_argument(
        "--comparison-output-root",
        type=Path,
        help="also build into this clean root and require all file digests to match",
    )

    verify = commands.add_parser("verify", help="verify against a frozen local manifest")
    verify.add_argument("--artifact-root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path)

    prove = commands.add_parser("prove", help="run isolated home/tamper/rollback proofs")
    prove.add_argument("--artifact-root", type=Path, required=True)
    prove.add_argument("--proof-root", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "build":
        if args.comparison_output_root:
            result = build_and_compare_candidates(
                repo_root=args.repo_root.resolve(),
                output_root=args.output_root.resolve(),
                comparison_output_root=args.comparison_output_root.resolve(),
            )
        else:
            result = build_candidate(
                repo_root=args.repo_root.resolve(),
                output_root=args.output_root.resolve(),
            )
    elif args.command == "verify":
        artifact_root = args.artifact_root.resolve()
        manifest = (args.manifest or artifact_root / "trusted-manifest.json").resolve()
        verify_trusted_manifest(
            artifact_root=artifact_root,
            trusted_manifest=manifest.read_bytes(),
        )
        result = {"verified": True, "artifact_root": str(artifact_root)}
    else:
        result = prove_candidate(
            artifact_root=args.artifact_root.resolve(),
            proof_root=args.proof_root.resolve(),
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
