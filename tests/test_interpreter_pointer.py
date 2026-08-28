"""Hostile regression tests for the advisory interpreter pointer.

The pointer is diagnostic state only.  These tests cover safe publication; they
must not grow a read-or-execute API that could turn workspace text into program
selection authority.
"""
from __future__ import annotations

import importlib
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _api():
    """Import lazily so the RED suite still collects before production exists."""
    return importlib.import_module("graphify.interpreter_pointer")


def _safe_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "graphify-out"
    parent.mkdir(mode=0o700)
    if os.name != "nt":
        parent.chmod(0o700)
    return parent


def _write(pointer: Path, *, interpreter: Path | str | None = None) -> Path:
    kwargs = {} if interpreter is None else {"interpreter": interpreter}
    return _api().write_interpreter_pointer(pointer, **kwargs)


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication contract")
def test_write_publishes_exact_absolute_interpreter_with_restrictive_mode(tmp_path):
    pointer = _safe_parent(tmp_path) / ".graphify_python"
    expected = Path(sys.executable)

    result = _write(pointer, interpreter=expected)

    assert result == expected
    assert pointer.read_text(encoding="utf-8") == str(expected)
    assert pointer.is_file() and not pointer.is_symlink()
    if os.name != "nt":
        assert stat.S_IMODE(pointer.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication contract")
def test_default_interpreter_is_current_process_lexical_executable(tmp_path, monkeypatch):
    pointer = _safe_parent(tmp_path) / ".graphify_python"
    lexical = str(Path(sys.executable).parent / ".." / Path(sys.executable).parent.name / Path(sys.executable).name)
    module = _api()
    monkeypatch.setattr(module.sys, "executable", lexical)

    result = module.write_interpreter_pointer(pointer)

    assert result == Path(lexical)
    assert pointer.read_text(encoding="utf-8") == lexical


@pytest.mark.parametrize("interpreter", ["python3", "../python3", "/tmp/python\n/attacker"])
def test_write_rejects_malformed_interpreter_without_creating_pointer(tmp_path, interpreter):
    pointer = _safe_parent(tmp_path) / ".graphify_python"

    with pytest.raises(_api().InterpreterPointerError):
        _write(pointer, interpreter=interpreter)

    assert not pointer.exists()


def test_write_rejects_non_executable_target(tmp_path):
    target = tmp_path / "not-executable"
    target.write_text("not a program", encoding="utf-8")
    target.chmod(0o600)
    pointer = _safe_parent(tmp_path) / ".graphify_python"

    with pytest.raises(_api().InterpreterPointerError, match="executable"):
        _write(pointer, interpreter=target)

    assert not pointer.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink boundary")
def test_write_rejects_symlink_parent_without_touching_target(tmp_path):
    real_parent = tmp_path / "real-output"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "graphify-out"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(_api().InterpreterPointerError, match="parent"):
        _write(linked_parent / ".graphify_python", interpreter=sys.executable)

    assert list(real_parent.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_intermediate_ancestor_swap_to_symlink_never_redirects_publication(tmp_path, monkeypatch):
    outer = tmp_path / "outer"
    parent = outer / "inner" / "graphify-out"
    parent.mkdir(parents=True, mode=0o700)
    detached_outer = tmp_path / "detached-outer"
    attacker = tmp_path / "attacker"
    attacker_parent = attacker / "inner" / "graphify-out"
    attacker_parent.mkdir(parents=True, mode=0o700)
    pointer = parent / ".graphify_python"
    module = _api()
    real_open = module.os.open
    swapped = False

    def swap_ancestor_then_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "inner" and kwargs.get("dir_fd") is not None and not swapped:
            outer.rename(detached_outer)
            outer.symlink_to(attacker, target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", swap_ancestor_then_open)

    with pytest.raises(module.InterpreterPointerError, match="parent changed"):
        module.write_interpreter_pointer(pointer, interpreter=sys.executable)

    assert swapped
    assert not (attacker_parent / pointer.name).exists()
    assert not (detached_outer / "inner" / "graphify-out" / pointer.name).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor traversal contract")
def test_write_fails_closed_when_descriptor_traversal_is_unavailable(tmp_path, monkeypatch):
    pointer = _safe_parent(tmp_path) / ".graphify_python"
    module = _api()
    monkeypatch.setattr(module, "_POSIX_DIR_FD_TRAVERSAL", False)

    with pytest.raises(module.InterpreterPointerError, match="traversal is unavailable"):
        module.write_interpreter_pointer(pointer, interpreter=sys.executable)

    assert not pointer.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent validation contract")
def test_write_rejects_non_directory_parent(tmp_path):
    parent = tmp_path / "graphify-out"
    parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(_api().InterpreterPointerError, match="parent"):
        _write(parent / ".graphify_python", interpreter=sys.executable)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_write_rejects_group_or_other_writable_parent(tmp_path):
    parent = _safe_parent(tmp_path)
    parent.chmod(0o777)

    with pytest.raises(_api().InterpreterPointerError, match="parent"):
        _write(parent / ".graphify_python", interpreter=sys.executable)

    assert list(parent.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink boundary")
def test_write_rejects_symlink_destination_and_preserves_target(tmp_path):
    parent = _safe_parent(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("do not replace", encoding="utf-8")
    pointer = parent / ".graphify_python"
    pointer.symlink_to(victim)

    with pytest.raises(_api().InterpreterPointerError, match="destination"):
        _write(pointer, interpreter=sys.executable)

    assert pointer.is_symlink()
    assert victim.read_text(encoding="utf-8") == "do not replace"


@pytest.mark.parametrize("kind", ["directory", "fifo"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX non-regular file types")
def test_write_rejects_non_regular_destination(tmp_path, kind):
    pointer = _safe_parent(tmp_path) / ".graphify_python"
    if kind == "directory":
        pointer.mkdir()
    else:
        os.mkfifo(pointer)

    with pytest.raises(_api().InterpreterPointerError, match="destination"):
        _write(pointer, interpreter=sys.executable)

    assert pointer.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX atomic replacement contract")
def test_atomic_replace_failure_preserves_old_pointer_and_cleans_temp(tmp_path, monkeypatch):
    parent = _safe_parent(tmp_path)
    pointer = parent / ".graphify_python"
    old = sys.executable
    pointer.write_text(old, encoding="utf-8")
    pointer.chmod(0o600)
    module = _api()

    def fail_replace(*args, **kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)

    with pytest.raises(module.InterpreterPointerError, match="publish|replace|atomic"):
        module.write_interpreter_pointer(pointer, interpreter=sys.executable)

    assert pointer.read_text(encoding="utf-8") == old
    assert sorted(path.name for path in parent.iterdir()) == [pointer.name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX atomic replacement contract")
def test_pre_replace_flush_failure_preserves_old_pointer_and_cleans_temp(tmp_path, monkeypatch):
    parent = _safe_parent(tmp_path)
    pointer = parent / ".graphify_python"
    old = sys.executable
    pointer.write_text(old, encoding="utf-8")
    pointer.chmod(0o600)
    module = _api()

    def fail_fsync(fd):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(module.os, "fsync", fail_fsync)

    with pytest.raises(module.InterpreterPointerError, match="publish|flush|atomic"):
        module.write_interpreter_pointer(pointer, interpreter=sys.executable)

    assert pointer.read_text(encoding="utf-8") == old
    assert sorted(path.name for path in parent.iterdir()) == [pointer.name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX destination replacement schedule")
def test_destination_swap_before_publication_fails_closed(tmp_path, monkeypatch):
    parent = _safe_parent(tmp_path)
    pointer = parent / ".graphify_python"
    old = sys.executable
    pointer.write_text(old, encoding="utf-8")
    pointer.chmod(0o600)
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    module = _api()
    real_replace = module.os.replace

    def swap_then_replace(src, dst, *args, **kwargs):
        pointer.unlink()
        pointer.symlink_to(victim)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(module.os, "replace", swap_then_replace)

    published = False
    try:
        module.write_interpreter_pointer(pointer, interpreter=sys.executable)
        published = True
    except module.InterpreterPointerError:
        pass

    assert victim.read_text(encoding="utf-8") == "untouched"
    if published:
        assert not pointer.is_symlink()
        assert pointer.read_text(encoding="utf-8") == sys.executable


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent replacement schedule")
def test_parent_swap_at_replace_boundary_never_reports_false_success(tmp_path, monkeypatch):
    parent = _safe_parent(tmp_path)
    pointer = parent / ".graphify_python"
    detached_parent = tmp_path / "detached-output"
    module = _api()
    real_replace = module.os.replace

    def swap_parent_then_replace(src, dst, *args, **kwargs):
        parent.rename(detached_parent)
        parent.mkdir(mode=0o700)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(module.os, "replace", swap_parent_then_replace)

    with pytest.raises(module.InterpreterPointerError, match="parent changed"):
        module.write_interpreter_pointer(pointer, interpreter=sys.executable)

    assert not pointer.exists()
    detached_pointer = detached_parent / pointer.name
    assert detached_pointer.read_text(encoding="utf-8") == sys.executable
    assert not [path for path in detached_parent.iterdir() if path != detached_pointer]


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication contract")
def test_concurrent_writers_publish_only_complete_values(tmp_path):
    pointer = _safe_parent(tmp_path) / ".graphify_python"
    values = [Path(sys.executable)] * 12

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(lambda value: _write(pointer, interpreter=value), values))

    assert outcomes == values
    assert pointer.read_text(encoding="utf-8") == str(Path(sys.executable))
    assert not [path for path in pointer.parent.iterdir() if path != pointer]


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication contract")
def test_cli_write_uses_running_interpreter_and_returns_success(tmp_path):
    pointer = _safe_parent(tmp_path) / ".graphify_python"

    result = subprocess.run(
        [sys.executable, "-E", "-P", "-B", "-m", "graphify.interpreter_pointer", "write", str(pointer)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert pointer.read_text(encoding="utf-8") == sys.executable


def test_cli_failure_is_nonzero_and_preserves_old_pointer(tmp_path):
    parent = _safe_parent(tmp_path)
    pointer = parent / ".graphify_python"
    old = sys.executable
    pointer.write_text(old, encoding="utf-8")
    pointer.chmod(0o600)
    if os.name != "nt":
        parent.chmod(0o777)

    result = subprocess.run(
        [sys.executable, "-E", "-P", "-B", "-m", "graphify.interpreter_pointer", "write", str(pointer)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert pointer.read_text(encoding="utf-8") == old


def test_forced_windows_branch_fails_closed_without_publication(tmp_path, monkeypatch):
    module = _api()
    pointer = _safe_parent(tmp_path) / ".graphify_python"
    monkeypatch.setattr(module, "_WINDOWS", True)

    with pytest.raises(module.InterpreterPointerError, match="unavailable on Windows"):
        module.write_interpreter_pointer(pointer, interpreter=sys.executable)

    assert not pointer.exists()
    assert list(pointer.parent.iterdir()) == []


def test_forced_windows_branch_preserves_reparse_like_destination(tmp_path, monkeypatch):
    module = _api()
    parent = _safe_parent(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    pointer = parent / ".graphify_python"
    pointer.symlink_to(victim)
    monkeypatch.setattr(module, "_WINDOWS", True)

    with pytest.raises(module.InterpreterPointerError, match="unavailable on Windows"):
        module.write_interpreter_pointer(pointer, interpreter=sys.executable)

    assert pointer.is_symlink()
    assert victim.read_text(encoding="utf-8") == "untouched"


def test_module_exposes_no_pointer_read_or_execute_api():
    public_names = {name for name in vars(_api()) if not name.startswith("_")}

    assert not public_names & {
        "read_interpreter_pointer",
        "execute_interpreter_pointer",
        "resolve_interpreter_pointer",
    }
