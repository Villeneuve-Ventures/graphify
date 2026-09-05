"""Frozen 80-case matrix for the protected change verifier."""

from __future__ import annotations

import ast
import base64
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import graphify.protected_change_verifier as verifier


_OID = bytes.fromhex("11" * 20)
_OTHER_OID = bytes.fromhex("22" * 20)
_EXPECTED_SCHEMA_SHA256 = (
    "59666f5cf42b4a8e37c0275194f955124a65a3502090502ec8d557e8c5e24c8d"
)


def _expect(condition: bool) -> None:
    if not condition:
        raise AssertionError("optimization-safe test oracle failed")


_CASE_NAMES = (
    "real-v2",
    "real-v3",
    "empty-index",
    "multi-entry-order",
    "space-path",
    "tab-path",
    "newline-path",
    "unicode-path",
    "backslash-path",
    "4094-byte-name",
    "4095-byte-name",
    "saturated-long-name",
    "mode-100644",
    "mode-100755",
    "mode-120000",
    "optional-extension",
    "duplicate-optional-extensions",
    "all-ff-oid",
    "canonical-json-base64-vector",
    "embedded-schema",
    "parser-source-provenance",
    "nested-manifest-digests",
    "acquisition-metadata",
    "canonical-success-json",
    "optimized-success-equivalence",
    "hostile-ambient-config",
    "source-immutability",
    "swap-restore-same-buffer",
    "final-symlink",
    "parent-symlink",
    "non-regular-index",
    "index-size-cap",
    "read-shrink",
    "read-growth",
    "bad-signature",
    "dirc-v1",
    "dirc-v4",
    "unsupported-version",
    "entry-count-bound",
    "impossible-entry-count",
    "checksum-mismatch",
    "missing-checksum",
    "trailing-framing-bytes",
    "short-entry-prefix",
    "missing-path-terminator",
    "empty-path",
    "invalid-utf8-path",
    "absolute-path",
    "trailing-slash",
    "empty-component",
    "dot-component",
    "dot-dot-component",
    "git-component",
    "path-byte-limit",
    "name-length-mismatch",
    "duplicate-and-prefix-conflict",
    "unsorted-path",
    "nonzero-padding",
    "assume-valid-flag",
    "extended-flag",
    "nonzero-stage",
    "gitlink-mode",
    "unsupported-mode",
    "all-zero-oid",
    "extension-count-limit",
    "extension-payload-limit",
    "extension-aggregate-limit",
    "malformed-extension-header",
    "truncated-extension-payload",
    "fsmonitor-extension",
    "lowercase-required-extension",
    "split-index-extension",
    "sparse-index-extension",
    "reconstruction-stream-limit",
    "success-manifest-limit",
    "fabricated-snapshot",
    "snapshot-digest-corruption",
    "optimized-rejection-equivalence",
    "hostile-sentinel-redaction",
    "canonical-error-json",
)


def _git(
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
    index_path: Path | None = None,
) -> bytes:
    env = os.environ.copy()
    if index_path is not None:
        env["GIT_INDEX_FILE"] = str(index_path)
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        input=input_bytes,
        env=env,
    ).stdout


def _record(mode: bytes, oid: bytes, path: bytes) -> bytes:
    return mode + b" " + oid + b" 0\t" + path + b"\0"


def _real_index(tmp_path: Path) -> tuple[Path, Path, bytes, bytes]:
    repo = tmp_path / "source"
    _git(tmp_path, "init", "-q", str(repo))
    oid = _git(repo, "hash-object", "-w", "--stdin", input_bytes=b"alpha").strip()
    record = _record(b"100644", oid, b"tracked.txt")
    _git(repo, "update-index", "-z", "--index-info", input_bytes=record)
    index_path = repo / ".git" / "index"
    return repo, index_path, index_path.read_bytes(), record


def _swappable_indexes(
    tmp_path: Path,
) -> tuple[Path, Path, bytes, bytes, bytes, bytes]:
    repo, index_path, index_a, record_a = _real_index(tmp_path)
    oid_b = _git(repo, "hash-object", "-w", "--stdin", input_bytes=b"beta").strip()
    record_b = _record(b"100755", oid_b, b"tracked.txt")
    index_b_path = tmp_path / "index-b"
    _git(repo, "read-tree", "--empty", index_path=index_b_path)
    _git(
        repo,
        "update-index",
        "-z",
        "--index-info",
        input_bytes=record_b,
        index_path=index_b_path,
    )
    return repo, index_path, index_a, index_b_path.read_bytes(), record_a, record_b


def _entry(
    path: bytes,
    *,
    mode: int = 0o100644,
    oid: bytes = _OID,
    flags: int | None = None,
    padding_byte: int = 0,
) -> bytes:
    if flags is None:
        flags = min(len(path), 0x0FFF)
    prefix = struct.pack(">10I20sH", 0, 0, 0, 0, 0, 0, mode, 0, 0, 0, oid, flags)
    entry_size = 62 + len(path) + 1
    padding = bytes([padding_byte]) * ((-entry_size) % 8)
    return prefix + path + b"\0" + padding


def _extension(signature: bytes, payload: bytes) -> bytes:
    return signature + len(payload).to_bytes(4, "big") + payload


def _wrap(body: bytes, *, version: int = 2, count: int = 0) -> bytes:
    content = b"DIRC" + struct.pack(">II", version, count) + body
    return content + hashlib.sha1(content).digest()


def _index(
    entries: list[bytes] | tuple[bytes, ...] = (),
    *,
    version: int = 2,
    extensions: list[bytes] | tuple[bytes, ...] = (),
) -> bytes:
    return _wrap(b"".join(entries) + b"".join(extensions), version=version, count=len(entries))


def _rehash(raw: bytes) -> bytes:
    return raw[:-20] + hashlib.sha1(raw[:-20]).digest()


def _write_index(tmp_path: Path, raw: bytes, name: str = "index") -> Path:
    path = tmp_path / name
    path.write_bytes(raw)
    return path


def _rejects(raw: bytes, invariant: str, *, limits: verifier.Limits = verifier.LIMITS) -> None:
    with pytest.raises(verifier.VerificationError) as raised:
        verifier._parse_index_bytes(raw, limits)
    _expect(raised.value.invariant == invariant)


def _cli(path: Path, *, optimized: bool = False, env: dict[str, str] | None = None):
    if env is None:
        effective_env = os.environ.copy()
        effective_env.pop("PYTHONOPTIMIZE", None)
    else:
        effective_env = dict(env)
    command = [sys.executable]
    if optimized:
        command.append("-O")
    command.extend(["-m", "graphify.protected_change_verifier", "--index", str(path)])
    return subprocess.run(command, check=False, capture_output=True, env=effective_env)


def _assert_path_accepted(path: bytes) -> verifier.ParsedIndex:
    parsed = verifier._parse_index_bytes(_index([_entry(path)]))
    _expect(parsed.entries[0].raw_path == path)
    return parsed


def _documented_repository_verifier() -> tuple[str, str]:
    docs = (
        Path(__file__).parents[1] / "docs" / "protected-change-review.md"
    ).read_text(encoding="utf-8")
    section = docs.split("### Repository verifier reference", 1)[1]
    match = re.search(r"```sh\n(.*?)\n```", section, flags=re.DOTALL)
    if match is None:
        raise AssertionError("documented repository verifier block is missing")
    return docs, match.group(1)


def _launcher_with_assignments(snippet: str, assignments: dict[str, Path]) -> str:
    for name, value in assignments.items():
        snippet, count = re.subn(
            rf"(?m)^{re.escape(name)}=.*$",
            lambda _match, replacement=f"{name}={shlex.quote(str(value))}": replacement,
            snippet,
        )
        _expect(count == 1)
    return snippet


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _assert_git_admin_path_cases(tmp_path: Path, cases: list[tuple[bytes, bool]]) -> None:
    _require_supported_git_version()
    repo, _index_path, _raw, _record_bytes = _real_index(tmp_path)
    oid = _git(repo, "hash-object", "-w", "--stdin", input_bytes=b"target").strip()
    for number, (path, rejected) in enumerate(cases):
        for mode in (0o100644, 0o100755, 0o120000):
            records = _record(f"{mode:06o}".encode(), oid, path)
            destination = tmp_path / f"admin-roundtrip-{number}-{mode}"
            _git(repo, "-c", "core.protectHFS=true", "-c", "core.protectNTFS=true",
                 "update-index", "-z", "--index-info", input_bytes=records,
                 index_path=destination)
            actual = _git(repo, "ls-files", "--stage", "-z", index_path=destination)
            _expect(actual == (b"" if rejected else records))
            raw = _index([_entry(path, mode=mode, oid=bytes.fromhex(oid.decode()))])
            if rejected:
                _rejects(raw, "entry.path.shape")
            else:
                _expect(verifier._parse_index_bytes(raw).reconstruction_records == records)


def _require_supported_git_version() -> None:
    version = subprocess.run(["git", "--version"], check=True, capture_output=True)
    if version.stdout != b"git version 2.55.0\n" or version.stderr:
        raise RuntimeError("real-Git oracle requires exactly Git 2.55.0")


@pytest.mark.parametrize(
    "case_number",
    range(1, 81),
    ids=[f"case-{number:03d}-{name}" for number, name in enumerate(_CASE_NAMES, 1)],
)
def test_frozen_normative_case(
    case_number: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Each collected item is exactly one numbered logical regression."""
    if case_number == 1:
        repo, index_path, _raw, record = _real_index(tmp_path)
        snapshot = verifier.verify_index(index_path)
        _expect(snapshot.result()["source"]["index_version"] == 2)
        _expect(verifier.build_reconstruction_stream(snapshot) == record)
        _expect(_git(repo, "ls-files", "--stage", "-z") == record)
    elif case_number == 2:
        repo, _index_path, raw, record = _real_index(tmp_path)
        version_three = _rehash(raw[:4] + (3).to_bytes(4, "big") + raw[8:])
        v3_path = _write_index(tmp_path, version_three, "index-v3")
        parsed = verifier.verify_index(v3_path)
        _expect(parsed.result()["source"]["index_version"] == 3)
        _expect(_git(repo, "ls-files", "--stage", "-z", index_path=v3_path) == record)
    elif case_number == 3:
        repo = tmp_path / "empty"
        _git(tmp_path, "init", "-q", str(repo))
        _git(repo, "read-tree", "--empty")
        snapshot = verifier.verify_index(repo / ".git" / "index")
        _expect(snapshot.reconstruction_records == b"")
        _expect(snapshot.result()["source"]["entry_count"] == 0)
    elif case_number == 4:
        parsed = verifier._parse_index_bytes(
            _index([_entry(b"a", oid=_OID), _entry(b"b", oid=_OTHER_OID)])
        )
        _expect(parsed.reconstruction_records == (
            _record(b"100644", _OID.hex().encode(), b"a")
            + _record(b"100644", _OTHER_OID.hex().encode(), b"b")
        ))
    elif case_number == 5:
        _assert_path_accepted(b"space path")
    elif case_number == 6:
        _assert_path_accepted(b"tab\tpath")
    elif case_number == 7:
        _assert_path_accepted(b"line\nbreak")
    elif case_number == 8:
        _assert_path_accepted("café/雪".encode())
    elif case_number == 9:
        _assert_path_accepted(b"back\\slash")
        # NTFS recognizes only its aliases after backslashes; HFS does not
        # reinterpret backslashes as separators on the supported platform.
        _assert_git_admin_path_cases(tmp_path, [(path, False) for path in (
            rb"back\slash", rb"safe\.gitx\config", rb"safe\git~2\config",
            rb"safe\.git~1\config", rb"safe\git~1x/config", rb"safe\ordinary/file",
            "safe\\.g\u200cit/config".encode(),
            "safe/.g\u200cit\\config".encode(),
        )])
    elif case_number == 10:
        _assert_path_accepted(b"a" * 4094)
    elif case_number == 11:
        _assert_path_accepted(b"a" * 4095)
    elif case_number == 12:
        _assert_path_accepted(b"a" * 4096)
    elif case_number == 13:
        _expect(_assert_path_accepted(b"a").entries[0].mode == "100644")
    elif case_number == 14:
        parsed = verifier._parse_index_bytes(_index([_entry(b"a", mode=0o100755)]))
        _expect(parsed.entries[0].mode == "100755")
    elif case_number == 15:
        parsed = verifier._parse_index_bytes(_index([_entry(b"a", mode=0o120000)]))
        _expect(parsed.entries[0].mode == "120000")
    elif case_number == 16:
        parsed = verifier._parse_index_bytes(
            _index(extensions=[_extension(b"A\x00\x1f\x7f", b"payload")])
        )
        _expect([item.signature for item in parsed.extensions] == ["A\x00\x1f\x7f"])
        acquisition = verifier.Acquisition(1, 0, 0, 0, stat.S_IFREG, 0, len(_index()))
        result = json.loads(verifier._build_result(parsed, acquisition, "0" * 64))
        _expect(result["source"]["extensions"][0]["signature"] == "A\x00\x1f\x7f")
        _rejects(
            _index(extensions=[_extension(b"A\x00\x1f\x80", b"")]),
            "extension.frame",
        )
    elif case_number == 17:
        parsed = verifier._parse_index_bytes(
            _index(extensions=[_extension(b"TEST", b"a"), _extension(b"TEST", b"b")])
        )
        _expect([item.signature for item in parsed.extensions] == ["TEST", "TEST"])
    elif case_number == 18:
        parsed = verifier._parse_index_bytes(_index([_entry(b"a", oid=b"\xff" * 20)]))
        _expect(parsed.entries[0].oid == b"\xff" * 20)
    elif case_number == 19:
        _expect(base64.b64encode(b"\xfb\x00") == b"+wA=")
        vector = {"path": "a/é", "status_porcelain_v2_z_base64": "+wA="}
        _expect(hashlib.sha256(verifier.canonical_json(vector)).hexdigest() == (
            "2abd1782cb53952044e4239efb441e7f2c17c487ce1a9376aa433845070bf4cd"
        ))
    elif case_number == 20:
        _expect(hashlib.sha256(verifier.SCHEMA_BYTES).hexdigest() == verifier.SCHEMA_SHA256)
        _expect(verifier.SCHEMA_BYTES == verifier.canonical_json(verifier.SCHEMA_DESCRIPTOR))
        _expect(len(verifier.INVARIANT_IDS) == 29)
        _expect("attestation.invalid" in verifier.INVARIANT_IDS)
        docs, launcher = _documented_repository_verifier()
        _expect(verifier.SCHEMA_SHA256 == _EXPECTED_SCHEMA_SHA256)
        _expect(_EXPECTED_SCHEMA_SHA256 in docs)
        _expect("rev-parse --path-format=absolute" in launcher)
        _expect("--git-path index" in launcher)
        _expect('exec "$env_path" -i' in launcher)
        _expect(launcher.count('"$env_path" -i') == 2)
        _expect("env -i" not in launcher)
        _expect("cd --" not in launcher)
        _expect(')" || exit' in launcher)
        _expect('[ -n "$index_path" ] || exit' in launcher)
        _expect(
            '"$verifier_python" -I -S -B "$verifier_module"'
            in launcher
        )
        ci = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
        _expect("https://www.kernel.org/pub/software/scm/git/git-2.55.0.tar.xz" in ci)
        _expect("457fdb04dc8728e007d4688695e6912e6f680727920f2a40bf11eacc17505357" in ci)
        _expect(
            "run: uv run --frozen python -O -m pytest "
            "tests/test_protected_change_verifier.py -q --tb=short" in ci
        )
        _expect("uv run --frozen python -m graphify.protected_change_verifier" not in docs)

        real_env = Path(shutil.which("env") or "")
        _expect(real_env.is_absolute())
        source_root = tmp_path / "source root"
        source_root.mkdir()
        shadow_marker = tmp_path / "shadow-module-ran"
        shadow_package = source_root / "graphify"
        shadow_package.mkdir()
        (shadow_package / "__init__.py").write_bytes(b"")
        (shadow_package / "protected_change_verifier.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(shadow_marker)!r}).write_text('shadow', encoding='utf-8')\n",
            encoding="utf-8",
        )
        empty_config = tmp_path / "empty config"
        empty_config.write_bytes(b"")
        index_path = tmp_path / "resolved index"
        index_path.write_bytes(_index())
        isolated_path = tmp_path / "isolated bin"
        isolated_path.mkdir()
        # Disposable interpreter-global site directory: prove both hooks run
        # under -I alone, then prove the documented -S invocation excludes them.
        runtime = tmp_path / "runtime"
        subprocess.run(
            [sys.executable, "-I", "-S", "-m", "venv", "--without-pip", "--symlinks",
             str(runtime)], check=True, capture_output=True,
        )
        pinned_python = runtime / "bin" / "python"
        site_directory = runtime / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        pth_marker = tmp_path / "pth-ran"
        site_marker = tmp_path / "sitecustomize-ran"
        for filename, marker in (("hostile.pth", pth_marker), ("sitecustomize.py", site_marker)):
            (site_directory / filename).write_text(
                f"import pathlib; p = pathlib.Path({str(marker)!r}); "
                "p.write_bytes(p.read_bytes() + b'x' if p.exists() else b'x')\n",
                encoding="utf-8",
            )
        subprocess.run([str(pinned_python), "-I", "-B", "-c", "pass"], check=True)
        hook_baseline = (pth_marker.read_bytes(), site_marker.read_bytes())
        pinned_module = tmp_path / "pinned-verifier" / "protected_change_verifier.py"
        pinned_module.parent.mkdir()
        pinned_module.write_bytes(Path(verifier.__file__).read_bytes())
        expected_parser_hash = hashlib.sha256(pinned_module.read_bytes()).hexdigest()
        hostile_path = tmp_path / "hostile bin"
        hostile_path.mkdir()
        hostile_env_marker = tmp_path / "ambient-env-ran"
        lookup_marker = tmp_path / "lookup-ran"
        git_arguments_marker = tmp_path / "git-arguments"
        fake_env = hostile_path / "env"
        pinned_git = tmp_path / "pinned git"
        _write_executable(
            fake_env,
            f"printf hostile > {shlex.quote(str(hostile_env_marker))}\nexit 97\n",
        )
        _write_executable(
            pinned_git,
            (
                f"printf lookup > {shlex.quote(str(lookup_marker))}\n"
                f"printf '%s\\n' \"$@\" > {shlex.quote(str(git_arguments_marker))}\n"
                f"printf '%s\\n' {shlex.quote(str(index_path))}\n"
            ),
        )
        assignments = {
            "source_root": source_root,
            "empty_config": empty_config,
            "env_path": real_env,
            "git_path": pinned_git,
            "verifier_python": pinned_python,
            "verifier_module": pinned_module,
            "isolated_path": isolated_path,
        }
        runnable = _launcher_with_assignments(launcher, assignments)
        completed = subprocess.run(
            ["/bin/sh", "-c", runnable],
            check=False,
            capture_output=True,
            cwd=source_root,
            env={"PATH": str(hostile_path)},
        )
        _expect(completed.returncode == 0)
        _expect(completed.stderr == b"")
        _expect(not hostile_env_marker.exists())
        _expect(not shadow_marker.exists())
        _expect((pth_marker.read_bytes(), site_marker.read_bytes()) == hook_baseline)
        _expect(lookup_marker.read_bytes() == b"lookup")
        _expect(git_arguments_marker.read_text(encoding="utf-8").splitlines() == [
            "-C",
            str(source_root),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "index",
        ])
        accepted = json.loads(completed.stdout)
        _expect(accepted["verifier"] == verifier.VERIFIER_VERSION)
        _expect(accepted["source"]["parser_source_sha256"] == expected_parser_hash)
        _expect(accepted["source"]["raw_index_sha256"] == hashlib.sha256(
            index_path.read_bytes()
        ).hexdigest())

        _write_executable(pinned_git, "exit 42\n")
        failed_lookup = subprocess.run(
            ["/bin/sh", "-c", _launcher_with_assignments(launcher, assignments)],
            check=False,
            capture_output=True,
            cwd=source_root,
            env={"PATH": str(hostile_path)},
        )
        _expect(failed_lookup.returncode == 42)
        _expect(failed_lookup.stdout == b"")
        _expect(not hostile_env_marker.exists())
        _expect(not shadow_marker.exists())

        _write_executable(pinned_git, "exit 0\n")
        empty_lookup = subprocess.run(
            ["/bin/sh", "-c", _launcher_with_assignments(launcher, assignments)],
            check=False,
            capture_output=True,
            cwd=source_root,
            env={"PATH": str(hostile_path)},
        )
        _expect(empty_lookup.returncode != 0)
        _expect(empty_lookup.stdout == b"")
        _expect(not hostile_env_marker.exists())
        _expect(not shadow_marker.exists())
    elif case_number == 21:
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        source = verifier.verify_index(index_path).result()["source"]
        _expect(source["parser_source_sha256"] == hashlib.sha256(
            Path(verifier.__file__).read_bytes()
        ).hexdigest())
    elif case_number == 22:
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        result = verifier.verify_index(index_path).result()
        source_bytes = verifier.canonical_json(result["source"])
        candidate_bytes = verifier.canonical_json(result["candidate_index"])
        _expect(result["candidate_index"]["source_manifest_sha256"] == hashlib.sha256(
            source_bytes
        ).hexdigest())
        _expect(result["evidence"]["candidate_index_manifest_sha256"] == hashlib.sha256(
            candidate_bytes
        ).hexdigest())
        _expect(result["evidence"]["source_manifest_sha256"] == hashlib.sha256(
            source_bytes
        ).hexdigest())
    elif case_number == 23:
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        before = index_path.stat()
        acquisition = verifier.verify_index(index_path).result()["source"]["acquisition"]
        _expect(acquisition == {
            "component_count": len(index_path.parts) - 1,
            "ctime_ns": before.st_ctime_ns,
            "device": before.st_dev,
            "inode": before.st_ino,
            "mode": before.st_mode,
            "mtime_ns": before.st_mtime_ns,
            "size": before.st_size,
        })
        for field in (
            "component_count",
            "st_ctime_ns",
            "st_dev",
            "st_ino",
            "st_mode",
            "st_mtime_ns",
            "st_size",
        ):
            for value in (-1, True):
                component_count = 1
                values = {
                    "st_ctime_ns": 1,
                    "st_dev": 1,
                    "st_ino": 1,
                    "st_mode": stat.S_IFREG,
                    "st_mtime_ns": 1,
                    "st_size": 1,
                }
                if field == "component_count":
                    component_count = value
                else:
                    values[field] = value
                with pytest.raises(verifier.VerificationError) as raised:
                    verifier.Acquisition.from_stat(
                        SimpleNamespace(**values),  # type: ignore[arg-type]
                        component_count,
                    )
                _expect(raised.value.invariant == "acquisition.changed")

        negative_time = _write_index(tmp_path, _index(), "negative-time-index")
        try:
            os.utime(negative_time, ns=(-1, -1))
        except (OSError, OverflowError):
            pass
        else:
            if negative_time.stat().st_mtime_ns < 0:
                with pytest.raises(verifier.VerificationError) as raised:
                    verifier.verify_index(negative_time)
                _expect(raised.value.invariant == "acquisition.changed")
                normal = _cli(negative_time)
                optimized = _cli(negative_time, optimized=True)
                _expect(
                    (normal.returncode, normal.stdout, normal.stderr)
                    == (optimized.returncode, optimized.stdout, optimized.stderr)
                )
                _expect(normal.returncode == 1)
                _expect(normal.stderr == b"")
                _expect(
                    json.loads(normal.stdout)["invariant"] == "acquisition.changed"
                )
    elif case_number == 24:
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        snapshot = verifier.verify_index(index_path)
        _expect(snapshot.canonical_result == verifier.canonical_json(snapshot.result()))
        _expect(not snapshot.canonical_result.endswith(b"\n"))
    elif case_number == 25:
        oracle_active = False
        try:
            _expect(False)
        except AssertionError:
            oracle_active = True
        _expect(oracle_active)
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        normal = _cli(index_path)
        optimized = _cli(index_path, optimized=True)
        _expect((normal.returncode, normal.stdout, normal.stderr) == (
            optimized.returncode,
            optimized.stdout,
            optimized.stderr,
        ))
        _expect(normal.returncode == 0)
        hostile_env = os.environ.copy()
        hostile_env["PYTHONOPTIMIZE"] = "2"
        optimization_probe = subprocess.run(
            [sys.executable, "-c", "import sys; print(sys.flags.optimize)"],
            check=False,
            capture_output=True,
            env=hostile_env,
        )
        _expect(optimization_probe.returncode == 0)
        _expect(optimization_probe.stdout == b"2\n")
        _expect(optimization_probe.stderr == b"")
        ambient = _cli(index_path, env=hostile_env)
        ambient_optimized = _cli(index_path, optimized=True, env=hostile_env)
        _expect(
            (normal.returncode, normal.stdout, normal.stderr)
            == (ambient.returncode, ambient.stdout, ambient.stderr)
            == (
                ambient_optimized.returncode,
                ambient_optimized.stdout,
                ambient_optimized.stderr,
            )
        )
    elif case_number == 26:
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        baseline = verifier.verify_index(index_path).canonical_result
        for key, value in {
            "GIT_INDEX_FILE": "/hostile/index",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "true",
            "GIT_OBJECT_DIRECTORY": "/hostile/objects",
            "GIT_WORK_TREE": "/hostile/worktree",
        }.items():
            monkeypatch.setenv(key, value)
        _expect(verifier.verify_index(index_path).canonical_result == baseline)
    elif case_number == 27:
        repo, index_path, raw, _record_bytes = _real_index(tmp_path)
        protected = [index_path, repo / ".git" / "HEAD", repo / ".git" / "config"]
        before = [(path.read_bytes(), path.stat()) for path in protected]
        verifier.verify_index(index_path)
        after = [(path.read_bytes(), path.stat()) for path in protected]
        _expect(raw == index_path.read_bytes())
        _expect([item[0] for item in after] == [item[0] for item in before])
        _expect([(s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) for _, s in after] == [
            (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) for _, s in before
        ])
    elif case_number == 28:
        repo, index_path, index_a, index_b, record_a, record_b = _swappable_indexes(
            tmp_path
        )
        accepted = _git(repo, "ls-files", "--stage", "-z")
        snapshot = verifier.verify_index(index_path)
        destination = tmp_path / "destination-index"
        _git(repo, "read-tree", "--empty", index_path=destination)
        try:
            index_path.write_bytes(index_b)
            _expect(_git(repo, "ls-files", "--stage", "-z") == record_b)
            feeder = verifier.build_reconstruction_stream(snapshot)
            _git(
                repo,
                "update-index",
                "-z",
                "--index-info",
                input_bytes=feeder,
                index_path=destination,
            )
        finally:
            index_path.write_bytes(index_a)
        _expect(accepted == record_a)
        _expect(_git(repo, "ls-files", "--stage", "-z", index_path=destination) == record_a)
        _expect(index_path.read_bytes() == index_a)
        production_tree = ast.parse(Path(verifier.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(production_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        _expect("subprocess" not in imported)
        _expect("ls-files" not in Path(verifier.__file__).read_text(encoding="utf-8"))
    elif case_number == 29:
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        link = tmp_path / "index-link"
        link.symlink_to(index_path)
        with pytest.raises(verifier.VerificationError, match="acquisition.symlink"):
            verifier.verify_index(link)
    elif case_number == 30:
        real_parent = tmp_path / "real-parent"
        real_parent.mkdir()
        target = _write_index(real_parent, _index())
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        _expect(target.exists())
        with pytest.raises(verifier.VerificationError, match="acquisition.symlink"):
            verifier.verify_index(linked_parent / "index")
    elif case_number == 31:
        directory = tmp_path / "index-directory"
        directory.mkdir()
        with pytest.raises(verifier.VerificationError, match="acquisition.regular"):
            verifier.verify_index(directory)
    elif case_number == 32:
        oversized = tmp_path / "oversized-index"
        with oversized.open("wb") as stream:
            stream.truncate(verifier.LIMITS.index_bytes + 1)
        with pytest.raises(verifier.VerificationError) as raised:
            verifier.verify_index(oversized)
        _expect(raised.value.invariant == "index.size")
        bounded = _write_index(
            tmp_path,
            _index(
                extensions=[
                    _extension(b"TEST", b"x" * (verifier.LIMITS.read_chunk_bytes + 1))
                ]
            ),
            "bounded-index",
        )
        original_read = verifier.os.read
        requests: list[int] = []

        def recording_read(file_descriptor: int, size: int) -> bytes:
            requests.append(size)
            return original_read(file_descriptor, size)

        with monkeypatch.context() as context:
            context.setattr(verifier.os, "read", recording_read)
            verifier.verify_index(bounded)
        _expect(max(requests) == verifier.LIMITS.read_chunk_bytes)
    elif case_number == 33:
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        monkeypatch.setattr(verifier.os, "read", lambda _fd, _size: b"")
        with pytest.raises(verifier.VerificationError) as raised:
            verifier.verify_index(index_path)
        _expect(raised.value.invariant == "index.size")
    elif case_number == 34:
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        original_read = verifier.os.read

        def growing_read(file_descriptor: int, size: int) -> bytes:
            if size == 1:
                return b"x"
            return original_read(file_descriptor, size)

        with monkeypatch.context() as context:
            context.setattr(verifier.os, "read", growing_read)
            with pytest.raises(verifier.VerificationError) as raised:
                verifier.verify_index(index_path)
            _expect(raised.value.invariant == "acquisition.changed")
        with monkeypatch.context() as context:
            context.setattr(verifier, "_same_stable_file", lambda _left, _right: False)
            with pytest.raises(verifier.VerificationError) as raised:
                verifier.verify_index(index_path)
            _expect(raised.value.invariant == "acquisition.changed")
    elif case_number == 35:
        _rejects(_rehash(b"NOPE" + _index()[4:]), "index.signature")
    elif case_number == 36:
        _rejects(_index(version=1), "index.version")
    elif case_number == 37:
        _rejects(_index(version=4), "index.version")
    elif case_number == 38:
        _rejects(_index(version=99), "index.version")
    elif case_number == 39:
        _rejects(_wrap(b"", count=verifier.LIMITS.index_entries + 1), "index.count")
    elif case_number == 40:
        _rejects(_wrap(_entry(b"a"), count=2), "index.count")
    elif case_number == 41:
        corrupt = bytearray(_index())
        corrupt[-1] ^= 1
        _rejects(bytes(corrupt), "index.checksum")
    elif case_number == 42:
        _rejects(b"DIRC" + struct.pack(">II", 2, 0), "index.checksum")
    elif case_number == 43:
        _rejects(_wrap(b"x"), "extension.frame")
    elif case_number == 44:
        _rejects(_wrap(b"x" * 61, count=1), "entry.prefix")
    elif case_number == 45:
        prefix = _entry(b"")[:62]
        _rejects(_wrap(prefix + b"unterminated", count=1), "entry.path.terminator")
    elif case_number == 46:
        _rejects(_index([_entry(b"")]), "entry.path.shape")
    elif case_number == 47:
        _rejects(_index([_entry(b"bad-\xff")]), "entry.path.utf8")
    elif case_number == 48:
        _rejects(_index([_entry(b"/absolute")]), "entry.path.shape")
    elif case_number == 49:
        _rejects(_index([_entry(b"trailing/")]), "entry.path.shape")
    elif case_number == 50:
        _rejects(_index([_entry(b"empty//component")]), "entry.path.shape")
    elif case_number == 51:
        _rejects(_index([_entry(b"dot/./component")]), "entry.path.shape")
    elif case_number == 52:
        _rejects(_index([_entry(b"dot/../component")]), "entry.path.shape")
    elif case_number == 53:
        # The oracle is version-sensitive; reject a different version before
        # touching Git state. This subcase patches only the disposable context.
        with monkeypatch.context() as unsupported:
            unsupported.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
                stdout=b"git version 2.54.0\n", stderr=b""))
            with pytest.raises(RuntimeError, match="exactly Git 2.55.0"):
                _require_supported_git_version()
        _assert_git_admin_path_cases(tmp_path, [(path, True) for path in (
            bytes.fromhex("736166655c2e6769745c636f6e666967"),
            rb"safe/.git\config", rb"safe\.git/config", rb"safe\GIT~1\config",
            rb"safe\git~1. \config", rb"safe\.GIT.:stream\config",
            "safe/.g\u200cit/config".encode(),
            ".git:é".encode(), "safe\\.git:é\\config".encode(),
        )])
        _rejects(_index([_entry(b"nested/.git/config")]), "entry.path.shape")
        for component in (
            ".git",
            ".GIT",
            ".git.",
            ".git ",
            ".git:stream",
            "git~1",
            "GIT~1.",
            "git~1 ",
            "git~1:stream",
            "\u200c.git",
            ".g\u200di\u202at",
            ".git\ufeff",
        ):
            _expect(verifier._is_reserved_git_admin_component(component))
            _rejects(
                _index([_entry(f"safe/{component}/file".encode())]),
                "entry.path.shape",
            )
        for component in (
            ".git~1",
            "git~2",
            "git~1x",
            ".gitx",
            "x.git",
        ):
            _expect(not verifier._is_reserved_git_admin_component(component))
            _assert_path_accepted(f"safe/{component}/file".encode())
        # Subcases of the existing path-shape invariant, with stock Git as
        # the independent losslessness oracle (including regular-file modes).
        ignorables = [*range(0x200C, 0x2010), *range(0x202A, 0x202F),
                      *range(0x206A, 0x2070), 0xFEFF]
        aliases = [".gitmodules", ".GiTmOdUlEs"]
        aliases += [".git" + chr(code) + "modules" for code in ignorables]
        aliases += [chr(code) + ".gitmodules" + chr(code) for code in ignorables]
        ntfs = [".gitmodules", *(f"gitmod~{i}" for i in range(1, 5))]
        ntfs += ["gi7eba"[:length] + "~" + "1" + "0" * (6 - length)
                 for length in range(7)]
        ntfs += ["~9999999", "gi7eba~9", "gi7eb~99"]
        aliases += [stem.upper() + suffix for stem in ntfs
                    for suffix in ("", ". ", ":stream", " .:stream/é")]
        aliases += [".gitmodules/file", ".git\u200dmodules/file",
                    r"ordinary\gitmod~1", r"ordinary\.gitmodules"]
        controls = [".gitmodulesx", "x.gitmodules", "gitmod~0", "gitmod~5",
                    "gi7eba~0", "gi7eba~10", "gi7ebx~1", "gi7eb~01",
                    ".gitmodules\u200b", ".gitmodules.\u200c", "gitmod~1x",
                    "gitmod~1/file", ".gitmodules./file", r"ordinary\.gitmodulesx",
                    "ordinary\\.git\u200dmodules", "gi7eb~1", "gi7eba~1x"]
        repo, _source_index, _raw, _records = _real_index(tmp_path)
        oid = _git(repo, "hash-object", "-w", "--stdin", input_bytes=b"target").strip()
        for number, (name, rejected) in enumerate(
            [(name, True) for name in aliases] + [(name, False) for name in controls]
        ):
            for prefix in ("", "nested/"):
                for mode in (0o100644, 0o100755, 0o120000):
                    path = (prefix + name).encode()
                    records = _record(f"{mode:06o}".encode(), oid, path)
                    destination = tmp_path / f"roundtrip-{number}-{len(prefix)}-{mode}"
                    _git(repo, "-c", "core.protectHFS=true", "-c", "core.protectNTFS=true",
                         "update-index", "-z", "--index-info", input_bytes=records,
                         index_path=destination)
                    actual = _git(repo, "ls-files", "--stage", "-z", index_path=destination)
                    should_reject = rejected and mode == 0o120000
                    _expect(actual == (b"" if should_reject else records))
                    raw = _index([_entry(path, mode=mode, oid=bytes.fromhex(oid.decode()))])
                    if should_reject:
                        _rejects(raw, "entry.path.shape")
                    else:
                        _expect(verifier._parse_index_bytes(raw).reconstruction_records == records)
    elif case_number == 54:
        path = b"a" * (verifier.LIMITS.path_bytes + 1)
        _rejects(_index([_entry(path)]), "entry.path.length")
    elif case_number == 55:
        _rejects(_index([_entry(b"mismatch", flags=1)]), "entry.path.length")
    elif case_number == 56:
        _rejects(_index([_entry(b"same"), _entry(b"same")]), "entry.path.order")
        _rejects(
            _index([_entry(b"a"), _entry(b"a-"), _entry(b"a/b")]),
            "entry.path.order",
        )
    elif case_number == 57:
        _rejects(_index([_entry(b"z"), _entry(b"a")]), "entry.path.order")
    elif case_number == 58:
        _rejects(_index([_entry(b"ab", padding_byte=1)]), "entry.padding")
    elif case_number == 59:
        _rejects(_index([_entry(b"a", flags=0x8001)]), "entry.flags")
    elif case_number == 60:
        _rejects(_index([_entry(b"a", flags=0x4001)]), "entry.flags")
    elif case_number == 61:
        _rejects(_index([_entry(b"a", flags=0x1001)]), "entry.flags")
    elif case_number == 62:
        _rejects(_index([_entry(b"a", mode=0o160000)]), "entry.mode")
    elif case_number == 63:
        _rejects(_index([_entry(b"a", mode=0o100600)]), "entry.mode")
    elif case_number == 64:
        _rejects(_index([_entry(b"a", oid=b"\0" * 20)]), "entry.oid")
    elif case_number == 65:
        extensions = [_extension(b"TEST", b"") for _ in range(129)]
        _rejects(_index(extensions=extensions), "extension.count")
    elif case_number == 66:
        frame = b"TEST" + (verifier.LIMITS.extension_payload_bytes + 1).to_bytes(4, "big")
        _rejects(_wrap(frame), "extension.bytes")
    elif case_number == 67:
        half = verifier.LIMITS.extension_total_bytes // 2
        extensions = [_extension(b"TESA", b"a" * half), _extension(b"TESB", b"b" * half)]
        _rejects(_index(extensions=extensions), "extension.bytes")
    elif case_number == 68:
        _rejects(_wrap(b"TEST\0\0\0"), "extension.frame")
    elif case_number == 69:
        _rejects(_wrap(b"TEST" + (10).to_bytes(4, "big") + b"x"), "extension.frame")
    elif case_number == 70:
        _rejects(_index(extensions=[_extension(b"FSMN", b"")]), "extension.fsmonitor")
    elif case_number == 71:
        _rejects(_index(extensions=[_extension(b"abcd", b"")]), "extension.required")
    elif case_number == 72:
        _rejects(_index(extensions=[_extension(b"link", b"")]), "extension.required")
    elif case_number == 73:
        _rejects(_index(extensions=[_extension(b"sdir", b"")]), "extension.required")
    elif case_number == 74:
        raw = _index([_entry(b"a")])
        parsed_record_size = len(_record(b"100644", _OID.hex().encode(), b"a"))
        limits = dataclasses.replace(verifier.LIMITS, reconstruction_bytes=parsed_record_size - 1)
        with monkeypatch.context() as context:
            context.setattr(
                verifier,
                "_reconstruction_record",
                lambda *_args: (_ for _ in ()).throw(
                    AssertionError("reconstruction allocation began before preflight")
                ),
            )
            _rejects(raw, "output.reconstruction_limit", limits=limits)
    elif case_number == 75:
        parsed = verifier._parse_index_bytes(_index([_entry(b"a")]))
        acquisition = verifier.Acquisition(1, 0, 0, 0, stat.S_IFREG, 0, len(_index()))
        accepted_size = len(verifier._build_result(parsed, acquisition, "0" * 64))
        encoded_records_size = 4 * ((len(parsed.reconstruction_records) + 2) // 3)
        _expect(accepted_size > encoded_records_size + 1)
        intermediate_limit = encoded_records_size + 1
        monkeypatch.setattr(
            verifier,
            "LIMITS",
            dataclasses.replace(verifier.LIMITS, manifest_bytes=intermediate_limit),
        )
        base64_started = False
        derived_json_started = False
        original_canonical_json = verifier.canonical_json

        def forbidden_b64encode(payload: bytes) -> bytes:
            nonlocal base64_started
            base64_started = True
            raise AssertionError("Base64 allocation began before manifest preflight")

        def guarded_canonical_json(value: object) -> bytes:
            nonlocal derived_json_started
            schema = value.get("schema") if isinstance(value, dict) else None
            if schema in {
                "graphify.protected-change-review.candidate-index.v1",
                "graphify.protected-change-verifier.result.v1",
            }:
                derived_json_started = True
                raise AssertionError("derived JSON allocation began before manifest preflight")
            return original_canonical_json(value)

        monkeypatch.setattr(verifier.base64, "b64encode", forbidden_b64encode)
        monkeypatch.setattr(verifier, "canonical_json", guarded_canonical_json)
        with pytest.raises(verifier.VerificationError) as raised:
            verifier._build_result(parsed, acquisition, "0" * 64)
        _expect(raised.value.invariant == "output.manifest_limit")
        _expect(raised.value.details["actual"] > intermediate_limit)
        _expect(not base64_started)
        _expect(not derived_json_started)
    elif case_number == 76:
        parsed = verifier._parse_index_bytes(_index())
        with pytest.raises(verifier.VerificationError) as raised:
            verifier.build_reconstruction_stream(parsed)  # type: ignore[arg-type]
        _expect(raised.value.invariant == "attestation.invalid")
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        issued = verifier.verify_index(index_path)
        fabricated = dataclasses.replace(issued, _token=object())
        with pytest.raises(verifier.VerificationError) as raised:
            verifier.build_reconstruction_stream(fabricated)
        _expect(raised.value.invariant == "attestation.invalid")
    elif case_number == 77:
        _repo, index_path, _raw, _record_bytes = _real_index(tmp_path)
        snapshot = verifier.verify_index(index_path)
        corrupt = dataclasses.replace(snapshot, reconstruction_records=b"corrupt")
        with pytest.raises(verifier.VerificationError) as raised:
            verifier.build_reconstruction_stream(corrupt)
        _expect(raised.value.invariant == "attestation.invalid")
    elif case_number == 78:
        path = _write_index(tmp_path, _index()[:-1] + b"x", "rejected-index")
        normal = _cli(path)
        optimized = _cli(path, optimized=True)
        _expect((normal.returncode, normal.stdout, normal.stderr) == (
            optimized.returncode,
            optimized.stdout,
            optimized.stderr,
        ))
        _expect(json.loads(normal.stdout)["invariant"] == "index.checksum")
    elif case_number == 79:
        sentinel = b"HOSTILE-RAW-PATH-SENTINEL"
        path = _write_index(
            tmp_path,
            _index([_entry(sentinel + b"/../escape")]),
            "HOSTILE-INPUT-PATH-SENTINEL",
        )
        completed = _cli(path)
        _expect(completed.returncode == 1)
        _expect(completed.stderr == b"")
        _expect(sentinel not in completed.stdout)
        _expect(sentinel not in completed.stderr)
        _expect(b"HOSTILE-INPUT-PATH-SENTINEL" not in completed.stdout)
        _expect(b"HOSTILE-INPUT-PATH-SENTINEL" not in completed.stderr)
        _expect(json.loads(completed.stdout)["invariant"] == "entry.path.shape")
    elif case_number == 80:
        path = _write_index(tmp_path, _rehash(b"NOPE" + _index()[4:]), "bad-index")
        completed = _cli(path)
        expected = verifier.canonical_json(
            {
                "details": {},
                "invariant": "index.signature",
                "result": "rejected",
                "schema": "graphify.protected-change-review.verifier-error.v1",
                "schema_sha256": verifier.SCHEMA_SHA256,
                "verifier": verifier.VERIFIER_VERSION,
            }
        )
        _expect(completed.returncode == 1)
        _expect(completed.stderr == b"")
        _expect(completed.stdout == expected)
        _expect(not completed.stdout.endswith(b"\n"))
    else:
        raise AssertionError("the frozen matrix contains exactly cases 1 through 80")
