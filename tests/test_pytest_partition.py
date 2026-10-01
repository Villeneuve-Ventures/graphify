"""Subprocess proofs of full discovery, module ownership and shard diagnostics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.ci_shard_allocation import ALLOCATION_PATH, balance
from tools.pytest_partition import allocation, owner

ROOT = Path(__file__).resolve().parents[1]


def run(root, *args, shard=None, timing=True, plugins=()):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(key, None)
    command = [sys.executable, "-m", "pytest", "-p", "tools.pytest_partition"]
    if timing:
        command += ["-p", "tools.pytest_timings", "--ci-timing-json", str(root / "timings.json")]
    for plugin in plugins:
        command += ["-p", plugin]
    if shard is not None:
        command += [f"--ci-shard={shard}"]
    return subprocess.run([*command, "tests/", "-q", "--tb=short", *args],
                          cwd=root, env=env, text=True, capture_output=True, timeout=30)


def payload(root):
    return json.loads((root / "timings.json").read_text())


@pytest.fixture
def suite(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (tmp_path / "tests/nested").mkdir(parents=True)
    # Include previously unmeasured modules on each owner, plus nested discoveries.
    paths = []
    for shard in (1, 2, 3, 4):
        path = next(f"tests/test_new_{n}.py" for n in range(100)
                    if owner(f"tests/test_new_{n}.py", {}) == shard)
        paths.append(path)
    paths += ["tests/nested/test_nested.py", "tests/test_workspace_structural_s4.py"]
    (tmp_path / "tests/helper.py").write_text("ANSWER = 42\n")
    (tmp_path / "tests/__init__.py").write_text("")
    (tmp_path / "conftest.py").write_text(
        "import pytest\n@pytest.hookimpl(wrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    report = yield\n"
        "    report.duration = {'setup': 1.25, 'call': 2.5, 'teardown': 3.75}[report.when]\n"
        "    return report\n"
    )
    for path in paths:
        (tmp_path / path).write_text(
            "from tests.helper import ANSWER\nimport pytest\n"
            "@pytest.mark.parametrize('n', [1, 2])\n"
            "def test_import(n): assert ANSWER == 42\n"
            "@pytest.mark.skip(reason='fixture skip')\ndef test_skip(): pass\n"
        )
    return tmp_path


def nodes(result):
    return [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]


def test_shards_exact_union_disjoint_whole_module_and_deterministic(suite):
    ordinary = run(suite, "--collect-only", timing=False)
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    full = nodes(ordinary)
    sets = []
    owners, identity = allocation()
    for shard in (1, 2, 3, 4):
        observed = run(suite, "--collect-only", shard=shard)
        assert observed.returncode == 0, observed.stdout + observed.stderr
        selected = nodes(observed)
        assert len(selected) == len(set(selected))
        assert selected == nodes(run(suite, "--collect-only", shard=shard, timing=False))
        info = payload(suite)
        assert set(info["full_inventory"]) == set(full)
        assert set(info["selected_inventory"]) == set(selected)
        assert info["full_items"] == len(full) == info["selected_items"] + info["deselected_items"]
        assert info["partition"] == {"shard": shard, "shard_count": 4,
                                     "allocation_sha256": identity, "selection_validated": True}
        assert "deselected-tests" not in info["incomplete_reasons"]
        assert all(owner(path, owners) == shard for path in info["selected_inventory"].values())
        assert not any(set(selected) & earlier for earlier in sets)
        sets.append(set(selected))
        # The same modules execute once, with all outcomes and phase attribution intact.
        executed = run(suite, shard=shard)
        assert executed.returncode == 0, executed.stdout + executed.stderr
        measured = payload(suite)
        assert measured["status"] == "complete"
        assert measured["selected_items"] == measured["executed_items"] == measured["completed_items"]
        for module in measured["modules"]:
            assert module["selected_items"] == 3
            assert module["setup_seconds"] == 3.75
            assert module["call_seconds"] == 5.0
            assert module["teardown_seconds"] == 11.25
            assert module["total_seconds"] == 20.0
    assert set.union(*sets) == set(full)
    for path in {node.split("::")[0] for node in full}:
        module_nodes = {node for node in full if node.split("::")[0] == path}
        assert sum(module_nodes <= shard for shard in sets) == 1


@pytest.mark.parametrize("shard", [1, 2, 3, 4])
def test_collection_errors_from_other_shards_remain_fatal(suite, shard):
    path = next(f"tests/test_broken_{n}.py" for n in range(100)
                if owner(f"tests/test_broken_{n}.py", {}) != shard)
    (suite / path).write_text("def broken(\n")
    result = run(suite, shard=shard)
    assert result.returncode == 2
    assert "SyntaxError" in result.stdout and path in result.stdout
    info = payload(suite)
    assert info["collection_errors"] == 1 and info["executed_items"] == 0
    assert info["status"] == "incomplete"


@pytest.mark.parametrize("args", [
    ("-k", "import"), ("-m", "slow"), ("--lf",), ("--ff",), ("--nf",),
    ("--sw",), ("--sw-skip",), ("--sw-reset",), ("--ignore=tests/nested",),
    ("--ignore-glob=*nested*",), ("--deselect=tests/test_new_0.py",),
    ("--pyargs",), ("--keep-duplicates",), ("--noconftest",),
    ("--confcutdir=tests",), ("--continue-on-collection-errors",),
    ("-c", "pytest.ini"), ("--collect-in-virtualenv",), ("--doctest-modules",),
    ("-o", "python_files=test_new_0.py"), ("tests/nested",), (".",),
])
def test_incompatible_selection_is_rejected(suite, args):
    result = run(suite, *args, shard=1)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "CI shards" in result.stderr


@pytest.mark.parametrize("args", [("-n", "2"), ("--dist=load",)])
def test_distributed_execution_is_rejected(suite, args):
    result = run(suite, *args, shard=1, plugins=("xdist.plugin",))
    assert result.returncode == 4
    assert "CI shards" in result.stderr


@pytest.mark.parametrize("shard", [0, 5, "garbage"])
def test_invalid_shard_is_rejected(suite, shard):
    assert run(suite, shard=shard).returncode == 4


@pytest.mark.parametrize("hook", ["pytest_collection_modifyitems", "pytest_collection_finish"])
def test_external_deselection_cannot_masquerade_as_valid_selection(suite, hook):
    body = ("def pytest_collection_modifyitems(config, items):\n"
            "    config.hook.pytest_deselected(items=items[:1])\n    del items[:1]\n")
    if hook.endswith("finish"):
        body = "def pytest_collection_finish(session): session.items.clear()\n"
    with (suite / "conftest.py").open("a") as file:
        file.write(body)
    result = run(suite, shard=1)
    assert result.returncode == 4
    assert "Another plugin changed" in result.stderr
    assert payload(suite)["status"] == "incomplete"


def test_early_exit_is_not_complete_shard_telemetry(suite):
    owners = allocation()[0]
    path = next(path for path in (suite / "tests").glob("test_*.py")
                if owner(path.relative_to(suite).as_posix(), owners) == 1)
    path.write_text("import pytest\ndef test_exit(): pytest.exit('early', returncode=0)\n")
    result = run(suite, shard=1)
    assert result.returncode == 0
    assert payload(suite)["status"] == "incomplete"
    assert "execution-incomplete" in payload(suite)["incomplete_reasons"]


def test_committed_balance_and_fallback_recipe():
    data = json.loads(ALLOCATION_PATH.read_text())
    weights = {path: value["microseconds"] for path, value in data["modules"].items()}
    owners, loads = balance(weights)
    assert len(owners) == 229
    assert owners == allocation()[0]
    assert loads == {int(key): value for key, value in data["predicted_microseconds"].items()}
    assert sum(loads.values()) == 2599339259
    assert max(loads.values()) - min(loads.values()) == 252
    assert balance({"tests/b.py": 10, "tests/a.py": 10})[0] == {"tests/a.py": 1, "tests/b.py": 2}
    import hashlib
    path = "tests/nested/test_future.py"
    assert owner(path, owners) == 1 + int.from_bytes(hashlib.sha256(path.encode()).digest()[:4], "big") % 4


def test_empty_shard_preserves_pytests_no_tests_exit(suite):
    for path in (suite / "tests").rglob("test_*.py"):
        path.unlink()
    path = "tests/test_workspace_structural_s4.py"
    (suite / path).write_text("def test_only(): pass\n")
    shard = next(shard for shard in (1, 2, 3, 4) if shard != owner(path, allocation()[0]))
    result = run(suite, shard=shard)
    assert result.returncode == 5
    assert payload(suite)["selected_items"] == 0
    assert payload(suite)["full_items"] == 1
    assert payload(suite)["status"] == "incomplete"


@pytest.mark.parametrize("mutation", ["broken-json", "modules-list", "negative-weight", "bad-owner", "bad-load"])
def test_invalid_allocation_configuration_is_usage_error(suite, mutation):
    data = json.loads(ALLOCATION_PATH.read_text())
    path = next(iter(data["modules"]))
    if mutation == "modules-list":
        data["modules"] = []
    elif mutation == "negative-weight":
        data["modules"][path]["microseconds"] = -1
    elif mutation == "bad-owner":
        data["modules"][path]["shard"] = 5
    elif mutation == "bad-load":
        data["predicted_microseconds"]["1"] = 0
    candidate = suite / "allocation.json"
    candidate.write_text("{" if mutation == "broken-json" else json.dumps(data))
    (suite / "conftest.py").write_text(
        "import pytest\nfrom pathlib import Path\nimport tools.pytest_partition as partition\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_configure():\n"
        f"    partition.ALLOCATION_PATH = Path({str(candidate)!r})\n"
    )
    result = run(suite, shard=1)
    assert result.returncode == 4
    assert "Invalid CI shard allocation" in result.stderr


def test_shards_require_repository_root_cwd(suite):
    result = run(suite / "tests", shard=1, timing=False)
    assert result.returncode == 4
    assert "repository root" in result.stderr
