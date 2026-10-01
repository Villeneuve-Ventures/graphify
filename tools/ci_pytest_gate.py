"""Record individual matrix outcomes and require all four in the aggregate gate.

These required execution receipts are separate from advisory timing output.
The matrix rollup is also checked, but never substitutes for individual receipts.
Run identity and attempt binding fail closed on partial reruns/missing shards.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SHARDS = (1, 2, 3, 4)


def identity(env: dict[str, str]) -> dict:
    return {name: env[name] for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")}


def artifact_name(shard: int, env: dict[str, str]) -> str:
    return f"pytest-result-{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}-shard-{shard}"


def record(output: Path, shard: int, env: dict[str, str]) -> None:
    if shard not in SHARDS:
        raise ValueError("Invalid shard")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "schema_version": 1, "shard": shard, **identity(env),
        "pytest_outcome": env["PYTEST_OUTCOME"], "job_status": env["JOB_STATUS"],
    }, sort_keys=True) + "\n", encoding="utf-8")


def check(root: Path, env: dict[str, str]) -> None:
    if env.get("MATRIX_RESULT") != "success":
        raise ValueError(f"Matrix execution did not succeed: {env.get('MATRIX_RESULT')!r}")
    expected = {root / artifact_name(shard, env) / "result.json" for shard in SHARDS}
    actual = set(root.rglob("*.json"))
    if actual != expected:
        raise ValueError(f"Missing or unexpected shard receipts: {sorted(map(str, actual ^ expected))}")
    for shard in SHARDS:
        path = root / artifact_name(shard, env) / "result.json"
        receipt = json.loads(path.read_text())
        if receipt != {
            "schema_version": 1, "shard": shard, **identity(env),
            "pytest_outcome": "success", "job_status": "success",
        }:
            raise ValueError(f"Unsuccessful or mismatched shard receipt: {path}")
    print("All four pytest shard executions succeeded")


def smoke() -> None:
    # Process-scoped disposable home: never update the operator's installed skills.
    with tempfile.TemporaryDirectory(prefix="graphify-install-smoke-") as directory:
        env = {**os.environ, "HOME": directory, "USERPROFILE": directory,
               "CODEX_HOME": str(Path(directory) / ".codex"),
               "XDG_CONFIG_HOME": str(Path(directory) / ".config")}
        for args in (("--help",), ("install",)):
            subprocess.run([sys.executable, "-m", "graphify", *args], env=env, check=True)
        skill = Path(directory) / ".claude/skills/graphify/SKILL.md"
        if not skill.is_file() or not skill.read_text():
            raise ValueError("Installation did not produce the default Claude skill")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    writer = commands.add_parser("record")
    writer.add_argument("--shard", type=int, choices=SHARDS, required=True)
    writer.add_argument("--output", type=Path, required=True)
    checker = commands.add_parser("check")
    checker.add_argument("--receipts", type=Path, required=True)
    commands.add_parser("smoke")
    args = parser.parse_args()
    try:
        if args.command == "record":
            record(args.output, args.shard, os.environ)
        elif args.command == "check":
            check(args.receipts, os.environ)
        else:
            smoke()
    except (KeyError, ValueError, OSError) as error:
        parser.exit(1, f"Pytest gate: {error}\n")


if __name__ == "__main__":
    main()
