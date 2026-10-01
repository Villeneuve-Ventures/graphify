"""Reproduce the four whole-module CI shards from the preserved Linux sample.

Run: python -m tools.ci_shard_allocation TIMINGS_JSON [--check]
The source bytes must match the pinned sample; no new dependencies are needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

SHARDS = (1, 2, 3, 4)
BASELINE = "6dcd6941ec47a5e5d83044a2ac702dc2d3a6c984"
TIMING_SHA256 = "6bbcb47b2c7a1e2a03c01e769130b17b54903e799f7d095fb023cc2a03ffa9d1"
ALLOCATION_PATH = Path(__file__).with_name("pytest_shards.json")
RECIPE = (
    "Longest processing time first: sort modules by descending measured total "
    "microseconds, then ascending POSIX path; assign to the least-loaded shard, "
    "breaking load ties by ascending shard number. Whole modules remain intact."
)


def balance(weights: dict[str, int]) -> tuple[dict[str, int], dict[int, int]]:
    loads = dict.fromkeys(SHARDS, 0)
    owners = {}
    for path in sorted(weights, key=lambda path: (-weights[path], path)):
        shard = min(SHARDS, key=lambda shard: (loads[shard], shard))
        owners[path] = shard
        loads[shard] += weights[path]
    return owners, loads


def derive(raw: bytes, root: Path) -> dict:
    if hashlib.sha256(raw).hexdigest() != TIMING_SHA256:
        raise ValueError("Timing bytes do not match the pinned successful Linux sample")
    timing = json.loads(raw)
    env = timing["environment"]
    if not (
        timing["schema_version"] == 1 and timing["status"] == "complete"
        and timing["mode"] == "run" and timing["exit_code"] == 0
        and timing["collection_completed"] and timing["sessionfinish_completed"]
        and timing["collection_errors"] == timing["deselected_items"] == 0
        and not timing["incomplete_reasons"] and not timing["output_errors"]
        and timing["selected_items"] == timing["executed_items"]
        == timing["completed_items"] == 7021
        and len(timing["modules"]) == 229
        and env["source_revision"] == env["ci"]["GITHUB_SHA"] == BASELINE
        and env["ci_source_matches_checkout"] is True
        and env["ci"]["RUNNER_OS"] == "Linux" and env["machine"] == "x86_64"
        and env["python"].startswith("3.14.7 ") and env["pytest"] == "9.0.3"
        and env["git"] == "git version 2.55.0"
        and not env["tracked_changes"] and not env["errors"]
    ):
        raise ValueError("Timing sample is incomplete or has incompatible source/environment")
    # Read immutable Git blobs, never dirty working files or another checkout.
    import subprocess
    sources = {"observer": "tools/pytest_timings.py", **{
        path: path for path in (".github/workflows/ci.yml", "pyproject.toml", "uv.lock")
    }}
    for name, path in sources.items():
        blob = subprocess.check_output(["git", "show", f"{BASELINE}:{path}"], cwd=root)
        if hashlib.sha256(blob).hexdigest() != env["sha256"][name]:
            raise ValueError(f"Baseline source binding mismatch: {name}")
    weights = {}
    for module in timing["modules"]:
        path = module["path"]
        if path in weights or not path.startswith("tests/") or ".." in Path(path).parts:
            raise ValueError("Invalid or duplicate module path")
        seconds = [module[f"{phase}_seconds"] for phase in ("setup", "call", "teardown")]
        total = module["total_seconds"]
        if not all(math.isfinite(value) and value >= 0 for value in (*seconds, total)):
            raise ValueError("Nonfinite or negative module phase total")
        if abs(sum(seconds) - total) > 0.000002:
            raise ValueError("Inconsistent phase total")
        if module["selected_items"] != module["executed_items"]:
            raise ValueError("Incomplete module execution")
        weights[path] = round(total * 1_000_000)
    if sum(module["selected_items"] for module in timing["modules"]) != 7021:
        raise ValueError("Inconsistent module inventory")
    owners, loads = balance(weights)
    return {
        "schema_version": 1, "shards": list(SHARDS), "recipe": RECIPE,
        "fallback": "SHA-256 of relative POSIX module path, first 4 bytes big-endian modulo 4, plus 1",
        "provenance": {
            "run_url": "https://github.com/Villeneuve-Ventures/graphify/actions/runs/36871547583",
            "artifact_id": 11169867555, "artifact_name": "pytest-timings-36871547583-1",
            "archive_sha256": "ca30c8b93af204818b8cf4d08b073ff2582f3c59d7f30c6ab99e023ef9991967",
            "timings_sha256": TIMING_SHA256, "source_revision": BASELINE,
            "environment": env, "selected_items": 7021, "module_count": 229,
            "collection_seconds": timing["collection_seconds"],
            "session_seconds": timing["session_seconds"], "subtests": timing["subtests"],
        },
        "predicted_microseconds": {str(shard): loads[shard] for shard in SHARDS},
        "modules": {path: {"microseconds": weights[path], "shard": owners[path]}
                    for path in sorted(weights)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("timings", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = derive(args.timings.read_bytes(), Path(__file__).resolve().parents[1])
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.check:
        if ALLOCATION_PATH.read_text() != data:
            raise SystemExit("Committed allocation differs from reproducible sample")
    else:
        ALLOCATION_PATH.write_text(data, encoding="utf-8")
    print("Predicted module seconds:", {k: v / 1_000_000
                                      for k, v in payload["predicted_microseconds"].items()})


if __name__ == "__main__":
    main()
