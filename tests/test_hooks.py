"""Tests for hooks.py - git hook install/uninstall."""
import ast
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
import graphify.hooks as hooks
from graphify.hooks import (
    _CHECKOUT_MARKER,
    _CHECKOUT_MARKER_END,
    _HOOK_MARKER,
    _HOOK_MARKER_END,
    _POST_MERGE_MARKER,
    _POST_MERGE_MARKER_END,
    _hooks_dir,
    install,
    status,
    uninstall,
)


def _make_git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    return tmp_path


def test_install_creates_hook(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = install(repo)
    hook = repo / ".git" / "hooks" / "post-commit"
    assert hook.exists()
    assert _HOOK_MARKER in hook.read_text()
    assert "installed" in result


def test_install_is_executable(tmp_path):
    repo = _make_git_repo(tmp_path)
    install(repo)
    hook = repo / ".git" / "hooks" / "post-commit"
    if os.name == "nt":
        assert hook.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    else:
        assert hook.stat().st_mode & 0o111  # executable bit set


def test_install_idempotent(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    install(repo)
    hook_paths = {
        repo / ".git" / "hooks" / "post-commit",
        repo / ".git" / "hooks" / "post-checkout",
        repo / ".git" / "hooks" / "post-merge",
    }
    original_write_text = Path.write_text
    original_write_bytes = Path.write_bytes

    def spy_write_text(path, *args, **kwargs):
        assert path not in hook_paths, f"idempotent reinstall rewrote {path.name}"
        return original_write_text(path, *args, **kwargs)

    def spy_write_bytes(path, *args, **kwargs):
        assert path not in hook_paths, f"idempotent reinstall rewrote {path.name}"
        return original_write_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy_write_text)
    monkeypatch.setattr(Path, "write_bytes", spy_write_bytes)
    result = install(repo)
    assert "already installed" in result
    for hook_path, marker in zip(
        sorted(hook_paths),
        (_CHECKOUT_MARKER, _HOOK_MARKER, _POST_MERGE_MARKER),
        strict=True,
    ):
        assert hook_path.read_text().count(marker) == 1


def _rendered_hook(script: str) -> bytes:
    exclusion = hooks._POST_COMMIT_OUTPUT_EXCLUSION.replace(
        "__GRAPHIFY_REPO_OUTPUT__", shlex.quote(hooks._merge_output_path())
    )
    return (
        script.replace("__PINNED_PYTHON__", shlex.quote(hooks._pinned_python()))
        .replace("__GRAPHIFY_OUTPUT__", shlex.quote(hooks._hook_output_path()))
        .replace("__GRAPHIFY_OUTPUT_EXCLUSION__", exclusion)
        .encode()
    )


def test_install_updates_existing_graphify_sections_and_preserves_other_content(tmp_path):
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    commit_hook = hooks_dir / "post-commit"
    checkout_hook = hooks_dir / "post-checkout"
    commit_prefix = b"#!/bin/sh\nprintf 'commit prefix\\n'\n"
    commit_suffix = b"printf 'commit suffix\\n'\n"
    checkout_prefix = b"#!/bin/sh\r\nprintf 'checkout prefix\\r\\n'\r\n"
    stale_commit = (
        f"{_HOOK_MARKER}\n# stale commit body\n{_HOOK_MARKER_END}\n".encode()
    )
    stale_checkout = (
        f"{_CHECKOUT_MARKER}\r\n# stale checkout body\r\n{_CHECKOUT_MARKER_END}".encode()
    )
    commit_hook.write_bytes(commit_prefix + stale_commit + commit_suffix)
    checkout_hook.write_bytes(checkout_prefix + stale_checkout)
    commit_hook.chmod(0o751)
    checkout_hook.chmod(0o741)

    result = install(repo)

    commit_bytes = commit_hook.read_bytes()
    checkout_bytes = checkout_hook.read_bytes()
    assert "updated" in result
    assert commit_bytes == commit_prefix + _rendered_hook(hooks._HOOK_SCRIPT) + commit_suffix
    assert checkout_bytes == checkout_prefix + _rendered_hook(hooks._CHECKOUT_SCRIPT)
    assert commit_bytes.count(_HOOK_MARKER.encode()) == 1
    assert commit_bytes.count(_HOOK_MARKER_END.encode()) == 1
    assert checkout_bytes.count(_CHECKOUT_MARKER.encode()) == 1
    assert checkout_bytes.count(_CHECKOUT_MARKER_END.encode()) == 1
    assert commit_hook.stat().st_mode & 0o777 == 0o751
    assert checkout_hook.stat().st_mode & 0o777 == 0o751


@pytest.mark.parametrize(
    "malformed",
    [
        f"{_HOOK_MARKER}\nlegacy\n",
        f"{_HOOK_MARKER_END}\nlegacy\n",
        f"{_HOOK_MARKER_END}\nlegacy\n{_HOOK_MARKER}\n",
        f"{_HOOK_MARKER}\n{_HOOK_MARKER}\nlegacy\n{_HOOK_MARKER_END}\n",
        f"{_HOOK_MARKER}\nlegacy\n{_HOOK_MARKER_END}\n{_HOOK_MARKER_END}\n",
        (
            f"{_HOOK_MARKER}\nlegacy one\n{_HOOK_MARKER_END}\n"
            f"{_HOOK_MARKER}\nlegacy two\n{_HOOK_MARKER_END}\n"
        ),
    ],
    ids=["start-only", "end-only", "reversed", "duplicate-start", "duplicate-end", "two-pairs"],
)
def test_install_rejects_malformed_hook_markers_without_writing(tmp_path, malformed):
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    commit_hook = hooks_dir / "post-commit"
    checkout_hook = hooks_dir / "post-checkout"
    commit_before = ("#!/bin/sh\n" + malformed + "printf 'commit suffix\\n'\n").encode()
    checkout_before = b"#!/bin/sh\nprintf 'checkout untouched\\n'\n"
    commit_hook.write_bytes(commit_before)
    checkout_hook.write_bytes(checkout_before)

    with pytest.raises(RuntimeError, match="marker|Graphify|graphify"):
        install(repo)

    assert commit_hook.read_bytes() == commit_before
    assert checkout_hook.read_bytes() == checkout_before


def test_install_preflights_malformed_checkout_before_updating_commit(tmp_path):
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    commit_hook = hooks_dir / "post-commit"
    checkout_hook = hooks_dir / "post-checkout"
    commit_before = (
        f"#!/bin/sh\n{_HOOK_MARKER}\n# stale commit\n{_HOOK_MARKER_END}\n"
    ).encode()
    checkout_before = (
        f"#!/bin/sh\n{_CHECKOUT_MARKER}\n# missing checkout end\n"
    ).encode()
    commit_hook.write_bytes(commit_before)
    checkout_hook.write_bytes(checkout_before)

    with pytest.raises(RuntimeError, match="marker|Graphify|graphify"):
        install(repo)

    assert commit_hook.read_bytes() == commit_before
    assert checkout_hook.read_bytes() == checkout_before


def test_install_ignores_marker_like_substrings_as_ownership(tmp_path):
    repo = _make_git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    existing = (
        "#!/bin/sh\n"
        f"printf '%s\\n' '{_HOOK_MARKER}'\n"
        f"{_HOOK_MARKER_END} trailing text\n"
    ).encode()
    hook.write_bytes(existing)

    result = install(repo)

    content = hook.read_bytes()
    assert "appended" in result
    assert content.startswith(existing)
    lines = content.decode().splitlines()
    assert sum(line == _HOOK_MARKER for line in lines) == 1
    assert sum(line == _HOOK_MARKER_END for line in lines) == 1


def test_status_ignores_marker_like_substrings(tmp_path):
    repo = _make_git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{_HOOK_MARKER}'\n",
        encoding="utf-8",
    )

    result = status(repo)

    assert result.splitlines()[0] == (
        "post-commit: not installed (hook exists but graphify not found)"
    )


def test_substring_only_user_hook_round_trips_through_install_and_uninstall(tmp_path):
    repo = _make_git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    user_content = (
        "#!/bin/sh\n"
        f"printf '%s\\n' '{_HOOK_MARKER}'\n"
        f"printf '%s\\n' '{_HOOK_MARKER_END}'\n"
        "printf 'user suffix\\n'\n"
    ).encode()
    hook.write_bytes(user_content)
    hook.chmod(0o751)

    install(repo)
    installed = hook.read_bytes()
    assert status(repo).splitlines()[0] == "post-commit: installed"
    assert installed.startswith(user_content.rstrip() + b"\n\n")
    assert hook.stat().st_mode & 0o777 == 0o751

    result = uninstall(repo)

    assert "graphify removed" in result
    assert hook.read_bytes() == user_content.rstrip() + b"\n\n"
    assert hook.stat().st_mode & 0o777 == 0o751


@pytest.mark.parametrize(
    "malformed",
    [
        f"{_HOOK_MARKER}\nlegacy\n",
        f"{_HOOK_MARKER_END}\nlegacy\n",
        f"{_HOOK_MARKER_END}\nlegacy\n{_HOOK_MARKER}\n",
        (
            f"{_HOOK_MARKER}\nlegacy one\n{_HOOK_MARKER_END}\n"
            f"{_HOOK_MARKER}\nlegacy two\n{_HOOK_MARKER_END}\n"
        ),
    ],
    ids=["start-only", "end-only", "reversed", "two-pairs"],
)
def test_uninstall_rejects_malformed_hook_markers_without_writing(tmp_path, malformed):
    repo = _make_git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    before = ("#!/bin/sh\n" + malformed + "printf 'user suffix\\n'\n").encode()
    hook.write_bytes(before)
    hook.chmod(0o751)

    with pytest.raises(RuntimeError, match="marker|Graphify|graphify"):
        uninstall(repo)

    assert hook.read_bytes() == before
    assert hook.stat().st_mode & 0o777 == 0o751


def test_uninstall_preflights_malformed_checkout_before_removing_commit(tmp_path):
    repo = _make_git_repo(tmp_path)
    install(repo)
    hooks_dir = repo / ".git" / "hooks"
    commit_hook = hooks_dir / "post-commit"
    checkout_hook = hooks_dir / "post-checkout"
    checkout_hook.write_bytes(
        (
            f"#!/bin/sh\n{_CHECKOUT_MARKER}\nlegacy one\n{_CHECKOUT_MARKER_END}\n"
            f"{_CHECKOUT_MARKER}\nlegacy two\n{_CHECKOUT_MARKER_END}\n"
        ).encode()
    )
    checkout_hook.chmod(0o741)
    commit_before = commit_hook.read_bytes()
    checkout_before = checkout_hook.read_bytes()
    commit_mode = commit_hook.stat().st_mode
    checkout_mode = checkout_hook.stat().st_mode

    with pytest.raises(RuntimeError, match="marker|Graphify|graphify"):
        uninstall(repo)

    assert commit_hook.read_bytes() == commit_before
    assert checkout_hook.read_bytes() == checkout_before
    assert commit_hook.stat().st_mode == commit_mode
    assert checkout_hook.stat().st_mode == checkout_mode


def test_install_appends_to_existing_hook(tmp_path):
    repo = _make_git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/bash\necho existing\n")
    hook.chmod(0o755)
    install(repo)
    content = hook.read_text()
    assert "existing" in content
    assert _HOOK_MARKER in content


def test_uninstall_removes_hook(tmp_path):
    repo = _make_git_repo(tmp_path)
    install(repo)
    result = uninstall(repo)
    hook = repo / ".git" / "hooks" / "post-commit"
    assert not hook.exists()
    assert "removed" in result.lower()


def test_uninstall_no_hook(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = uninstall(repo)
    assert "nothing to remove" in result


def test_status_installed(tmp_path):
    repo = _make_git_repo(tmp_path)
    install(repo)
    result = status(repo)
    assert "installed" in result


def test_status_not_installed(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = status(repo)
    assert "not installed" in result


def test_no_git_repo_raises(tmp_path):
    with pytest.raises(RuntimeError, match="No git repository"):
        install(tmp_path / "not_a_repo")


def test_install_creates_post_checkout_hook(tmp_path):
    repo = _make_git_repo(tmp_path)
    install(repo)
    hook = repo / ".git" / "hooks" / "post-checkout"
    assert hook.exists()
    assert _CHECKOUT_MARKER in hook.read_text()


def test_install_creates_post_merge_hook(tmp_path):
    repo = _make_git_repo(tmp_path)
    install(repo)
    hook = repo / ".git" / "hooks" / "post-merge"
    assert hook.exists()
    assert _POST_MERGE_MARKER in hook.read_text()


def test_install_post_checkout_is_executable(tmp_path):
    repo = _make_git_repo(tmp_path)
    install(repo)
    hook = repo / ".git" / "hooks" / "post-checkout"
    if os.name == "nt":
        assert hook.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    else:
        assert hook.stat().st_mode & 0o111


def test_uninstall_removes_post_checkout_hook(tmp_path):
    repo = _make_git_repo(tmp_path)
    install(repo)
    uninstall(repo)
    hook = repo / ".git" / "hooks" / "post-checkout"
    assert not hook.exists()


def test_status_shows_all_hooks(tmp_path):
    repo = _make_git_repo(tmp_path)
    install(repo)
    result = status(repo)
    assert "post-commit" in result
    assert "post-checkout" in result
    assert "post-merge" in result
    assert result.splitlines()[:3] == [
        "post-commit: installed",
        "post-checkout: installed",
        "post-merge: installed",
    ]



def test_hooks_dir_resolves_relative_git_hooks_path(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=".git/hooks\n")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _hooks_dir(repo) == (repo / ".git" / "hooks").resolve()


def test_hooks_dir_rejects_multiline_git_output(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="--path-format=absolute\n.git/hooks\n")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _hooks_dir(repo) == repo / ".git" / "hooks"
    assert not (repo / "--path-format=absolute\n.git").exists()


def test_hooks_dir_accepts_absolute_git_hooks_path(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hooks = tmp_path / "actual-hooks"

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=f"{hooks}\n")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _hooks_dir(repo) == hooks.resolve()

def test_hook_skips_head_on_exe():
    """Hook script must skip shebang extraction for .exe binaries (Windows)."""
    from graphify.hooks import _PYTHON_DETECT
    assert "*.exe) _SHEBANG=" in _PYTHON_DETECT or '*.exe)' in _PYTHON_DETECT


def test_install_embeds_pinned_interpreter(tmp_path):
    """Hook scripts must embed sys.executable so the hook works without the
    graphify launcher on PATH (uv tool / pipx isolation, #1127).

    When graphify is installed via `uv tool install graphifyy` or `pipx install
    graphifyy`, the interpreter lives in an isolated venv and the launcher is in
    ~/.local/bin.  GUI git clients and CI runners often run with a minimal PATH
    that omits that directory, so `command -v graphify` fails, the python3/python
    fallbacks cannot import graphify (wrong venv), and the hook silently exits 0.
    Pinning sys.executable at install time makes the hook work regardless of PATH.
    """
    import re, sys
    repo = _make_git_repo(tmp_path)
    install(repo)
    commit_hook = (repo / ".git" / "hooks" / "post-commit").read_text()
    checkout_hook = (repo / ".git" / "hooks" / "post-checkout").read_text()
    # Compute the sanitized value the same way install() does.
    expected = sys.executable if not re.search(r"[^a-zA-Z0-9/_.@:\\-]", sys.executable) else ""
    if expected:
        assert expected in commit_hook, "sanitized sys.executable missing from post-commit"
        assert expected in checkout_hook, "sanitized sys.executable missing from post-checkout"
    # The placeholder must be fully substituted -- no __PINNED_PYTHON__ left.
    assert "__PINNED_PYTHON__" not in commit_hook, "placeholder not substituted in post-commit"
    assert "__PINNED_PYTHON__" not in checkout_hook, "placeholder not substituted in post-checkout"


def test_install_fallback_is_loud_not_silent(tmp_path):
    """The detection fallback must emit a message to stderr rather than bare exit 0.

    A silent no-op (the pre-fix behaviour) leaves the user with no indication
    that the hook ran but found nothing, making the bug extremely hard to diagnose.
    """
    from graphify.hooks import _PYTHON_DETECT
    assert "could not locate" in _PYTHON_DETECT, (
        "fallback branch must print a diagnostic message; bare 'exit 0' is silent and unhelpful"
    )


def test_hook_discovery_never_reads_workspace_interpreter_pointer():
    """A valid-looking pointer naming an attacker must not be a hook fallback."""
    from graphify.hooks import _PYTHON_DETECT

    assert ".graphify_python" not in _PYTHON_DETECT
    assert "_FROM_FILE" not in _PYTHON_DETECT


def test_hook_probe_and_detached_launch_both_use_isolation_flags():
    """The value accepted by the isolated probe remains bound for isolated launch."""
    from graphify.hooks import _PYTHON_DETECT

    probe_lines = [line for line in _PYTHON_DETECT.splitlines() if '"$_GFY_PROBE"' in line]
    assert probe_lines
    assert all(" -E -P -B -c " in line for line in probe_lines)
    launch = _detached_launch(_REBUILD_BODY_COMMIT)
    assert launch.startswith('"$GRAPHIFY_PYTHON" -E -P -B -c "')


def test_hook_discovery_uses_no_path_resolved_parser_or_symlink_helpers():
    """Interpreter provenance must be established without ambient helper tools."""
    from graphify.hooks import _PYTHON_DETECT

    assert "command -v readlink" not in _PYTHON_DETECT
    assert "$(readlink " not in _PYTHON_DETECT
    assert "head -" not in _PYTHON_DETECT
    assert "tr -" not in _PYTHON_DETECT
    assert "sed '" not in _PYTHON_DETECT
    assert "/usr/bin/readlink" in _PYTHON_DETECT
    assert "/bin/readlink" in _PYTHON_DETECT


def test_hostile_path_resolver_helpers_are_never_executed(tmp_path):
    """PATH sentinels cannot intercept shebang or symlink inspection."""
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    hostile = tmp_path / "hostile-bin"
    workspace.mkdir()
    hostile.mkdir()
    launcher = hostile / "launcher"
    launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    launcher.chmod(0o755)
    (hostile / "graphify").symlink_to(launcher)
    sentinel = tmp_path / "ambient-helper-ran"
    for helper in ("head", "tr", "sed", "readlink"):
        path = hostile / helper
        path.write_text('#!/bin/sh\n: > "$SENTINEL"\nexit 99\n', encoding="utf-8")
        path.chmod(0o755)

    script = _PYTHON_DETECT.replace("__PINNED_PYTHON__", "")
    result = subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=workspace,
        env={
            **os.environ,
            "PATH": f"{hostile}:/usr/bin:/bin",
            "SENTINEL": str(sentinel),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert not sentinel.exists()


@pytest.mark.parametrize("command", ["graphify", "python3", "python"])
@pytest.mark.parametrize("selected_root", ["input", "output"])
def test_hook_dynamic_fallback_rejects_explicit_external_roots(
    tmp_path, command, selected_root
):
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controlled = tmp_path / f"controlled-{selected_root}"
    bin_dir = controlled / "bin"
    bin_dir.mkdir(parents=True)
    marker = tmp_path / f"hook-{selected_root}-{command}-ran"
    candidate = bin_dir / command
    candidate.write_text(
        f"#!/bin/sh\n: > '{marker}'\nexec '{sys.executable}' \"$@\"\n",
        encoding="utf-8",
    )
    candidate.chmod(0o755)
    selected_alias = tmp_path / f"hook-selected-{selected_root}"
    selected_alias.symlink_to(controlled, target_is_directory=True)
    other = tmp_path / f"hook-other-{selected_root}"
    other.mkdir()
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "GRAPHIFY_INPUT_PATH": str(selected_alias if selected_root == "input" else other),
        "GRAPHIFY_OUTPUT_ROOT": str(selected_alias if selected_root == "output" else other),
    }
    script = _PYTHON_DETECT.replace("__PINNED_PYTHON__", "")

    result = subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert result.returncode == 0
    assert not marker.exists(), f"hook executed {selected_root}-controlled {command}"


@pytest.mark.parametrize("selected_root", ["input", "output"])
def test_hook_root_deny_rejects_every_dynamic_absolute_path(tmp_path, selected_root):
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bin_dir = tmp_path / "ambient-bin"
    bin_dir.mkdir()
    marker = tmp_path / f"hook-root-{selected_root}-ran"
    candidate = bin_dir / "python3"
    candidate.write_text(
        f"#!/bin/sh\n: > '{marker}'\nexec '{sys.executable}' \"$@\"\n",
        encoding="utf-8",
    )
    candidate.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir)}
    env["GRAPHIFY_INPUT_PATH" if selected_root == "input" else "GRAPHIFY_OUTPUT_ROOT"] = "/"
    script = _PYTHON_DETECT.replace("__PINNED_PYTHON__", "")

    result = subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert result.returncode == 0
    assert not marker.exists()


def test_hook_external_lexical_venv_symlink_preserves_invocation_semantics(tmp_path):
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = tmp_path / "hook-external-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        check=True,
        capture_output=True,
    )
    candidate = environment / "bin" / "python3"
    assert candidate.is_symlink()
    site_packages = Path(
        subprocess.check_output(
            [str(candidate), "-E", "-P", "-B", "-c", "import site; print(site.getsitepackages()[0])"],
            text=True,
        ).strip()
    )
    marker = tmp_path / "hook-lexical-venv-imported"
    package = site_packages / "graphify"
    package.mkdir()
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    dist_info = site_packages / "graphifyy-0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: graphifyy\nVersion: 0\n",
        encoding="utf-8",
    )
    (dist_info / "RECORD").write_text("graphify/__init__.py,,\n", encoding="utf-8")
    canonical = candidate.resolve()
    marker.unlink(missing_ok=True)
    subprocess.run(
        [str(canonical), "-E", "-P", "-B", "-c", "import graphify"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    assert not marker.exists()
    script = (
        _PYTHON_DETECT.replace("__PINNED_PYTHON__", "")
        + '\n[ -n "$GRAPHIFY_PYTHON" ] && "$GRAPHIFY_PYTHON" -E -P -B -c \'import graphify\'\n'
    )

    result = subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=workspace,
        env={**os.environ, "PATH": str(candidate.parent)},
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()


def test_hook_corpus_local_symlink_to_external_interpreter_is_rejected(tmp_path):
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    corpus = tmp_path / "corpus"
    bin_dir = corpus / "bin"
    bin_dir.mkdir(parents=True)
    marker = tmp_path / "hook-corpus-symlink-ran"
    target = tmp_path / "hook-external-python"
    target.write_text(
        f"#!/bin/sh\n: > '{marker}'\nexec '{sys.executable}' \"$@\"\n",
        encoding="utf-8",
    )
    target.chmod(0o755)
    (bin_dir / "python3").symlink_to(target)
    script = _PYTHON_DETECT.replace("__PINNED_PYTHON__", "")

    result = subprocess.run(
        ["/bin/sh", "-c", script],
        cwd=workspace,
        env={**os.environ, "PATH": str(bin_dir), "GRAPHIFY_INPUT_PATH": str(corpus)},
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert result.returncode == 0
    assert not marker.exists()


def test_hook_check_no_additionalContext(tmp_path):
    """graphify hook-check must not emit additionalContext — Codex Desktop rejects it."""
    import sys
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text("{}", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "graphify", "hook-check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


# ── #1161: background rebuild must not rely on nohup (missing on Git for Windows) ──

import ast  # noqa: E402
import re  # noqa: E402

from graphify.hooks import (  # noqa: E402
    _HOOK_SCRIPT,
    _CHECKOUT_SCRIPT,
    _POST_MERGE_SCRIPT,
    _REBUILD_BODY_COMMIT,
    _REBUILD_BODY_CHECKOUT,
    _detached_launch,
)

_HOOK_SCRIPTS = [
    ("post-commit", _HOOK_SCRIPT),
    ("post-checkout", _CHECKOUT_SCRIPT),
    ("post-merge", _POST_MERGE_SCRIPT),
]
_WORKTREE_SKIPPING_HOOK_SCRIPTS = [
    ("post-commit", _HOOK_SCRIPT),
    ("post-checkout", _CHECKOUT_SCRIPT),
]


@pytest.mark.parametrize("name,script", _HOOK_SCRIPTS)
def test_hooks_do_not_use_nohup(name, script):
    """Git for Windows' bundled shell ships no `nohup`/`setsid`, so the old
    `nohup ... &` launch died with 'nohup: command not found' and the rebuild
    silently never ran (#1161). The generated hooks must not reference either."""
    assert "nohup" not in script, f"{name} still references nohup (#1161)"
    assert "setsid" not in script, f"{name} still references setsid (#1161)"
    assert "disown" not in script, f"{name} still uses disown (#1161)"


@pytest.mark.parametrize("name,script", _HOOK_SCRIPTS)
def test_hooks_use_cross_platform_detach(name, script):
    """The replacement detaches via Python: start_new_session on POSIX and
    DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP on Windows (#1161)."""
    assert "subprocess.Popen" in script
    assert "start_new_session=True" in script, f"{name} missing POSIX detach"
    assert "0x00000008" in script, f"{name} missing Windows DETACHED_PROCESS flag"
    assert "0x00000200" in script, f"{name} missing CREATE_NEW_PROCESS_GROUP flag"


@pytest.mark.parametrize("name,script", _HOOK_SCRIPTS)
def test_hooks_limit_windows_workers_by_default(name, script):
    """Git for Windows/MSYS hooks can expose fragile pipe handles to spawned
    ProcessPoolExecutor children. Hook-triggered rebuilds should default to one
    worker there, while still allowing explicit user overrides."""
    assert '[ -n "${WINDIR:-}" ] || [ -n "${MSYSTEM:-}" ]' in script
    assert 'export GRAPHIFY_MAX_WORKERS="${GRAPHIFY_MAX_WORKERS:-1}"' in script


def _launcher_payload(script: str) -> str:
    """Extract the `python -c "<payload>"` the hook hands to GRAPHIFY_PYTHON.

    The launcher is the only `-c` invocation whose body begins with
    `import os, subprocess, sys` (the interpreter-detection probes in
    _PYTHON_DETECT use `-c "$_GFY_PROBE"`)."""
    m = re.search(r'-c "(import os, subprocess, sys.*?)"\n', script, re.DOTALL)
    assert m, "launcher payload not found"
    return m.group(1)


@pytest.mark.parametrize("name,script", _HOOK_SCRIPTS)
def test_launcher_payload_is_shell_quote_safe(name, script):
    """The launcher is carried inside a shell double-quoted `-c "..."` argument,
    so it must contain no characters the shell would interpret there: an
    unescaped double-quote, $, backtick or backslash would corrupt the hook."""
    payload = _launcher_payload(script)
    for bad in ('"', "$", "`", "\\"):
        assert bad not in payload, f"{name} launcher payload contains unsafe {bad!r}"


@pytest.mark.parametrize("name,script", _HOOK_SCRIPTS)
def test_launcher_and_rebuild_body_are_valid_python(name, script):
    """Both the launcher and the rebuild body it re-executes must parse, so a
    quoting slip can't ship a hook that crashes the moment git fires it."""
    payload = _launcher_payload(script)
    ast.parse(payload)  # launcher itself
    inner = re.search(r"_src = '''(.*?)'''", payload, re.DOTALL)
    assert inner, f"{name}: embedded rebuild body not found"
    ast.parse(inner.group(1))  # the detached child's source


def test_rebuild_bodies_are_shell_quote_safe():
    """The shared rebuild bodies are embedded verbatim into the launcher, so they
    too must avoid characters unsafe inside a shell double-quoted argument."""
    for body in (_REBUILD_BODY_COMMIT, _REBUILD_BODY_CHECKOUT):
        for bad in ('"', "$", "`", "\\"):
            assert bad not in body
        assert "'''" not in body  # would terminate the launcher's _src literal


@pytest.mark.parametrize(
    "name,body",
    [("post-commit", _REBUILD_BODY_COMMIT), ("post-checkout", _REBUILD_BODY_CHECKOUT)],
)
def test_rebuild_bodies_read_graphify_root(name, body):
    """The rebuild must honour the persisted scan root rather than hardcoding the
    repo top (#1173). Both bodies read <output-dir>/.graphify_root and pass the
    recovered root to _rebuild_code instead of the bare Path('.')."""
    assert ".graphify_root" in body, f"{name} ignores .graphify_root (#1173)"
    # The output dir is resolved from GRAPHIFY_OUT at hook-run time, not hardcoded
    # to graphify-out/, so a renamed output dir is still found (#1423).
    assert "GRAPHIFY_OUT" in body, f"{name} ignores the GRAPHIFY_OUT override (#1423)"
    # The recovered root is what gets rebuilt, not a hardcoded cwd.
    assert "_rebuild_code(_root" in body, f"{name} does not pass the recovered root"
    # Quote-safe inside the shell-double-quoted launcher: single quotes only.
    assert "read_text(encoding='utf-8')" in body, f"{name} root read is not single-quoted"


def test_rebuild_bodies_with_graphify_root_are_valid_python():
    """The .graphify_root snippet must parse so a quoting slip can't ship a hook
    that crashes the moment git fires it (#1173)."""
    for body in (_REBUILD_BODY_COMMIT, _REBUILD_BODY_CHECKOUT):
        ast.parse(body)


def test_detached_launch_targets_graphify_python():
    """The launcher must run via the resolved $GRAPHIFY_PYTHON, not a bare
    `python`, so it uses the same interpreter the detection block selected."""
    snippet = _detached_launch(_REBUILD_BODY_COMMIT)
    assert snippet.startswith('"$GRAPHIFY_PYTHON" -E -P -B -c "')
    assert "nohup" not in snippet


def test_installed_hooks_contain_no_nohup(tmp_path):
    """End-to-end: the files written to .git/hooks must be nohup-free (#1161)."""
    repo = _make_git_repo(tmp_path)
    install(repo)
    for name in ("post-commit", "post-checkout", "post-merge"):
        text = (repo / ".git" / "hooks" / name).read_text(encoding="utf-8")
        assert "nohup" not in text, f"installed {name} still references nohup"
        assert "start_new_session=True" in text


# ── #1385: reject Windows-style hooks paths instead of creating a junk dir ───

def _set_hookspath(repo: Path, value: str) -> None:
    subprocess.run(["git", "-C", str(repo), "config", "--local", "core.hooksPath", value],
                   check=True, capture_output=True)


@pytest.mark.parametrize("winpath", [
    r"C:\Users\u\repo\.git\hooks",
    r"c:/Users/u/.git/hooks",
    r"D:\hooks",
    r"some\back\slashed\path",
])
def test_windows_hookspath_rejected_no_junk_dir_on_posix(tmp_path, monkeypatch, winpath):
    """A Windows-style core.hooksPath must raise (loud failure), not silently
    create a backslash-named junk directory and report success on POSIX/WSL (#1385)."""
    monkeypatch.setattr("graphify.hooks.os.name", "posix")
    repo = _make_git_repo(tmp_path)
    _set_hookspath(repo, winpath)
    with pytest.raises(RuntimeError, match="Windows path"):
        install(repo)
    # no junk directory got created anywhere under the repo
    junk = [p for p in repo.rglob("*") if "\\" in p.name or p.name.startswith(("C:", "c:", "D:"))]
    assert junk == [], f"junk dir created: {junk}"


def test_posix_custom_hookspath_still_works(tmp_path):
    """A legitimate POSIX core.hooksPath (Husky-style) must still install."""
    repo = _make_git_repo(tmp_path)
    _set_hookspath(repo, ".husky")
    msg = install(repo)
    assert "post-commit" in msg
    assert (repo / ".husky" / "post-commit").exists()


def test_default_hooks_dir_unaffected(tmp_path):
    """No core.hooksPath -> normal .git/hooks install, no rejection."""
    repo = _make_git_repo(tmp_path)
    install(repo)
    assert (repo / ".git" / "hooks" / "post-commit").exists()


# ── foreground hook cost: probes must be cheap and quiet ─────────────────────

def test_probes_use_find_spec_not_full_import():
    """`python -c "import graphify"` executes the FULL package import — 10s+ on a
    cold cache or AV-scanned site-packages — and could run up to four times
    synchronously before the detached launch even started, so every commit
    stalled for tens of seconds. Probes must locate the package with
    importlib.util.find_spec (no execution); the detached rebuild still reports
    a broken install loudly in its log."""
    from graphify.hooks import _PYTHON_DETECT
    assert '-c "import graphify"' not in _PYTHON_DETECT, (
        "interpreter probe still imports the full package in the hook foreground"
    )
    assert "find_spec" in _PYTHON_DETECT


def test_shebang_read_is_null_byte_safe():
    """On Windows, `command -v graphify` can return the launcher path WITHOUT its
    .exe suffix, so the `*.exe)` guard misses and the shebang probe reads a
    BINARY: the shell then warns 'ignored null byte in input' if binary bytes go
    through command substitution. Use the shell read builtin and accept only a
    real #! line, without any PATH-resolved parsing process."""
    from graphify.hooks import _PYTHON_DETECT
    assert 'IFS= read -r _GFY_FIRST_LINE < "$GRAPHIFY_BIN"' in _PYTHON_DETECT
    assert "_SHEBANG=$(" not in _PYTHON_DETECT
    assert "'#'!*)" in _PYTHON_DETECT


def test_probe_prefers_sibling_python_exe_on_windows_layouts():
    """pip on Windows puts Scripts/graphify(.exe) beside ..\\python.exe (or
    .\\python.exe in a venv). Resolving that directly beats shebang-parsing a
    binary launcher — and works whether or not command -v kept the suffix."""
    from graphify.hooks import _PYTHON_DETECT
    assert "/../python.exe" in _PYTHON_DETECT
    assert "/python.exe" in _PYTHON_DETECT


@pytest.mark.parametrize("name,script", _HOOK_SCRIPTS)
def test_hooks_reuse_git_dir_from_env(name, script):
    """git exports GIT_DIR to hooks, so the rev-parse fallback should only run
    when the script is invoked by hand — each extra git exec costs 1s+ on
    AV-scanned Windows machines and lands in the commit's foreground."""
    assert "GIT_DIR=${GIT_DIR:-" in script, f"{name} always re-runs git rev-parse"


@pytest.mark.parametrize("name,script", _HOOK_SCRIPTS)
def test_hooks_honor_skip_env(name, script):
    """GRAPHIFY_SKIP_HOOK=1 must suppress BOTH hooks. post-checkout previously
    lacked the check, so the var stopped commit rebuilds but not branch-switch
    ones (#1809)."""
    assert '[ "${GRAPHIFY_SKIP_HOOK:-0}" = "1" ] && exit 0' in script, (
        f"{name} does not honor GRAPHIFY_SKIP_HOOK"
    )


@pytest.mark.parametrize("name,script", _WORKTREE_SKIPPING_HOOK_SCRIPTS)
def test_hooks_skip_linked_worktrees(name, script):
    """Ordinary rebuild hooks must short-circuit in a linked worktree,
    and must compare ABSOLUTE paths so the primary checkout (where --git-common-dir
    is the relative ".git") is not false-positived and wrongly skipped (#1809, #1806)."""
    assert script.count("_GFY_GITDIR=") == 1, f"{name} guard not present exactly once"
    assert "git rev-parse --git-common-dir" in script
    # absolute-normalized compare, not a raw string compare of git output
    assert 'cd "$(git rev-parse --git-dir 2>/dev/null)" 2>/dev/null && pwd' in script
    assert '[ "$_GFY_GITDIR" != "$_GFY_COMMONDIR" ]' in script


def test_post_merge_hook_only_skips_active_linked_worktrees():
    assert "git rev-parse --git-common-dir" in _POST_MERGE_SCRIPT
    assert 'w.get("state") == "merge_pending"' in _POST_MERGE_SCRIPT


def _worktree_guard_snippet() -> str:
    from graphify.hooks import _WORKTREE_GUARD
    return _WORKTREE_GUARD + "echo RAN\n"


def test_worktree_guard_runs_on_primary_skips_linked(tmp_path):
    """End-to-end against a real `git worktree`: the guard falls through on the
    primary checkout and exits early inside a linked worktree (#1809, #1806)."""
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git not available")
    primary = tmp_path / "primary"
    primary.mkdir()

    def _git(*args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)

    _git("init", "-q", ".", cwd=primary)
    _git("config", "user.email", "t@t.co", cwd=primary)
    _git("config", "user.name", "t", cwd=primary)
    (primary / "a.txt").write_text("x")
    _git("add", "-A", cwd=primary)
    _git("commit", "-qm", "init", cwd=primary)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", str(linked), "-b", "feature", cwd=primary)

    snippet = _worktree_guard_snippet()
    r_primary = subprocess.run(["sh", "-c", snippet], cwd=primary,
                               capture_output=True, text=True)
    r_linked = subprocess.run(["sh", "-c", snippet], cwd=linked,
                              capture_output=True, text=True)
    assert "RAN" in r_primary.stdout, "guard wrongly skipped the primary checkout"
    assert "RAN" not in r_linked.stdout, "guard failed to skip the linked worktree"


# ── #1907: duplicate keys in .git/config must not trigger spurious warnings ──

def _append_duplicate_config_entries(repo: Path) -> None:
    """Append git-legal duplicate keys/sections (as VS Code writes them)."""
    cfg = repo / ".git" / "config"
    cfg.write_text(
        cfg.read_text(encoding="utf-8")
        + '[remote "origin"]\n'
        + "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        + "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        + "[core]\n"
        + "\tignorecase = true\n",
        encoding="utf-8",
    )


def test_hooks_dir_no_warning_on_duplicate_config_keys(tmp_path, capsys):
    """git legally allows duplicate keys and repeated sections in .git/config;
    a strict configparser raised DuplicateOptionError/DuplicateSectionError and
    printed a spurious 'could not read core.hooksPath' warning on every hook
    command (#1907). _hooks_dir must resolve cleanly with no stderr noise."""
    repo = _make_git_repo(tmp_path)
    _append_duplicate_config_entries(repo)
    d = _hooks_dir(repo)
    err = capsys.readouterr().err
    assert "could not read core.hooksPath" not in err
    assert d == (repo / ".git" / "hooks").resolve()


def test_hooks_dir_duplicate_config_keys_honor_custom_hookspath(tmp_path, capsys):
    """With duplicate keys present, a custom core.hooksPath must still be
    honored (no fall-through to .git/hooks) and no warning printed (#1907)."""
    repo = _make_git_repo(tmp_path)
    _set_hookspath(repo, ".husky")
    _append_duplicate_config_entries(repo)
    d = _hooks_dir(repo)
    err = capsys.readouterr().err
    assert "could not read core.hooksPath" not in err
    assert d == (repo / ".husky").resolve()


# ── #1902: hook install must register the graph.json union merge driver ─────

def test_install_registers_merge_driver(tmp_path):
    """install() must set merge.graphify.* via git config and add the
    .gitattributes line that README/CHANGELOG 0.7.0 document (#1902)."""
    repo = _make_git_repo(tmp_path)
    result = install(repo)
    res = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "merge.graphify.driver"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0
    driver = res.stdout.strip()
    assert driver
    assert (
        "merge-driver --managed-current graphify-out/graph.json %O %A %B"
        in driver
    )
    attrs = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert any(
        "graph.json" in line and "merge=graphify" in line
        for line in attrs.splitlines()
    )
    assert "merge driver" in result


def test_install_merge_driver_idempotent(tmp_path):
    """Running install twice must not duplicate the .gitattributes line."""
    repo = _make_git_repo(tmp_path)
    install(repo)
    install(repo)
    lines = (repo / ".gitattributes").read_text(encoding="utf-8").splitlines()
    matches = [l for l in lines if "merge=graphify" in l]
    assert len(matches) == 1


def test_install_rebinds_merge_driver_when_output_changes(tmp_path, monkeypatch):
    """Reinstall must migrate every merge surface to the configured output."""
    import graphify.paths as paths_module

    repo = _make_git_repo(tmp_path)
    install(repo)

    monkeypatch.setattr(paths_module, "GRAPHIFY_OUT", "custom-out")
    result = install(repo)

    attrs = (repo / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "graphify-out/graph.json merge=graphify" not in attrs
    assert attrs.count("custom-out/graph.json merge=graphify") == 1
    driver = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "merge.graphify.driver"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "--managed-current custom-out/graph.json %O %A %B" in driver
    for hook_name in ("post-commit", "post-checkout", "post-merge"):
        hook = (repo / ".git" / "hooks" / hook_name).read_text(encoding="utf-8")
        assert "GRAPHIFY_OUT=custom-out\nexport GRAPHIFY_OUT\n" in hook
        assert "__GRAPHIFY_OUTPUT__" not in hook
    assert "merge driver: registered" in result
    assert "merge driver: registered" in status(repo)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell quoting contract")
def test_install_preserves_absolute_output_in_recovery_hooks(tmp_path, monkeypatch):
    """Hook runtime identity must not reuse the repo-relative merge fallback."""
    import graphify.paths as paths_module

    repo = _make_git_repo(tmp_path / "repo")
    shared_output = tmp_path / "shared output"
    monkeypatch.setattr(paths_module, "GRAPHIFY_OUT", str(shared_output))

    install(repo)

    expected = f"GRAPHIFY_OUT={shlex.quote(str(shared_output))}\nexport GRAPHIFY_OUT\n"
    for hook_name in ("post-commit", "post-checkout", "post-merge"):
        hook = repo / ".git" / "hooks" / hook_name
        content = hook.read_text(encoding="utf-8")
        assert expected in content
        assert "GRAPHIFY_OUT=graphify-out\nexport GRAPHIFY_OUT\n" not in content
        syntax = subprocess.run(
            ["/bin/sh", "-n", str(hook)], capture_output=True, text=True
        )
        assert syntax.returncode == 0, syntax.stderr

    post_commit = (repo / ".git" / "hooks" / "post-commit").read_text(
        encoding="utf-8"
    )
    assert "GRAPHIFY_REPO_OUTPUT" not in post_commit
    assert "literal,exclude" not in post_commit
    assert (repo / ".gitattributes").read_text(encoding="utf-8") == (
        "graphify-out/graph.json merge=graphify\n"
    )
    driver = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "merge.graphify.driver"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "--managed-current graphify-out/graph.json %O %A %B" in driver


def test_merge_driver_recursive_config_lifecycle(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")

    install(repo)

    def config_get(key: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), "config", "--get", key],
            capture_output=True,
            text=True,
        )

    recursive = config_get("merge.graphify.recursive")
    assert recursive.returncode == 0
    assert recursive.stdout.strip() == "binary"
    assert "merge driver: registered" in status(repo)

    subprocess.run(
        ["git", "-C", str(repo), "config", "merge.graphify.recursive", "text"],
        check=True,
    )
    assert "merge driver: partially registered" in status(repo)

    install(repo)
    assert config_get("merge.graphify.recursive").stdout.strip() == "binary"

    uninstall(repo)
    for key in (
        "merge.graphify.name",
        "merge.graphify.driver",
        "merge.graphify.recursive",
    ):
        assert config_get(key).returncode != 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell quoting contract")
def test_install_quotes_custom_output_in_recovery_hooks(tmp_path, monkeypatch):
    """Output configuration is data, never executable hook syntax."""
    import graphify.paths as paths_module

    repo = _make_git_repo(tmp_path)
    sentinel = repo / "custom-output-injected"
    output_name = f"custom'; touch {sentinel}; #"
    monkeypatch.setattr(paths_module, "GRAPHIFY_OUT", output_name)

    install(repo)

    expected = f"GRAPHIFY_OUT={shlex.quote(output_name)}\nexport GRAPHIFY_OUT\n"
    for hook_name in ("post-commit", "post-merge"):
        hook = repo / ".git" / "hooks" / hook_name
        content = hook.read_text(encoding="utf-8")
        assert expected in content
        syntax = subprocess.run(
            ["/bin/sh", "-n", str(hook)], capture_output=True, text=True
        )
        assert syntax.returncode == 0, syntax.stderr
        probe = subprocess.run(
            ["/bin/sh", str(hook)],
            cwd=repo,
            env={**os.environ, "GRAPHIFY_SKIP_HOOK": "1"},
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0, probe.stderr
    assert not sentinel.exists()


def _run_installed_post_commit_guard(repo: Path) -> subprocess.CompletedProcess[str]:
    hook = (repo / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    guard, separator, _remainder = hook.partition(
        "# Detect a trusted Python interpreter"
    )
    assert separator
    return subprocess.run(
        ["/bin/sh", "-c", guard + "printf 'REACHED_LAUNCH_BOUNDARY\\n'\n"],
        cwd=repo,
        env={
            name: value
            for name, value in os.environ.items()
            if name not in {"GRAPHIFY_OUT", "GRAPHIFY_OUTPUT_ROOT", "GRAPHIFY_SKIP_HOOK"}
        },
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX installed-hook guard contract")
@pytest.mark.parametrize(
    ("output_name", "changed_paths", "reaches_launch"),
    [
        (
            "custom-out",
            ("custom-out/graph.json", "custom-out/nested/manifest.json"),
            False,
        ),
        ("custom-out", ("custom-outside/module.py",), True),
        ("custom-out", ("src/custom-out/module.py",), True),
        ("custom-out", ("custom-out/graph.json", "src/module.py"), True),
        ("custom';touch-injected;#", ("custom';touch-injected;#/graph.json",), False),
        ("gráph-out", ("gráph-out/graph.json",), False),
    ],
    ids=[
        "output-only",
        "similar-prefix",
        "nested-source",
        "mixed",
        "shell-significant-output",
        "non-ascii-output",
    ],
)
def test_installed_post_commit_filters_only_configured_output_descendants(
    tmp_path, monkeypatch, output_name, changed_paths, reaches_launch
):
    import graphify.paths as paths_module

    repo = _make_git_repo(tmp_path / "repo")
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "hook-tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Graphify Hook Tests"],
        check=True,
    )
    monkeypatch.setattr(paths_module, "GRAPHIFY_OUT", output_name)
    install(repo)
    hook = repo / ".git" / "hooks" / "post-commit"
    syntax = subprocess.run(["/bin/sh", "-n", str(hook)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    (repo / "baseline.txt").write_text("base\n", encoding="utf-8")
    setup_env = {**os.environ, "GRAPHIFY_SKIP_HOOK": "1"}
    subprocess.run(
        ["git", "-C", str(repo), "add", ".gitattributes", "baseline.txt"],
        check=True,
        env=setup_env,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "baseline"],
        check=True,
        capture_output=True,
        env=setup_env,
    )
    for relative in changed_paths:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "--", *changed_paths],
        check=True,
        env=setup_env,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "scenario"],
        check=True,
        capture_output=True,
        env=setup_env,
    )

    result = _run_installed_post_commit_guard(repo)

    assert result.returncode == 0, result.stderr
    assert ("REACHED_LAUNCH_BOUNDARY" in result.stdout) is reaches_launch
    assert not (repo / "touch-injected").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook mode contract")
def test_install_repairs_existing_post_merge_execute_bits_without_mode_loss(tmp_path):
    repo = _make_git_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-merge"
    user_content = b"#!/bin/sh\nprintf 'user hook\\n'\n"
    original_mode = 0o2640
    hook.write_bytes(user_content)
    hook.chmod(original_mode)

    first = install(repo)

    installed = hook.read_bytes()
    assert "appended to existing post-merge" in first
    assert installed.startswith(user_content.rstrip() + b"\n\n")
    assert installed.count(_POST_MERGE_MARKER.encode()) == 1
    assert stat.S_IMODE(hook.stat().st_mode) == original_mode | 0o111

    hook.chmod(original_mode)
    second = install(repo)
    assert "post-merge: already installed" in second
    assert hook.read_bytes() == installed
    assert stat.S_IMODE(hook.stat().st_mode) == original_mode | 0o111

    rendered = _rendered_hook(hooks._POST_MERGE_SCRIPT)
    stale = (
        f"{_POST_MERGE_MARKER}\n# stale post-merge body\n"
        f"{_POST_MERGE_MARKER_END}\n"
    ).encode()
    hook.write_bytes(installed.replace(rendered, stale))
    hook.chmod(original_mode)
    third = install(repo)
    assert "updated post-merge hook" in third
    assert hook.read_bytes() == installed
    assert stat.S_IMODE(hook.stat().st_mode) == original_mode | 0o111


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook object contract")
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_install_rejects_non_regular_post_merge_before_mutation(tmp_path, kind):
    repo = _make_git_repo(tmp_path / "repo")
    hooks_dir = repo / ".git" / "hooks"
    post_merge = hooks_dir / "post-merge"
    target = tmp_path / "external-post-merge"
    target.write_bytes(b"external hook\n")
    if kind == "symlink":
        post_merge.symlink_to(target)
    else:
        os.mkfifo(post_merge)

    with pytest.raises(RuntimeError, match="Unsafe non-regular post-merge hook"):
        install(repo)

    assert target.read_bytes() == b"external hook\n"
    assert not (hooks_dir / "post-commit").exists()
    assert not (hooks_dir / "post-checkout").exists()
    assert not (repo / ".gitattributes").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook object contract")
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_uninstall_rejects_non_regular_post_merge_before_mutation(tmp_path, kind):
    repo = _make_git_repo(tmp_path / "repo")
    install(repo)
    hooks_dir = repo / ".git" / "hooks"
    post_merge = hooks_dir / "post-merge"
    post_merge.unlink()
    target = tmp_path / "external-post-merge"
    target.write_bytes(b"external hook\n")
    if kind == "symlink":
        post_merge.symlink_to(target)
    else:
        os.mkfifo(post_merge)
    commit_before = (hooks_dir / "post-commit").read_bytes()
    checkout_before = (hooks_dir / "post-checkout").read_bytes()

    with pytest.raises(RuntimeError, match="Unsafe non-regular post-merge hook"):
        uninstall(repo)

    assert target.read_bytes() == b"external hook\n"
    assert (hooks_dir / "post-commit").read_bytes() == commit_before
    assert (hooks_dir / "post-checkout").read_bytes() == checkout_before


def test_install_preserves_existing_gitattributes(tmp_path):
    """A pre-existing .gitattributes entry must survive install (no clobber)."""
    repo = _make_git_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
    install(repo)
    content = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert "*.png binary" in content
    assert "merge=graphify" in content


def test_uninstall_removes_merge_driver_keeps_other_attrs(tmp_path):
    """uninstall() must unset merge.graphify.* and remove only the graphify
    .gitattributes line, keeping the file when other entries exist."""
    repo = _make_git_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
    install(repo)
    uninstall(repo)
    res = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "merge.graphify.driver"],
        capture_output=True, text=True,
    )
    assert res.returncode != 0
    content = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert "*.png binary" in content
    assert "merge=graphify" not in content


# ── PR #90: interpreter pins and ambient package origins are data, not shell ──

_SPECIAL_PIN_NAMES = (
    "python with spaces",
    "python$cash",
    "python;semi",
    "python(paren)",
    "python[brackets]",
    "python'quote",
)


@pytest.mark.parametrize("name", _SPECIAL_PIN_NAMES)
def test_pinned_python_preserves_valid_absolute_special_paths(tmp_path, monkeypatch, name):
    """An absolute interpreter path is shell data; punctuation is not invalidity."""
    import graphify.hooks as hooks

    pinned = str(tmp_path / name)
    monkeypatch.setattr(hooks.sys, "executable", pinned)

    assert hooks._pinned_python() == pinned
    assert shlex.split(shlex.quote(hooks._pinned_python())) == [pinned]


@pytest.mark.parametrize(
    "value", ["python", "bin/python", "./python", "../python", "C:python", r"C:bin\python.exe"]
)
def test_pinned_python_still_rejects_relative_paths(monkeypatch, value):
    import graphify.hooks as hooks

    monkeypatch.setattr(hooks.sys, "executable", value)

    assert hooks._pinned_python() == ""


def _write_capturing_executable(path: Path, argv_log: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" >> {shlex.quote(str(argv_log))}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook quoting contract")
def test_installed_hooks_quote_special_pin_and_execute_exact_token(tmp_path, monkeypatch):
    """The installed hook must neither split nor evaluate the trusted pin."""
    import graphify.hooks as hooks

    repo = _make_git_repo(tmp_path / "repo")
    sentinel = repo / "hook-pin-injected"
    argv_log = tmp_path / "hook-pin-argv.log"
    pinned = tmp_path / "python'; touch hook-pin-injected; #'"
    _write_capturing_executable(pinned, argv_log)
    monkeypatch.setattr(hooks, "_pinned_python", lambda: str(pinned))

    install(repo)
    (repo / "graphify-out").mkdir()
    for hook_name in ("post-commit", "post-checkout", "post-merge"):
        hook = repo / ".git" / "hooks" / hook_name
        syntax = subprocess.run(["/bin/sh", "-n", str(hook)], capture_output=True, text=True)
        assert syntax.returncode == 0, syntax.stderr

    result = subprocess.run(
        ["/bin/sh", str(repo / ".git" / "hooks" / "post-checkout"), "old", "new", "1"],
        cwd=repo,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    assert argv_log.exists(), "the exact pinned executable was not selected"
    assert argv_log.read_text(encoding="utf-8").splitlines()[:4] == ["-E", "-P", "-B", "-c"]


@pytest.mark.skipif(os.name == "nt", reason="Git invokes merge drivers through POSIX sh here")
def test_configured_merge_driver_keeps_special_executable_and_arguments_distinct(
    tmp_path, monkeypatch
):
    """Exercise Git's stored driver command without a brittle content merge.

    Git itself expands ``%O/%A/%B`` before passing the stored command to the
    shell. Invoking the configured value through that same shell boundary keeps
    this focused on executable quoting and argv separation; a full graph merge
    would additionally couple the regression to graph fixture validity.
    """
    import graphify.hooks as hooks

    repo = _make_git_repo(tmp_path / "repo")
    argv_log = tmp_path / "merge-driver-argv.log"
    sentinel = repo / "merge-driver-injected"
    pinned = tmp_path / "capture; touch merge-driver-injected; #"
    _write_capturing_executable(pinned, argv_log)
    monkeypatch.setattr(hooks, "_pinned_python", lambda: str(pinned))
    install(repo)
    driver = subprocess.check_output(
        ["git", "-C", str(repo), "config", "--get", "merge.graphify.driver"],
        text=True,
    ).strip()
    base, current, other = (tmp_path / name for name in ("base graph", "current graph", "other graph"))
    command = driver.replace("%O", shlex.quote(str(base))).replace(
        "%A", shlex.quote(str(current))
    ).replace("%B", shlex.quote(str(other)))

    result = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=repo,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    assert argv_log.read_text(encoding="utf-8").splitlines() == [
        "-E", "-P", "-B", "-m", "graphify", "merge-driver",
        "--managed-current", "graphify-out/graph.json",
        str(base), str(current), str(other),
    ]


@pytest.mark.skipif(os.name == "nt", reason="Git invokes merge drivers through POSIX sh here")
def test_configured_merge_driver_preserves_literal_percent_in_managed_path(
    tmp_path, monkeypatch
):
    """Git placeholders must not rewrite percent text in the managed selector."""
    import graphify.hooks as hooks
    import graphify.paths as paths_module

    repo = _make_git_repo(tmp_path / "repo")
    argv_log = tmp_path / "merge-driver-percent-argv.log"
    pinned = tmp_path / "capture"
    _write_capturing_executable(pinned, argv_log)
    monkeypatch.setattr(hooks, "_pinned_python", lambda: str(pinned))
    monkeypatch.setattr(paths_module, "GRAPHIFY_OUT", "custom%O-out")
    install(repo)
    driver = subprocess.check_output(
        ["git", "-C", str(repo), "config", "--get", "merge.graphify.driver"],
        text=True,
    ).strip()
    base, current, other = (tmp_path / name for name in ("base", "current", "other"))
    command = driver.replace("%O", shlex.quote(str(base))).replace(
        "%A", shlex.quote(str(current))
    ).replace("%B", shlex.quote(str(other)))

    result = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=repo,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert argv_log.read_text(encoding="utf-8").splitlines() == [
        "-E", "-P", "-B", "-m", "graphify", "merge-driver",
        "--managed-current", "custom%O-out/graph.json",
        str(base), str(current), str(other),
    ]


def _isolated_interpreter(tmp_path: Path) -> tuple[Path, Path]:
    environment = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        check=True,
        capture_output=True,
    )
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")
    site_packages = _interpreter_site_packages(interpreter)
    return interpreter, site_packages


def _interpreter_site_packages(interpreter: Path) -> Path:
    return Path(
        subprocess.check_output(
            [
                str(interpreter),
                "-E",
                "-P",
                "-B",
                "-S",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
    )


def _write_executable_pth(site_packages: Path, sentinel: Path) -> None:
    (site_packages / "00-startup-sentinel.pth").write_text(
        f"import pathlib; pathlib.Path({str(sentinel)!r}).touch()\n",
        encoding="utf-8",
    )


def _editable_interpreter(tmp_path: Path, origin: Path) -> Path:
    interpreter, site_packages = _isolated_interpreter(tmp_path)
    (site_packages / "graphify-origin.pth").write_text(str(origin) + "\n", encoding="utf-8")
    dist_info = site_packages / "graphifyy-0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: graphifyy\nVersion: 0\n",
        encoding="utf-8",
    )
    (dist_info / "direct_url.json").write_text(
        json.dumps({"url": origin.as_uri(), "dir_info": {"editable": True}}),
        encoding="utf-8",
    )
    return interpreter


def _wheel_interpreter(
    tmp_path: Path,
    *,
    record_state: str = "valid",
    marker: Path | None = None,
) -> Path:
    interpreter, site_packages = _isolated_interpreter(tmp_path)
    _write_graphify_package(site_packages, marker)
    dist_info = site_packages / "graphifyy-0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: graphifyy\nVersion: 0\n",
        encoding="utf-8",
    )
    record = dist_info / "RECORD"
    record_contents = {
        "valid": "graphify/__init__.py,,\n",
        "empty": "",
        "duplicate": "graphify/__init__.py,,\ngraphify/__init__.py,,\n",
        "unowned": "graphify/other.py,,\n",
    }
    if record_state in record_contents:
        record.write_text(record_contents[record_state], encoding="utf-8")
    elif record_state == "origin-mismatch":
        impostor = tmp_path / "impostor"
        _write_graphify_package(impostor, marker)
        (site_packages / "00-impostor.pth").write_text(
            f"import sys; sys.path.insert(0, {str(impostor)!r})\n",
            encoding="utf-8",
        )
        record.write_text("graphify/__init__.py,,\n", encoding="utf-8")
    elif record_state != "missing":
        raise AssertionError(f"unknown RECORD state: {record_state}")
    return interpreter


def _write_graphify_package(origin: Path, marker: Path | None = None) -> None:
    package = origin / "graphify"
    package.mkdir(parents=True)
    content = "\n"
    if marker is not None:
        content = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    (package / "__init__.py").write_text(content, encoding="utf-8")


def _run_python_detect(
    script: str,
    *,
    cwd: Path,
    interpreter: Path | None = None,
    input_root: Path | None = None,
    output_root: Path | None = None,
    env_overrides: dict[str, str] | None = None,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": str(interpreter.parent) if interpreter else "/nonexistent"}
    if input_root is not None:
        env["GRAPHIFY_INPUT_PATH"] = str(input_root)
    if output_root is not None:
        env["GRAPHIFY_OUTPUT_ROOT"] = str(output_root)
    if env_overrides is not None:
        env.update(env_overrides)
    return subprocess.run(
        ["/bin/sh", "-c", script + '\nprintf \'SELECTED=%s\\n\' "$GRAPHIFY_PYTHON"\n'],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook discovery contract")
def test_trusted_pin_allows_identity_valid_project_local_editable_origin(tmp_path):
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_graphify_package(workspace)
    interpreter = _editable_interpreter(tmp_path / "trusted", workspace)
    script = _PYTHON_DETECT.replace("__PINNED_PYTHON__", str(interpreter))

    result = _run_python_detect(script, cwd=workspace)

    assert result.returncode == 0, result.stderr
    assert f"SELECTED={interpreter}" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook discovery contract")
@pytest.mark.parametrize("marker_prefix", [b"", b"\xef\xbb\xbf"], ids=["no-newline", "utf8-bom"])
@pytest.mark.parametrize("marker_kind", ["posix", "windows-drive", "windows-unc"])
def test_dynamic_hook_discovery_denies_persisted_external_corpus(
    tmp_path, marker_prefix, marker_kind
):
    """A saved corpus root denies lower-authority editable installations."""
    workspace = tmp_path / "workspace"
    graphify_out = workspace / "graphify-out"
    graphify_out.mkdir(parents=True)
    corpus = tmp_path / "external-corpus"
    corpus.mkdir()
    _write_graphify_package(corpus)
    interpreter = _editable_interpreter(tmp_path / "dynamic", corpus)
    from graphify.hooks import _PYTHON_DETECT

    marker_values = {
        "posix": os.fsencode(corpus),
        "windows-drive": br"C:\External Corpus",
        "windows-unc": br"\\server\share\External Corpus",
    }
    (graphify_out / ".graphify_root").write_bytes(
        marker_prefix + marker_values[marker_kind]
    )
    script = _PYTHON_DETECT.replace("__PINNED_PYTHON__", "")
    if marker_kind == "posix":
        result = _run_python_detect(script, cwd=workspace, interpreter=interpreter)
        assert result.returncode == 0, result.stderr
        assert f"SELECTED={interpreter}" not in result.stdout
        return

    candidates = (
        "/usr/bin/cygpath.exe",
        "/usr/bin/cygpath",
        "/bin/cygpath.exe",
        "/bin/cygpath",
    )
    positions = [script.index(candidate) for candidate in candidates]
    assert positions == sorted(positions)
    assert "command -v cygpath" not in script
    converter_log = tmp_path / "hook-converter.log"
    converter = tmp_path / "trusted-hook-cygpath"
    converter.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "mode, value = sys.argv[1], sys.argv[-1]\n"
        "if mode != '-u' or value not in (r'C:\\External Corpus', r'\\\\server\\share\\External Corpus'):\n"
        "    raise SystemExit(2)\n"
        "with open(os.environ['GFY_HOOK_CONVERTER_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(value + '\\n')\n"
        "print(os.environ['GFY_HOOK_CORPUS'])\n",
        encoding="utf-8",
    )
    converter.chmod(0o755)
    ambient_marker = tmp_path / "ambient-hook-cygpath-ran"
    ambient_converter = interpreter.parent / "cygpath"
    _write_capturing_executable(ambient_converter, ambient_marker)
    overrides = {
        "GFY_HOOK_CONVERTER_LOG": str(converter_log),
        "GFY_HOOK_CORPUS": str(corpus),
    }

    for selected in range(len(candidates)):
        controlled = script
        for index, candidate in enumerate(candidates):
            replacement = str(converter) if index == selected else str(tmp_path / f"missing-cygpath-{index}")
            controlled = controlled.replace(candidate, replacement)
        result = _run_python_detect(
            controlled,
            cwd=workspace,
            interpreter=interpreter,
            env_overrides=overrides,
        )
        assert result.returncode == 0, f"{candidates[selected]}: {result.stderr}"
        assert f"SELECTED={interpreter}" not in result.stdout
        assert not ambient_marker.exists()

    assert marker_values[marker_kind].decode() in converter_log.read_text(encoding="utf-8")

    # A trusted conversion can succeed while the resulting POSIX path is stale
    # or not a directory. Those markers are invalid denial data, like a POSIX
    # nonexistent marker, so they must not suppress an otherwise valid dynamic
    # interpreter. This is distinct from conversion failure, which fails closed.
    trusted = script
    for index, candidate in enumerate(candidates):
        replacement = str(converter) if index == 0 else str(tmp_path / f"missing-stale-{index}")
        trusted = trusted.replace(candidate, replacement)
    not_directory = tmp_path / "converted-not-directory"
    not_directory.write_text("not a corpus", encoding="utf-8")
    for label, converted in (
        ("nonexistent", tmp_path / "converted-does-not-exist"),
        ("not-directory", not_directory),
    ):
        stale_result = _run_python_detect(
            trusted,
            cwd=workspace,
            interpreter=interpreter,
            env_overrides={**overrides, "GFY_HOOK_CORPUS": str(converted)},
        )
        assert stale_result.returncode == 0, f"{label}: {stale_result.stderr}"
        assert f"SELECTED={interpreter}" in stale_result.stdout, label
        assert not ambient_marker.exists()

    relative_result = _run_python_detect(
        trusted,
        cwd=workspace,
        interpreter=interpreter,
        env_overrides={**overrides, "GFY_HOOK_CORPUS": "relative/converted-root"},
    )
    assert f"SELECTED={interpreter}" not in relative_result.stdout
    assert not ambient_marker.exists()

    unavailable = script
    for candidate in candidates:
        unavailable = unavailable.replace(candidate, str(tmp_path / "missing-cygpath"))
    unavailable_result = _run_python_detect(
        unavailable,
        cwd=workspace,
        interpreter=interpreter,
        env_overrides=overrides,
    )
    assert f"SELECTED={interpreter}" not in unavailable_result.stdout
    assert not ambient_marker.exists()

    failing_converter = tmp_path / "failing-hook-cygpath"
    failing_converter.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    failing_converter.chmod(0o755)
    failed = script
    for index, candidate in enumerate(candidates):
        replacement = str(failing_converter) if index == 0 else str(tmp_path / f"missing-failed-{index}")
        failed = failed.replace(candidate, replacement)
    failed_result = _run_python_detect(
        failed,
        cwd=workspace,
        interpreter=interpreter,
        env_overrides=overrides,
    )
    assert f"SELECTED={interpreter}" not in failed_result.stdout
    assert not ambient_marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook discovery contract")
def test_persisted_hook_corpus_is_denial_only(tmp_path):
    """Persisted roots can reject dynamic candidates but cannot select authority."""
    from graphify.hooks import _PYTHON_DETECT, _REBUILD_BODY_CHECKOUT, _REBUILD_BODY_COMMIT

    pinned_workspace = tmp_path / "pinned-workspace"
    graphify_out = pinned_workspace / "graphify-out"
    graphify_out.mkdir(parents=True)
    _write_graphify_package(pinned_workspace)
    pinned = _editable_interpreter(tmp_path / "pinned", pinned_workspace)
    os.mkfifo(graphify_out / ".graphify_root")
    pinned_result = _run_python_detect(
        _PYTHON_DETECT.replace("__PINNED_PYTHON__", str(pinned)),
        cwd=pinned_workspace,
    )
    assert pinned_result.returncode == 0, pinned_result.stderr
    assert f"SELECTED={pinned}" in pinned_result.stdout

    corpus = tmp_path / "external-corpus"
    corpus.mkdir()
    _write_graphify_package(corpus)
    dynamic = _editable_interpreter(tmp_path / "dynamic", corpus)
    parity_cases = (
        ("unset", None, Path("graphify-out/.graphify_root")),
        ("empty", "", Path(".graphify_root")),
        ("relative", "out dir", Path("out dir/.graphify_root")),
        ("absolute", str(tmp_path / "absolute out"), tmp_path / "absolute out/.graphify_root"),
    )
    for label, graphify_out_value, marker_path in parity_cases:
        workspace = tmp_path / f"workspace-{label}"
        workspace.mkdir()
        marker = marker_path if marker_path.is_absolute() else workspace / marker_path
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(corpus), encoding="utf-8")
        irrelevant = tmp_path / f"irrelevant-output-{label}"
        irrelevant.mkdir()
        overrides = {"GRAPHIFY_OUTPUT_ROOT": str(irrelevant)}
        if graphify_out_value is not None:
            overrides["GRAPHIFY_OUT"] = graphify_out_value
        result = _run_python_detect(
            _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
            cwd=workspace,
            interpreter=dynamic,
            env_overrides=overrides,
        )
        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert f"SELECTED={dynamic}" not in result.stdout, label
        marker.unlink()

    invalid_workspace = tmp_path / "invalid-workspace"
    invalid_workspace.mkdir()
    invalid_out = invalid_workspace / "graphify-out"
    invalid_out.mkdir()
    marker = invalid_out / ".graphify_root"
    invalid_cases = (
        ("missing", None),
        ("relative", b"relative/path"),
        ("nonexistent", os.fsencode(tmp_path / "does-not-exist")),
        ("metacharacter", b"$(touch " + os.fsencode(tmp_path / "marker-content-ran") + b")"),
        ("extra-line", os.fsencode(corpus) + b"\n" + os.fsencode(tmp_path)),
    )
    for label, content in invalid_cases:
        marker.unlink(missing_ok=True)
        if content is not None:
            marker.write_bytes(content)
        result = _run_python_detect(
            _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
            cwd=invalid_workspace,
            interpreter=dynamic,
        )
        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert f"SELECTED={dynamic}" in result.stdout, label
        assert not (tmp_path / "marker-content-ran").exists()

    marker.unlink(missing_ok=True)
    target = tmp_path / "valid-marker-target"
    target.write_text(str(corpus), encoding="utf-8")
    marker.symlink_to(target)
    symlink_result = _run_python_detect(
        _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
        cwd=invalid_workspace,
        interpreter=dynamic,
        timeout=2,
    )
    assert f"SELECTED={dynamic}" in symlink_result.stdout
    marker.unlink()

    os.mkfifo(marker)
    fifo_result = _run_python_detect(
        _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
        cwd=invalid_workspace,
        interpreter=dynamic,
    )
    assert f"SELECTED={dynamic}" in fifo_result.stdout

    for body in (_REBUILD_BODY_COMMIT, _REBUILD_BODY_CHECKOUT):
        assert "Path(_out) / '.graphify_root'" in body
        assert "_root = Path(_txt)" in body


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook discovery contract")
def test_dynamic_probe_rejects_denied_origins_and_symlink_crossings(tmp_path):
    """Ambient interpreters are rejected when either origin spelling is denied."""
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    external = tmp_path / "external"
    for root in (workspace, input_root, output_root, external):
        root.mkdir()

    cases: list[tuple[str, Path, bool]] = []
    for label, root in (("workspace", workspace), ("input", input_root), ("output", output_root)):
        origin = root / f"{label}-origin"
        _write_graphify_package(origin)
        cases.append((label, origin, False))

    external_origin = external / "editable-origin"
    _write_graphify_package(external_origin)
    cases.append(("external", external_origin, True))

    lexical_inside = workspace / "lexical-inside-real-outside"
    lexical_inside.symlink_to(external_origin, target_is_directory=True)
    cases.append(("lexical-inside", lexical_inside, False))

    real_inside = workspace / "real-inside"
    _write_graphify_package(real_inside)
    lexical_outside = external / "lexical-outside-real-inside"
    lexical_outside.symlink_to(real_inside, target_is_directory=True)
    cases.append(("real-inside", lexical_outside, False))

    for label, origin, accepted in cases:
        interpreter = _editable_interpreter(tmp_path / f"env-{label}", origin)
        result = _run_python_detect(
            _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
            cwd=workspace,
            interpreter=interpreter,
            input_root=input_root,
            output_root=output_root,
        )
        selected = f"SELECTED={interpreter}" in result.stdout
        assert selected is accepted, f"{label}: stdout={result.stdout!r} stderr={result.stderr!r}"
        assert str(origin) not in result.stdout + result.stderr

    wheel = _wheel_interpreter(tmp_path / "env-external-wheel")
    wheel_result = _run_python_detect(
        _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
        cwd=workspace,
        interpreter=wheel,
        input_root=input_root,
        output_root=output_root,
    )
    assert f"SELECTED={wheel}" in wheel_result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook discovery contract")
def test_dynamic_identity_rejects_invalid_candidate_without_executing_pth(tmp_path):
    """Lower-authority identity rejection must not initialize candidate site."""
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    interpreter, site_packages = _isolated_interpreter(tmp_path / "invalid")
    sentinel = tmp_path / "invalid-candidate-startup-ran"
    _write_executable_pth(site_packages, sentinel)

    result = _run_python_detect(
        _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
        cwd=workspace,
        interpreter=interpreter,
    )

    assert result.returncode == 0, result.stderr
    assert f"SELECTED={interpreter}" not in result.stdout
    assert not sentinel.exists(), "ambient identity probe initialized site-packages"


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook discovery contract")
def test_dynamic_identity_selects_valid_wheel_without_executing_pth(tmp_path):
    """An unrelated executable .pth must neither run nor disqualify a wheel."""
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    interpreter = _wheel_interpreter(tmp_path / "wheel")
    sentinel = tmp_path / "valid-wheel-startup-ran"
    _write_executable_pth(_interpreter_site_packages(interpreter), sentinel)

    result = _run_python_detect(
        _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
        cwd=workspace,
        interpreter=interpreter,
    )

    assert result.returncode == 0, result.stderr
    assert f"SELECTED={interpreter}" in result.stdout
    assert not sentinel.exists(), "ambient identity probe initialized site-packages"


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook discovery contract")
def test_dynamic_identity_preserves_safe_editable_and_wheel_candidates(tmp_path):
    """No-site screening reconstructs safe path-only editable and wheel identity."""
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    editable_origin = tmp_path / "editable-origin"
    workspace.mkdir()
    _write_graphify_package(editable_origin)
    candidates = {
        "editable": _editable_interpreter(tmp_path / "editable", editable_origin),
        "wheel": _wheel_interpreter(tmp_path / "wheel"),
    }
    script = _PYTHON_DETECT.replace("__PINNED_PYTHON__", "")

    for label, interpreter in candidates.items():
        result = _run_python_detect(
            script,
            cwd=workspace,
            interpreter=interpreter,
        )
        assert result.returncode == 0, f"{label}: {result.stderr}"
        assert f"SELECTED={interpreter}" in result.stdout, label


def test_dynamic_identity_uses_one_shared_no_site_screening_policy():
    """All five ambient sites must route one shared identity-only no-site helper."""
    from graphify.hooks import _PYTHON_DETECT
    from tools.skillgen import gen

    generator_source = Path(gen.__file__).read_text(encoding="utf-8")
    generator_tree = ast.parse(generator_source)
    shared_imports = [
        node
        for node in generator_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("graphify.")
        and any(alias.name == "_GRAPHIFY_IDENTITY_SOURCE" for alias in node.names)
    ]
    assert len(shared_imports) == 1
    assigned_names = {
        target.id
        for node in generator_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "_GRAPHIFY_IDENTITY_SOURCE" not in assigned_names
    assert shlex.quote(gen._GRAPHIFY_IDENTITY_SOURCE) in _PYTHON_DETECT

    policy_lines = [
        line.strip()
        for line in gen._GRAPHIFY_IDENTITY_SOURCE.splitlines()
        if line.strip()
    ]
    assert policy_lines[0] == "import sys"
    assert "sys.flags.no_site" in policy_lines[1]
    assert "raise SystemExit" in policy_lines[1]

    assert _PYTHON_DETECT.count("_gfy_dynamic_usable() {") == 1
    dynamic_calls = [
        line
        for line in _PYTHON_DETECT.splitlines()
        if "_gfy_dynamic_usable" in line and "_gfy_dynamic_usable()" not in line
    ]
    assert len(dynamic_calls) == 5
    assert all("$_GFY_CANDIDATE" in line for line in dynamic_calls)
    assert "-E -P -B -S" in _PYTHON_DETECT
    assert "ambient-identity" in _PYTHON_DETECT
    assert "_GFY_DYNAMIC_PROBE" not in _PYTHON_DETECT
    assert _PYTHON_DETECT.count("could not locate a trusted final CPython") == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook discovery contract")
@pytest.mark.parametrize("source", ["trusted-pin", "dynamic"], ids=str)
@pytest.mark.parametrize(
    "record_state",
    ["valid", "missing", "empty", "duplicate", "unowned", "origin-mismatch"],
)
def test_hook_wheel_identity_requires_exact_record_ownership(tmp_path, source, record_state):
    """Wheel identity owns graphify/__init__.py only through one exact RECORD row."""
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / f"impostor-executed-{source}-{record_state}"
    interpreter = _wheel_interpreter(
        tmp_path / "candidate",
        record_state=record_state,
        marker=None if record_state == "valid" else marker,
    )
    pin = str(interpreter) if source == "trusted-pin" else ""
    screening_script = _PYTHON_DETECT.replace("__PINNED_PYTHON__", pin)

    if source == "dynamic" and record_state == "origin-mismatch":
        screening = _run_python_detect(
            screening_script,
            cwd=workspace,
            interpreter=interpreter,
        )
        assert screening.returncode == 0, screening.stderr
        assert f"SELECTED={interpreter}" in screening.stdout
        assert not marker.exists(), "startup hook executed during ambient screening"

        final = subprocess.run(
            [str(interpreter), "-E", "-P", "-B", "-c", "import graphify"],
            capture_output=True,
            text=True,
        )
        assert final.returncode == 0, final.stderr
        assert marker.exists(), "site-enabled final execution skipped startup hook"
        return

    script = (
        screening_script
        + '\n[ -z "$GRAPHIFY_PYTHON" ] || "$GRAPHIFY_PYTHON" -E -P -B -c \'import graphify\'\n'
    )

    result = _run_python_detect(
        script,
        cwd=workspace,
        interpreter=interpreter if source == "dynamic" else None,
    )

    assert result.returncode == 0, result.stderr
    if record_state == "valid":
        assert f"SELECTED={interpreter}" in result.stdout
    else:
        assert not marker.exists(), f"{record_state} impostor package executed"
        assert f"SELECTED={interpreter}" not in result.stdout


def test_dynamic_probe_receives_roots_as_argv_without_origin_reparse():
    from graphify.hooks import _PYTHON_DETECT

    probe_lines = [line for line in _PYTHON_DETECT.splitlines() if '"$_GFY_PROBE"' in line]
    assert probe_lines
    assert all('"$_GFY_WORKSPACE" "$_GFY_INPUT_ROOT" "$_GFY_OUTPUT_ROOT"' in line for line in probe_lines)
    assert "sys.argv" in _PYTHON_DETECT
    assert "_GFY_ORIGIN=$(" not in _PYTHON_DETECT


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook discovery contract")
def test_dynamic_failure_diagnostic_is_single_and_only_after_all_candidates_fail(tmp_path):
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    reject = bin_dir / "reject"
    reject.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    reject.chmod(0o755)
    for name in ("graphify", "python3", "python"):
        (bin_dir / name).symlink_to(reject)

    failed = _run_python_detect(
        _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
        cwd=workspace,
        interpreter=bin_dir / "python3",
    )
    assert failed.stderr.count("could not locate a trusted final CPython") == 1

    accept = bin_dir / "python3"
    accept.unlink()
    accept.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    accept.chmod(0o755)
    succeeded = _run_python_detect(
        _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
        cwd=workspace,
        interpreter=accept,
    )
    assert "could not locate a trusted final CPython" not in succeeded.stderr
    assert f"SELECTED={accept}" in succeeded.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook discovery contract")
def test_dynamic_candidate_rejects_parent_symlink_into_workspace(tmp_path):
    """Canonicalize the full candidate, including symlinks in parent directories."""
    from graphify.hooks import _PYTHON_DETECT

    workspace = tmp_path / "workspace"
    controlled_bin = workspace / "controlled" / "bin"
    controlled_bin.mkdir(parents=True)
    marker = tmp_path / "parent-symlink-candidate-executed"
    controlled_python = controlled_bin / "python3"
    controlled_python.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(marker))}\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    controlled_python.chmod(0o755)
    external_alias = tmp_path / "external-alias"
    external_alias.symlink_to(workspace / "controlled", target_is_directory=True)
    lexical_candidate = external_alias / "bin" / "python3"

    result = _run_python_detect(
        _PYTHON_DETECT.replace("__PINNED_PYTHON__", ""),
        cwd=workspace,
        interpreter=lexical_candidate,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "workspace-controlled candidate reached probe execution"
    assert f"SELECTED={lexical_candidate}" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="Git merge-driver shell contract is POSIX here")
@pytest.mark.parametrize(
    "hostile_printf", [False, True], ids=["clean-env", "hostile-exported-printf"]
)
def test_real_git_merge_preserves_percent_in_pinned_executable(
    tmp_path, monkeypatch, hostile_printf
):
    """Git placeholder expansion must not rewrite ``%O`` inside the executable."""
    import graphify.hooks as hooks

    repo = _make_git_repo(tmp_path / "repo")
    argv_log = tmp_path / "percent-driver-argv.log"
    pinned = tmp_path / "python%O pin"
    _write_capturing_executable(pinned, argv_log)
    monkeypatch.setattr(hooks, "_pinned_python", lambda: str(pinned))

    hostile_marker = repo / "hostile-printf-executed"
    git_env = {**os.environ, "GRAPHIFY_SKIP_HOOK": "1"}
    if hostile_printf:
        git_env.update(
            {
                "HOSTILE_PRINTF_MARKER": str(hostile_marker),
                "BASH_FUNC_printf%%": (
                    '() { : > "$HOSTILE_PRINTF_MARKER"; builtin printf "$@"; }'
                ),
            }
        )

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            capture_output=True,
            text=True,
            env=git_env,
        )

    git("config", "user.email", "hook-tests@example.invalid")
    git("config", "user.name", "Graphify Hook Tests")
    install(repo)
    graph = repo / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text("base\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    base_branch = git("branch", "--show-current").stdout.strip()

    git("checkout", "-b", "percent-driver-side")
    graph.write_text("side\n", encoding="utf-8")
    git("commit", "-am", "side")
    git("checkout", base_branch)
    graph.write_text("main\n", encoding="utf-8")
    git("commit", "-am", "main")

    merged = git("merge", "--no-edit", "percent-driver-side", check=False)

    assert not hostile_marker.exists(), "ambient exported printf function executed before driver"
    assert merged.returncode == 0, merged.stderr
    argv = argv_log.read_text(encoding="utf-8").splitlines()
    assert argv[:6] == ["-E", "-P", "-B", "-m", "graphify", "merge-driver"]
    assert argv[6:8] == ["--managed-current", "graphify-out/graph.json"]
    assert len(argv) == 11
    assert len(set(argv[8:])) == 3


@pytest.mark.skipif(os.name == "nt", reason="installed hook E2E uses POSIX shell")
def test_installed_post_commit_hook_recovers_merge_pending_union(tmp_path):
    from graphify import transaction as transaction_module
    from graphify.watch import _rebuild_code

    repo = _make_git_repo(tmp_path / "repo")
    other = tmp_path / "other"
    other.mkdir()
    current_source = repo / "current.py"
    other_source = other / "other.py"
    current_source.write_text("def current():\n    return 1\n", encoding="utf-8")
    other_source.write_text("def other():\n    return 2\n", encoding="utf-8")
    assert _rebuild_code(repo, no_cluster=True, block_on_lock=True)
    assert _rebuild_code(other, no_cluster=True, block_on_lock=True)

    output = repo / "graphify-out"
    graph = output / "graph.json"
    ancestor = tmp_path / "ancestor.json"
    ancestor.write_bytes(graph.read_bytes())
    (repo / other_source.name).write_bytes(other_source.read_bytes())
    transaction_module.merge_detached_snapshots(
        ancestor, graph, other / "graphify-out" / "graph.json"
    )

    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "hook-tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Graphify Hook Tests"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "current.py", "other.py"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "baseline"], check=True
    )
    install(repo)

    current_source.write_text(
        "def current():\n    return 3\n\ndef changed():\n    return current()\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "current.py"], check=True
    )
    disposable_home = tmp_path / "home"
    disposable_home.mkdir()
    committed = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "trigger hook"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(disposable_home)},
    )
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert "launching background rebuild" in committed.stdout + committed.stderr

    log = disposable_home / ".cache" / "graphify-rebuild.log"
    snapshot = _wait_for_hook_recovery(
        transaction_module,
        graph,
        {"current_current", "current_changed", "other_other"},
        log=log,
        purpose="installed-post-commit-merge-recovery-test",
    )
    assert snapshot.data["graph"][transaction_module.GRAPH_WATERMARK_KEY][
        "state"
    ] == "active"
    for name in (
        transaction_module.PREPARED_FILE,
        transaction_module.TRANSACTION_FILE,
        transaction_module.TRANSITION_FILE,
        transaction_module.TOKEN_TRANSITION_FILE,
    ):
        assert not (output / name).exists()
    assert not list(output.glob(".graphify_transaction_token.*"))
    status_snapshot = transaction_module.transaction_status(output)
    assert status_snapshot["transaction"] is None
    assert status_snapshot["prepared_creation"] is None
    assert status_snapshot["token_transition"] is None


def _wait_for_hook_recovery(
    transaction_module,
    graph: Path,
    expected_node_ids: set[str],
    *,
    log: Path,
    purpose: str,
):
    output = graph.parent
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            candidate = transaction_module.open_graph_snapshot(graph, purpose=purpose)
            status_snapshot = transaction_module.transaction_status(output)
        except transaction_module.PendingTransactionError:
            time.sleep(0.1)
            continue
        node_ids = {node["id"] for node in candidate.data["nodes"]}
        coordination_absent = all(
            not (output / name).exists()
            for name in (
                transaction_module.PREPARED_FILE,
                transaction_module.TRANSACTION_FILE,
                transaction_module.TRANSITION_FILE,
                transaction_module.TOKEN_TRANSITION_FILE,
            )
        ) and not list(output.glob(".graphify_transaction_token.*"))
        status_terminal = all(
            status_snapshot[name] is None
            for name in ("transaction", "prepared_creation", "token_transition")
        )
        if expected_node_ids <= node_ids and coordination_absent and status_terminal:
            return candidate
        time.sleep(0.1)
    pytest.fail(
        log.read_text(encoding="utf-8") if log.exists() else "hook recovery timed out"
    )


@pytest.mark.skipif(os.name == "nt", reason="installed hook E2E uses POSIX shell")
@pytest.mark.parametrize(
    ("output_name", "linked_worktree"),
    [
        ("graphify-out", False),
        ("custom-out", False),
        ("graphify-out", True),
    ],
    ids=["default-output", "custom-output", "linked-worktree"],
)
def test_installed_post_merge_hook_recovers_real_merge_pending_union(
    tmp_path, monkeypatch, output_name, linked_worktree
):
    from graphify import transaction as transaction_module
    from graphify import paths as paths_module

    monkeypatch.setattr(paths_module, "GRAPHIFY_OUT", output_name)

    def rebuild(root: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-E",
                "-P",
                "-B",
                "-c",
                (
                    "from pathlib import Path; "
                    "from graphify.watch import _rebuild_code; "
                    "raise SystemExit(0 if _rebuild_code(Path('.'), "
                    "no_cluster=True, block_on_lock=True) else 1)"
                ),
            ],
            cwd=root,
            env={**os.environ, "GRAPHIFY_OUT": output_name},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    repo = _make_git_repo(tmp_path / "repo")
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "hook-tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Graphify Hook Tests"],
        check=True,
    )
    (repo / "base.py").write_text("def base():\n    return 1\n", encoding="utf-8")
    rebuild(repo)
    install(repo)

    setup_env = {**os.environ, "GRAPHIFY_SKIP_HOOK": "1"}

    def git(*args: str, cwd=None, env=None, check=True):
        return subprocess.run(
            ["git", "-C", str(cwd or repo), *args],
            check=check,
            capture_output=True,
            text=True,
            env=setup_env if env is None else env,
        )

    graph = repo / output_name / "graph.json"
    graph_pathspec = f"{output_name}/graph.json"
    git("add", ".gitattributes", "base.py", graph_pathspec)
    git("commit", "-m", "base")
    base_branch = git("branch", "--show-current").stdout.strip()
    base_output = {
        path.relative_to(graph.parent): path.read_bytes()
        for path in graph.parent.rglob("*")
        if path.is_file()
    }

    git("checkout", "-b", "side")
    (repo / "side.py").write_text("def side():\n    return 2\n", encoding="utf-8")
    rebuild(repo)
    git("add", "side.py", graph_pathspec)
    git("commit", "-m", "side")

    merge_repo = repo
    if linked_worktree:
        merge_repo = tmp_path / "linked"
        git("worktree", "add", "-b", "linked-main", str(merge_repo), base_branch)
        graph = merge_repo / output_name / "graph.json"
        shutil.rmtree(graph.parent)
        rebuild(merge_repo)
    else:
        git("checkout", base_branch)
        for path in graph.parent.rglob("*"):
            if path.is_file() and path.relative_to(graph.parent) not in base_output:
                path.unlink()
        for relative, payload in base_output.items():
            target = graph.parent / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
    (merge_repo / "main.py").write_text(
        "def main():\n    return 3\n", encoding="utf-8"
    )
    rebuild(merge_repo)
    git("add", "main.py", graph_pathspec, cwd=merge_repo)
    git("commit", "-m", "main", cwd=merge_repo)

    disposable_home = tmp_path / "home"
    disposable_home.mkdir()
    merged = git(
        "merge",
        "--no-edit",
        "side",
        cwd=merge_repo,
        env={**os.environ, "HOME": str(disposable_home)},
        check=False,
    )
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert "Merge completed - launching background rebuild" in (
        merged.stdout + merged.stderr
    )

    log = disposable_home / ".cache" / "graphify-rebuild.log"
    snapshot = _wait_for_hook_recovery(
        transaction_module,
        graph,
        {"base_base", "main_main", "side_side"},
        log=log,
        purpose="installed-post-merge-recovery-test",
    )
    assert snapshot.data["graph"][transaction_module.GRAPH_WATERMARK_KEY][
        "state"
    ] == "active"
    if linked_worktree:
        active_probe = subprocess.run(
            ["/bin/sh", str(repo / ".git" / "hooks" / "post-merge")],
            cwd=merge_repo,
            env={**os.environ, "HOME": str(disposable_home)},
            capture_output=True,
            text=True,
        )
        assert active_probe.returncode == 0, active_probe.stderr
        assert "launching background rebuild" not in active_probe.stdout

        probe_base = tmp_path / "probe-base.json"
        probe_current = tmp_path / "probe-current.json"
        probe_other = tmp_path / "probe-other.json"
        for probe in (probe_base, probe_current, probe_other):
            probe.write_bytes(graph.read_bytes())
        transaction_module.merge_detached_snapshots(
            probe_base, probe_current, probe_other
        )
        graph.unlink()
        graph.symlink_to(probe_current)
        symlink_probe = subprocess.run(
            ["/bin/sh", str(repo / ".git" / "hooks" / "post-merge")],
            cwd=merge_repo,
            env={**os.environ, "HOME": str(disposable_home)},
            capture_output=True,
            text=True,
        )
        assert symlink_probe.returncode == 0, symlink_probe.stderr
        assert "launching background rebuild" not in symlink_probe.stdout


@pytest.mark.skipif(os.name == "nt", reason="installed hook E2E uses POSIX shell")
def test_recursive_merge_driver_uses_binary_for_criss_cross_merge_bases(
    tmp_path, monkeypatch
):
    from graphify import transaction as transaction_module

    repo = _make_git_repo(tmp_path / "repo")
    driver_log = tmp_path / "merge-driver.log"
    pinned_python = tmp_path / "logging-python"
    pinned_python.write_text(
        "#!/bin/sh\n"
        "for arg in \"$@\"; do\n"
        "    if [ \"$arg\" = \"merge-driver\" ]; then\n"
        f"        printf '%s\\n' merge-driver >> {shlex.quote(str(driver_log))}\n"
        "        break\n"
        "    fi\n"
        "done\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    pinned_python.chmod(0o755)
    monkeypatch.setattr(hooks, "_pinned_python", lambda: str(pinned_python))
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "hook-tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Graphify Hook Tests"],
        check=True,
    )
    skip_env = {**os.environ, "GRAPHIFY_SKIP_HOOK": "1"}

    def git(*args: str, cwd=None, env=None, check=True):
        return subprocess.run(
            ["git", "-C", str(cwd or repo), *args],
            check=check,
            capture_output=True,
            text=True,
            env=skip_env if env is None else env,
        )

    def rebuild(root: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-E",
                "-P",
                "-B",
                "-c",
                (
                    "from pathlib import Path; "
                    "from graphify.watch import _rebuild_code; "
                    "raise SystemExit(0 if _rebuild_code(Path('.'), "
                    "no_cluster=True, block_on_lock=True) else 1)"
                ),
            ],
            cwd=root,
            env={**skip_env, "GRAPHIFY_OUT": "graphify-out"},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def recover(root: Path, expected: set[str], label: str) -> None:
        home = tmp_path / f"home-{label}"
        home.mkdir()
        hook = repo / ".git" / "hooks" / "post-merge"
        launched = subprocess.run(
            ["/bin/sh", str(hook)],
            cwd=root,
            env={**os.environ, "HOME": str(home)},
            capture_output=True,
            text=True,
        )
        assert launched.returncode == 0, launched.stdout + launched.stderr
        assert "launching background rebuild" in launched.stdout + launched.stderr
        _wait_for_hook_recovery(
            transaction_module,
            root / "graphify-out" / "graph.json",
            expected,
            log=home / ".cache" / "graphify-rebuild.log",
            purpose=f"criss-cross-{label}",
        )

    (repo / "base.py").write_text("def base():\n    return 1\n", encoding="utf-8")
    rebuild(repo)
    install(repo)
    graph_pathspec = "graphify-out/graph.json"
    git("add", ".gitattributes", "base.py", graph_pathspec)
    git("commit", "-m", "base")
    base_branch = git("branch", "--show-current").stdout.strip()

    git("checkout", "-b", "left")
    (repo / "left.py").write_text("def left():\n    return 2\n", encoding="utf-8")
    rebuild(repo)
    git("add", "left.py", graph_pathspec)
    git("commit", "-m", "left-1")
    left_one = git("rev-parse", "HEAD").stdout.strip()

    right = tmp_path / "right"
    git("worktree", "add", "-b", "right", str(right), base_branch)
    shutil.rmtree(right / "graphify-out")
    (right / "right.py").write_text("def right():\n    return 3\n", encoding="utf-8")
    rebuild(right)
    git("add", "right.py", graph_pathspec, cwd=right)
    git("commit", "-m", "right-1", cwd=right)
    right_one = git("rev-parse", "HEAD", cwd=right).stdout.strip()

    left_merge = git("merge", "--no-edit", right_one, check=False)
    assert left_merge.returncode == 0, left_merge.stdout + left_merge.stderr
    recover(repo, {"base_base", "left_left", "right_right"}, "left-merge")
    git("add", graph_pathspec)
    git("commit", "-m", "finalize left merge")
    (repo / "left_after.py").write_text(
        "def left_after():\n    return left()\n", encoding="utf-8"
    )
    rebuild(repo)
    git("add", "left_after.py", graph_pathspec)
    git("commit", "-m", "left-after")

    right_merge = git("merge", "--no-edit", left_one, cwd=right, check=False)
    assert right_merge.returncode == 0, right_merge.stdout + right_merge.stderr
    recover(right, {"base_base", "left_left", "right_right"}, "right-merge")
    git("add", graph_pathspec, cwd=right)
    git("commit", "-m", "finalize right merge", cwd=right)
    (right / "right_after.py").write_text(
        "def right_after():\n    return right()\n", encoding="utf-8"
    )
    rebuild(right)
    git("add", "right_after.py", graph_pathspec, cwd=right)
    git("commit", "-m", "right-after", cwd=right)

    merge_bases = git("merge-base", "--all", "HEAD", "right").stdout.splitlines()
    assert len(merge_bases) == 2
    assert len(set(merge_bases)) == 2
    assert set(merge_bases) == {left_one, right_one}
    assert driver_log.read_text(encoding="utf-8").splitlines() == [
        "merge-driver",
        "merge-driver",
    ]

    final_merge = git("merge", "--no-edit", "right", check=False)
    assert final_merge.returncode == 0, final_merge.stdout + final_merge.stderr
    assert driver_log.read_text(encoding="utf-8").splitlines() == [
        "merge-driver",
        "merge-driver",
        "merge-driver",
    ]
    pending = transaction_module.load_detached_merge_snapshot(
        repo / graph_pathspec, role="current"
    )
    assert pending["graph"][transaction_module.GRAPH_WATERMARK_KEY]["state"] == (
        "merge_pending"
    )
    assert {node["id"] for node in pending["nodes"]} >= {
        "base_base",
        "left_left",
        "left_after_left_after",
        "right_right",
        "right_after_right_after",
    }

    recover(
        repo,
        {
            "base_base",
            "left_left",
            "left_after_left_after",
            "right_right",
            "right_after_right_after",
        },
        "final-merge",
    )


@pytest.mark.skipif(os.name == "nt", reason="installed hook E2E uses POSIX shell")
def test_installed_post_commit_hook_recovers_conflicted_merge_at_custom_output(
    tmp_path, monkeypatch
):
    from graphify import paths as paths_module
    from graphify import transaction as transaction_module

    output_name = "custom-out"
    monkeypatch.setattr(paths_module, "GRAPHIFY_OUT", output_name)

    def rebuild(root: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-E",
                "-P",
                "-B",
                "-c",
                (
                    "from pathlib import Path; "
                    "from graphify.watch import _rebuild_code; "
                    "raise SystemExit(0 if _rebuild_code(Path('.'), "
                    "no_cluster=True, block_on_lock=True) else 1)"
                ),
            ],
            cwd=root,
            env={**os.environ, "GRAPHIFY_OUT": output_name},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    repo = _make_git_repo(tmp_path / "repo")
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "hook-tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Graphify Hook Tests"],
        check=True,
    )
    (repo / "base.py").write_text("def base():\n    return 1\n", encoding="utf-8")
    conflict = repo / "conflict.txt"
    conflict.write_text("base\n", encoding="utf-8")
    rebuild(repo)
    install(repo)

    setup_env = {**os.environ, "GRAPHIFY_SKIP_HOOK": "1"}

    def git(*args: str, env=None, check=True):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            capture_output=True,
            text=True,
            env=setup_env if env is None else env,
        )

    graph = repo / output_name / "graph.json"
    graph_pathspec = f"{output_name}/graph.json"
    git("add", ".gitattributes", "base.py", "conflict.txt", graph_pathspec)
    git("commit", "-m", "base")
    base_branch = git("branch", "--show-current").stdout.strip()
    base_output = {
        path.relative_to(graph.parent): path.read_bytes()
        for path in graph.parent.rglob("*")
        if path.is_file()
    }

    git("checkout", "-b", "side")
    (repo / "side.py").write_text("def side():\n    return 2\n", encoding="utf-8")
    conflict.write_text("side\n", encoding="utf-8")
    rebuild(repo)
    git("add", "side.py", "conflict.txt", graph_pathspec)
    git("commit", "-m", "side")

    git("checkout", base_branch)
    for path in graph.parent.rglob("*"):
        if path.is_file():
            path.unlink()
    for relative, payload in base_output.items():
        target = graph.parent / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    (repo / "main.py").write_text("def main():\n    return 3\n", encoding="utf-8")
    conflict.write_text("main\n", encoding="utf-8")
    rebuild(repo)
    git("add", "main.py", "conflict.txt", graph_pathspec)
    git("commit", "-m", "main")

    disposable_home = tmp_path / "home"
    disposable_home.mkdir()
    runtime_env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"GRAPHIFY_OUT", "GRAPHIFY_OUTPUT_ROOT", "GRAPHIFY_SKIP_HOOK"}
    }
    runtime_env["HOME"] = str(disposable_home)
    merged = git("merge", "--no-edit", "side", env=runtime_env, check=False)
    assert merged.returncode != 0
    assert "CONFLICT" in merged.stdout + merged.stderr
    assert "launching background rebuild" not in merged.stdout + merged.stderr
    assert "Merge completed - launching background rebuild" not in (
        merged.stdout + merged.stderr
    )

    pending = transaction_module.load_detached_merge_snapshot(graph, role="current")
    assert pending["graph"][transaction_module.GRAPH_WATERMARK_KEY]["state"] == (
        "merge_pending"
    )
    assert {node["id"] for node in pending["nodes"]} >= {
        "base_base",
        "main_main",
        "side_side",
    }
    log = disposable_home / ".cache" / "graphify-rebuild.log"
    assert not log.exists()
    assert not (repo / "graphify-out").exists()

    conflict.write_text("resolved\n", encoding="utf-8")
    git("add", "conflict.txt", env=runtime_env)
    committed = git("commit", "--no-edit", env=runtime_env, check=False)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert "launching background rebuild" in committed.stdout + committed.stderr

    snapshot = _wait_for_hook_recovery(
        transaction_module,
        graph,
        {"base_base", "main_main", "side_side"},
        log=log,
        purpose="installed-post-commit-conflicted-merge-recovery-test",
    )
    assert snapshot.data["graph"][transaction_module.GRAPH_WATERMARK_KEY][
        "state"
    ] == "active"
    assert not (repo / "graphify-out").exists()
