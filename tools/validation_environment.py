"""Admit one checkout-bound environment before any validation setup or tests."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import unquote, urlparse


class EnvironmentRefused(ValueError):
    pass


GIT_NAMESPACE_VARIABLES = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_SUPER_PREFIX", "GIT_IMPLICIT_WORK_TREE", "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE", "GIT_REPLACE_REF_BASE",
})


def git_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in GIT_NAMESPACE_VARIABLES}


def python_supported(version: list[int], requirement: str) -> bool:
    """Check the comparison forms used by this project's Python requirement."""
    for clause in requirement.split(","):
        match = re.fullmatch(r"\s*(>=|<|==)\s*(\d+(?:\.\d+){0,2})(\.\*)?\s*", clause)
        if match is None:
            raise EnvironmentRefused("unsupported project requires-python expression")
        operator, number, wildcard = match.groups()
        expected = tuple(int(part) for part in number.split("."))
        if wildcard:
            if operator != "==" or tuple(version[:len(expected)]) != expected:
                return False
        else:
            expected += (0,) * (3 - len(expected))
            actual = tuple(version)
            if not {">=": actual >= expected, "<": actual < expected, "==": actual == expected}[operator]:
                return False
    return True


def _owned(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise EnvironmentRefused(f"unsafe validation environment path: {path}")
    return info


class ValidationEnvironment:
    """Admit one owned environment and one exact CPython qualification runtime.

    The checkout's single X.Y.Z `.python-version` is mandatory. An explicit
    runtime overrides test selection, not the package support range or the pin
    used to report CI patch parity. Construction probes existing environments
    and prints a receipt; it never installs packages or replaces an environment.
    Invalid pins, changed selections and mismatched candidates raise
    EnvironmentRefused before setup or tests. The bootstrap Python may differ.
    """

    def __init__(self, root: Path, selected: Path | None = None, runtime: str | None = None):
        self.root = root.resolve(strict=True)
        _owned(self.root, directory=True)
        if os.environ.get("PYTEST_ADDOPTS"):
            raise EnvironmentRefused("ambient-pytest-addopts; unset PYTEST_ADDOPTS")
        self.pin_path = self.root / ".python-version"
        try:
            self.pin_text = self.pin_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise EnvironmentRefused("validation requires a readable checkout .python-version pin") from exc
        self.declared_pin = self._exact_runtime(self.pin_text.strip(), ".python-version")
        self.runtime = self._exact_runtime(runtime, "--validation-runtime") if runtime is not None else self.declared_pin
        self.runtime_source = "explicit-override" if runtime is not None else ".python-version"
        self.explicit = selected is not None
        self.path = Path(os.path.abspath(selected)) if selected is not None else self.root / ".venv"
        if self.path != self.path.resolve():
            raise EnvironmentRefused("validation environment aliases and symlinks are unsupported")
        self.new_local = not self.path.exists()
        if self.new_local and self.explicit:
            raise EnvironmentRefused("explicit validation environment must already exist")
        if self.explicit and self.path != Path(sys.prefix).resolve():
            raise EnvironmentRefused("explicit validation environment must match the running Python prefix")
        self.identity = None
        self.proof = self.verify(allow_absent=not self.explicit)
        self.record("prewrite")

    def child_environment(self) -> dict[str, str]:
        """Pin checkout, environment and CPython request after removing ambient selectors.

        Config discovery stays disabled, including uv's `.python-version`
        discovery. Only the admitted request is reintroduced as UV_PYTHON.
        Offline, Python-download and cache settings retain their operator values.
        """
        env = git_environment()
        operational = {key: env[key] for key in ("UV_OFFLINE", "UV_PYTHON_DOWNLOADS", "UV_CACHE_DIR")
                       if key in env}
        for key in list(env):
            if key.startswith(("UV_", "PYTHON", "PYTEST_", "CONDA_")) or key in {
                "VIRTUAL_ENV", "__PYVENV_LAUNCHER__", "GRAPHIFY_VALIDATION_ENVIRONMENT",
                "MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKEFILES", "GNUMAKEFLAGS",
                "VALIDATION_ENVIRONMENT", "VALIDATION_RUNTIME",
            }:
                env.pop(key, None)
        env.update(operational)
        env.update(
            UV_WORKING_DIR=str(self.root), UV_PROJECT=str(self.root),
            UV_PROJECT_ENVIRONMENT=str(self.path), UV_NO_CONFIG="1", UV_NO_ENV_FILE="1",
            UV_NO_SYNC="1", PYTHONNOUSERSITE="1",
            UV_PYTHON="cpython@" + self.runtime,
        )
        return env

    @staticmethod
    def _exact_runtime(value: str, source: str) -> str:
        if re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value) is None:
            raise EnvironmentRefused(f"{source} must select one exact CPython version X.Y.Z")
        return value

    def verify(self, *, allow_absent: bool = False) -> dict:
        """Probe current ownership, runtime and checkout binding without setup writes.

        Only a not-yet-created checkout-local `.venv` may be absent when
        allow_absent is true. Existing candidates must match the exact requested
        final CPython runtime and the package support range. Refusals never
        authorize uv to rebuild or replace the candidate.
        """
        if self.pin_path.read_text(encoding="utf-8") != self.pin_text:
            raise EnvironmentRefused("declared Python pin changed during validation; restart validation")
        if not self.path.exists():
            if self.path.is_symlink() or not allow_absent or self.path != self.root / ".venv":
                raise EnvironmentRefused("validation environment disappeared or is an alias")
            parent = _owned(self.root, directory=True)
            return {"path": str(self.path), "state": "new-local", "owner": os.getuid(),
                    "parent_device": parent.st_dev, "parent_inode": parent.st_ino}
        info = _owned(self.path, directory=True)
        identity = (info.st_dev, info.st_ino, info.st_uid)
        if self.identity is not None and identity != self.identity:
            raise EnvironmentRefused("validation environment identity changed")
        _owned(self.path / "pyvenv.cfg", directory=False)
        _owned(self.path / "bin", directory=True)
        probe = subprocess.run(
            [str(self.path / "bin/python"), "-I", "-S", "-B", "-c",
             "import json,sys,sysconfig,tomllib; "
             "project=tomllib.load(open(sys.argv[1],'rb'))['project']; "
             "print(json.dumps({'prefix':sys.prefix,'version':list(sys.version_info[:3]),"
             "'implementation':sys.implementation.name,'releaselevel':sys.version_info.releaselevel,"
             "'purelib':sysconfig.get_path('purelib'),'requires_python':project['requires-python']}))",
             str(self.root / "pyproject.toml")],
            cwd=self.root, env=self.child_environment(), capture_output=True, text=True, check=True,
        )
        actual = json.loads(probe.stdout)
        if Path(actual["prefix"]).resolve() != self.path:
            raise EnvironmentRefused("isolated Python prefix does not match admitted environment")
        version = ".".join(map(str, actual["version"]))
        if (version != self.runtime or actual["implementation"] != "cpython"
                or actual["releaselevel"] != "final"):
            self.record("runtime-refused", {"path": str(self.path), "python_version": actual["version"],
                                           "python_implementation": actual["implementation"],
                                           "python_releaselevel": actual["releaselevel"]})
            raise EnvironmentRefused(
                f"candidate {actual['implementation']} {version} ({actual['releaselevel']}) "
                f"does not match requested validation runtime {self.runtime}; "
                "use a matching environment or deliberately select VALIDATION_RUNTIME=X.Y.Z "
                "(--validation-runtime); no environment was replaced"
            )
        if not python_supported(actual["version"], actual["requires_python"]):
            raise EnvironmentRefused("candidate Python does not satisfy project requires-python")
        site = Path(actual["purelib"])
        if site != site.resolve() or not site.is_relative_to(self.path) or site.name != "site-packages":
            raise EnvironmentRefused("candidate site-packages is outside admitted environment")
        current = self.path
        for part in site.relative_to(self.path).parts:
            current /= part
            _owned(current, directory=True)
        matches = [dist for dist in importlib.metadata.distributions(path=[str(site)])
                   if dist.metadata.get("Name", "").lower().replace("_", "-") == "graphifyy"]
        if len(matches) != 1:
            raise EnvironmentRefused("validation environment needs one checkout-bound editable graphifyy")
        _owned(Path(matches[0]._path), directory=True)
        _owned(Path(matches[0]._path) / "METADATA", directory=False)
        direct = matches[0].locate_file(matches[0]._path.name + "/direct_url.json")
        _owned(Path(direct), directory=False)
        value = json.loads(Path(direct).read_text())
        url = urlparse(value.get("url", ""))
        origin = Path(unquote(url.path))
        if (url.scheme != "file" or url.netloc or url.query or url.fragment
                or not value.get("dir_info", {}).get("editable")
                or origin != self.root or origin.resolve() != self.root):
            raise EnvironmentRefused("validation environment editable origin belongs to another checkout")
        normal = subprocess.run(
            [str(self.path / "bin/python"), "-I", "-B", "-c",
             "import importlib.util,json,sys; spec=importlib.util.find_spec('graphify'); "
             "print(json.dumps({'prefix':sys.prefix,'origin':spec.origin if spec else None}))"],
            cwd=self.root, env=self.child_environment(), capture_output=True, text=True, check=True,
        )
        selected = json.loads(normal.stdout)
        expected = self.root / "graphify/__init__.py"
        if (Path(selected["prefix"]).resolve() != self.path or selected["origin"] is None
                or Path(selected["origin"]) != expected
                or Path(selected["origin"]).resolve() != expected):
            raise EnvironmentRefused("normal Python package origin belongs to another checkout")
        self.identity = identity
        return {"path": str(self.path), "state": "existing-local" if not self.explicit else "explicit-external",
                "uid": info.st_uid, "device": info.st_dev, "inode": info.st_ino,
                "isolated_prefix": actual["prefix"], "normal_prefix": selected["prefix"],
                "python_version": actual["version"], "requires_python": actual["requires_python"],
                "python_implementation": actual["implementation"],
                "python_releaselevel": actual["releaselevel"],
                "site_packages": str(site),
                "actual_package_origin": selected["origin"], "editable_origin": str(origin)}

    def record(self, stage: str, proof: dict | None = None) -> None:
        """Print versioned observations; unknown candidate parity is null before setup.

        CI patch parity compares the observed final CPython version to this
        checkout's declared pin. It does not claim hosted CI passage or parity
        of dependencies, operating systems or native builds. Existing receipt
        fields remain available to retained-evidence consumers.
        """
        observed = self.proof if proof is None else proof
        actual = observed.get("python_version")
        final_cpython = (observed.get("python_implementation") == "cpython"
                         and observed.get("python_releaselevel") == "final")
        version = ".".join(map(str, actual)) if actual is not None else None
        print("validation_environment_receipt=" + json.dumps(
            {"version": 2, "stage": stage, **observed,
             "bootstrap_python": sys.executable, "bootstrap_python_version": list(sys.version_info[:3]),
             "bootstrap_implementation": sys.implementation.name,
             "declared_python_pin": self.declared_pin, "requested_python": self.runtime,
             "runtime_selection_source": self.runtime_source,
             "requested_pin_satisfied": final_cpython and version == self.runtime if actual is not None else None,
             "ci_patch_parity": final_cpython and version == self.declared_pin if actual is not None else None},
            sort_keys=True), flush=True)

    def before_tests(self) -> None:
        """Recheck the selected environment and pin, then emit the pre-test receipt."""
        self.proof = self.verify()
        self.record("postsetup-before-test")


def check_uv_argv(argv: tuple[str, ...]) -> None:
    if argv[0] != "uv":
        return
    valid = argv in {
        ("uv", "sync", "--all-extras", "--frozen"),
        ("uv", "sync", "--frozen", "--inexact"),
    } or argv[:6] == ("uv", "run", "--frozen", "python", "-m", "pytest")
    routing = {"--directory", "--project", "--env-file", "--config-file", "--python",
               "--active", "--isolated", "--no-project", "--package", "--with", "--with-editable"}
    if not valid or any(arg.split("=", 1)[0] in routing for arg in argv[1:]):
        raise EnvironmentRefused("unsupported inline uv routing arguments")
