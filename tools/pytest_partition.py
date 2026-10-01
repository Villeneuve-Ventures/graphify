"""Opt-in whole-module sharding after full normal tests/ collection.

Load with -p tools.pytest_partition --ci-shard=N. Without the option this is inert.
Timing metadata describes validated selection; it never changes pytest results.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.ci_shard_allocation import ALLOCATION_PATH, SHARDS, balance

PARTITION = pytest.StashKey[dict]()


def allocation() -> tuple[dict[str, int], str]:
    raw = ALLOCATION_PATH.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("shards") != list(SHARDS):
        raise ValueError("Invalid allocation configuration")
    modules = data.get("modules")
    if not isinstance(modules, dict) or not modules or any(
        not isinstance(path, str) or not path.startswith("tests/")
        or ".." in Path(path).parts or not isinstance(module, dict)
        or set(module) != {"microseconds", "shard"}
        or type(module["shard"]) is not int or module["shard"] not in SHARDS
        for path, module in modules.items()
    ):
        raise ValueError("Invalid allocation modules or owners")
    weights = {path: module["microseconds"] for path, module in modules.items()}
    if any(type(value) is not int or value < 0 for value in weights.values()):
        raise ValueError("Invalid allocation weight")
    owners, loads = balance(weights)
    if owners != {path: module["shard"] for path, module in data["modules"].items()} or (
        {str(shard): load for shard, load in loads.items()} != data["predicted_microseconds"]
    ):
        raise ValueError("Allocation does not match the balancing recipe")
    return owners, hashlib.sha256(raw).hexdigest()


def owner(path: str, owners: dict[str, int]) -> int:
    if path in owners:
        return owners[path]
    digest = hashlib.sha256(path.encode("utf-8")).digest()
    return 1 + int.from_bytes(digest[:4], "big") % len(SHARDS)


def inventory(items: list[pytest.Item], root: Path) -> dict[str, str]:
    result = {}
    for item in items:
        path = item.path.relative_to(root).as_posix()
        if item.nodeid in result:
            raise pytest.UsageError("CI shards require unique collected node IDs")
        result[item.nodeid] = path
    return result


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("CI").addoption("--ci-shard", type=int, choices=SHARDS,
                                    help="Run one of four whole-module CI shards.")


def pytest_configure(config: pytest.Config) -> None:
    shard = config.getoption("ci_shard")
    if shard is None:
        return
    if Path.cwd().resolve() != config.rootpath.resolve() or any(
        Path(argument).resolve() != config.rootpath / "tests"
        for argument in config.args
    ) or len(config.args) != 1:
        raise pytest.UsageError("CI shards require full tests/ discovery from the repository root")
    options = (
        "keyword", "markexpr", "lf", "failedfirst", "newfirst", "stepwise",
        "stepwise_skip", "stepwise_reset", "deselect", "ignore", "ignore_glob",
        "pyargs", "keepduplicates", "confcutdir", "noconftest", "inifilename",
        "collect_in_virtualenv", "doctestmodules", "numprocesses",
        "dist", "looponfail", "testmon", "continue_on_collection_errors",
    )
    # xdist's default --dist=no is compatible; executing workers is not.
    if any(config.getoption(name, default=False) not in (None, False, "no", [], "")
           for name in options):
        raise pytest.UsageError("CI shards cannot be combined with selection/distribution options")
    selection_ini = {"testpaths", "python_files", "python_classes", "python_functions",
                     "norecursedirs", "addopts"}
    if any(value.split("=", 1)[0].strip() in selection_ini
           for value in (config.getoption("override_ini", default=[]) or [])):
        raise pytest.UsageError("CI shards cannot override discovery configuration")
    try:
        owners, identity = allocation()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise pytest.UsageError(f"Invalid CI shard allocation: {error}") from error
    config.pluginmanager.register(_Partition(config, shard, owners, identity), "ci-partition")


class _Partition:
    def __init__(self, config, shard, owners, identity):
        self.config, self.shard, self.owners, self.identity = config, shard, owners, identity

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, items):
        full = inventory(items, self.config.rootpath)
        result = yield
        if inventory(items, self.config.rootpath) != full:
            raise pytest.UsageError("Another plugin changed the full CI shard inventory")
        selected, deselected = [], []
        for item in items:
            (selected if owner(full[item.nodeid], self.owners) == self.shard
             else deselected).append(item)
        metadata = {
            "shard": self.shard, "shard_count": len(SHARDS),
            "allocation_sha256": self.identity,
            "full_inventory": dict(sorted(full.items())),
            "selected_inventory": dict(sorted(inventory(selected, self.config.rootpath).items())),
            "deselected_items": len(deselected),
        }
        self.config.stash[PARTITION] = metadata
        if deselected:
            self.config.hook.pytest_deselected(items=deselected)
        items[:] = selected
        return result

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_collection(self, session):
        result = yield
        metadata = self.config.stash.get(PARTITION, None)
        if metadata is not None and inventory(session.items, self.config.rootpath) != (
            metadata["selected_inventory"]
        ):
            raise pytest.UsageError("Another plugin changed the final CI shard inventory")
        return result
