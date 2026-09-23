"""Source discovery accepts Git's annotated partial-clone fetch remotes."""

import os
import subprocess
import traceback

import pytest

from graphify.workspace.identity import SourceDiscoveryError, discover_source
from graphify.workspace.persistence import RuntimeCapabilities


def _git(root, *args):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        cwd=root, env=environment, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def source_repository(tmp_path):
    root = tmp_path.resolve() / "source"
    root.mkdir()
    _git(root, "init", "-q")
    for message in ("initial", "second"):
        _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
             "commit", "--allow-empty", "-qm", message)
    config = root / ".graphify" / "workspace.toml"
    config.parent.mkdir()
    config.write_text('''contract = "graphify.workspace.config"
schema_version = 1
repo_uuid = "550e8400-e29b-41d4-a716-446655440000"
[policy]
freshness = "current_only"
semantic_mode = "host_agent_only"
network_egress = false
headless_backends = []
''')
    return root


@pytest.mark.parametrize("remote", [
    "user:TEST_SECRET@host:owner/repo",
    "https://user:TEST_SECRET@host:abc/repo",
    "https://host:TEST_SECRET/repo",
    "https://user:TEST_SECRET@[invalid/repo",
])
def test_discovery_errors_do_not_disclose_remote_credentials(source_repository, remote):
    _git(source_repository, "remote", "add", "origin", remote)
    with pytest.raises(SourceDiscoveryError) as raised:
        discover_source(source_repository)
    assert "TEST_SECRET" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("remote", [
    "https://example.test/org/repo%20name.git",
    "https://example.test/org/../repo.git",
    "https://example.test/org//repo.git",
    "https://invalid_host.test/org/repo.git",
    "ssh://bad%20user@example.test/org/repo.git",
])
def test_noncanonical_remote_is_rejected_before_enrollment(
    source_repository, tmp_path, remote,
):
    from graphify.workspace.identity import IdentityAction, OperatorAuthorization
    from graphify.workspace.registry import RegistryStore

    _git(source_repository, "remote", "add", "origin", remote)
    store = RegistryStore(
        tmp_path.resolve() / "state",
        capabilities=RuntimeCapabilities.supported_test_fixture(),
    )
    with pytest.raises(SourceDiscoveryError, match="canonical"):
        source = discover_source(source_repository)
        store.enroll(source, OperatorAuthorization(
            IdentityAction.ENROLL, "fixture", "test enrollment", "2026-09-23T00:00:00Z", "enroll",
        ))
    assert not store.state.root.exists()


@pytest.mark.parametrize("remote,expected", [
    ("https://EXAMPLE.test/org/repo.git/", "https://example.test/org/repo.git"),
    ("git@EXAMPLE.test:org/repo.git", "ssh://git@example.test/org/repo.git"),
])
def test_normalized_remotes_remain_enrollable(source_repository, tmp_path, remote, expected):
    from graphify.workspace.identity import IdentityAction, OperatorAuthorization
    from graphify.workspace.registry import RegistryStore

    _git(source_repository, "remote", "add", "origin", remote)
    source = discover_source(source_repository)
    assert source.registry_source["remote_aliases"][0]["url"] == expected
    store = RegistryStore(
        tmp_path.resolve() / "state",
        capabilities=RuntimeCapabilities.supported_test_fixture(),
    )
    store.enroll(source, OperatorAuthorization(
        IdentityAction.ENROLL, "fixture", "test enrollment", "2026-09-23T00:00:00Z", "enroll",
    ))
    assert store.resolve_active_source(source.repo_uuid) == source


def test_shallow_repository_requires_complete_history(source_repository, tmp_path):
    clone = tmp_path.resolve() / "shallow"
    _git(tmp_path, "clone", "--depth=1", source_repository.as_uri(), str(clone))
    _git(clone, "remote", "set-url", "origin", "https://example.test/owner/repo.git")
    (clone / ".graphify").mkdir()
    (clone / ".graphify/workspace.toml").write_bytes(
        (source_repository / ".graphify/workspace.toml").read_bytes()
    )
    head = _git(clone, "rev-parse", "HEAD")
    with pytest.raises(SourceDiscoveryError, match="shallow"):
        discover_source(clone)
    _git(clone, "fetch", "--unshallow", source_repository.as_uri())
    discovered = discover_source(clone)
    assert discovered.head_commit == head.strip()
    assert discovered.history_roots == tuple(
        _git(source_repository, "rev-list", "--max-parents=0", "HEAD").splitlines()
    )


@pytest.mark.parametrize("hostile_environment", [False, True])
def test_partial_clone_fetch_annotation_preserves_source_identity(
    tmp_path, monkeypatch, hostile_environment,
):
    root = tmp_path.resolve() / "source"
    root.mkdir()
    if hostile_environment:
        config_path = tmp_path / "global.gitconfig"
        config_path.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /nonexistent/signer\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config_path))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config_path))
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "outside.git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "outside"))
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    def git(*args):
        return _git(root, *args)

    git("init", "-q")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "--allow-empty", "-qm", "initial")
    git("remote", "add", "origin", "https://example.test/owner/repo.git")
    # A push-only destination must never enter the discovered fetch aliases.
    git("remote", "set-url", "--push", "origin", "https://example.test/push/repo.git")
    config = root / ".graphify" / "workspace.toml"
    config.parent.mkdir()
    config.write_text('''contract = "graphify.workspace.config"
schema_version = 1
repo_uuid = "550e8400-e29b-41d4-a716-446655440000"
[policy]
freshness = "current_only"
semantic_mode = "host_agent_only"
network_egress = false
headless_backends = []
''')
    ordinary = discover_source(root)
    git("config", "remote.origin.partialclonefilter", "blob:none")
    assert "(fetch) [blob:none]" in git("remote", "-v")
    partial = discover_source(root)
    assert partial == ordinary
    assert len(partial.registry_source["remote_aliases"]) == 1
    assert (root / ".git").is_dir()
    assert not (tmp_path / "outside.git").exists()


@pytest.mark.parametrize("replacement", ["root", "git", "config"])
def test_discovery_rejects_identity_changes_between_git_commands(
    source_repository, monkeypatch, replacement,
):
    from graphify.workspace import identity
    import shutil

    root = source_repository
    _git(root, "remote", "add", "origin", "https://example.test/owner/repo.git")
    original_git = identity._git
    changed = False

    def changing_git(path, *arguments, **kwargs):
        nonlocal changed
        result = original_git(path, *arguments, **kwargs)
        trigger = ("rev-parse", "--git-common-dir") if replacement == "root" else (
            "rev-list", "--max-parents=0",
        )
        if not changed and arguments[:len(trigger)] == trigger:
            changed = True
            if replacement == "root":
                moved = root.with_name("original")
                root.rename(moved)
                shutil.copytree(moved, root)
            elif replacement == "git":
                moved = root / "original.git"
                (root / ".git").rename(moved)
                shutil.copytree(moved, root / ".git")
            else:
                config = root / ".graphify/workspace.toml"
                config.write_bytes(config.read_bytes() + b"\n# changed\n")
        return result

    monkeypatch.setattr(identity, "_git", changing_git)
    with pytest.raises(SourceDiscoveryError, match="changed"):
        discover_source(root)
    assert changed


def test_discovery_config_read_has_fixed_limit(source_repository, monkeypatch):
    from graphify.workspace import identity

    _git(source_repository, "remote", "add", "origin", "https://example.test/owner/repo.git")
    config = source_repository / ".graphify/workspace.toml"
    with config.open("ab") as stream:
        stream.truncate(1024 * 1024 + 1)
    reads = []
    original_read = identity.os.read

    def record_read(descriptor, size):
        if os.fstat(descriptor).st_ino == config.stat().st_ino:
            reads.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(identity.os, "read", record_read)
    with pytest.raises(SourceDiscoveryError, match="byte limit"):
        discover_source(source_repository)
    assert not reads


def test_active_resolution_rejects_replaced_clone(source_repository, tmp_path):
    import shutil
    from graphify.workspace.identity import (
        IdentityAction, OperatorAuthorization, SourceAmbiguousError,
    )
    from graphify.workspace.registry import RegistryStore

    root = source_repository
    _git(root, "remote", "add", "origin", "https://example.test/owner/repo.git")
    source = discover_source(root)
    store = RegistryStore(
        tmp_path.resolve() / "state",
        capabilities=RuntimeCapabilities.supported_test_fixture(),
    )
    store.enroll(source, OperatorAuthorization(
        IdentityAction.ENROLL, "fixture", "test enrollment", "2026-09-23T00:00:00Z", "enroll",
    ))
    assert store.resolve_active_source(source.repo_uuid) == source
    moved = root.with_name("original")
    root.rename(moved)
    shutil.copytree(moved, root)
    replacement = discover_source(root)
    assert replacement.registry_source == source.registry_source
    assert replacement.history_roots == source.history_roots
    with pytest.raises(SourceAmbiguousError, match="identity"):
        store.resolve_active_source(source.repo_uuid)
    # Authorized rebind still permits shared history; selection remains explicit.
    store.rebind(replacement, OperatorAuthorization(
        IdentityAction.REBIND, "fixture", "test rebind", "2026-09-23T00:00:00Z", "rebind",
    ))
    with pytest.raises(SourceAmbiguousError, match="identity"):
        store.resolve_active_source(source.repo_uuid)


@pytest.mark.parametrize("limit", [None, 2 * 1024 * 1024, 32])
@pytest.mark.parametrize("reader", ["discover_source", "read_workspace_config", "read_workspace_config_with_digest"])
def test_config_limit_applies_to_all_readers(source_repository, limit, reader):
    from graphify.workspace import identity

    _git(source_repository, "remote", "add", "origin", "https://example.test/owner/repo.git")
    config = source_repository / ".graphify/workspace.toml"
    if limit != 32:
        with config.open("ab") as stream:
            stream.truncate(1024 * 1024 + 1)
    with pytest.raises(SourceDiscoveryError, match="byte limit"):
        getattr(identity, reader)(source_repository, max_bytes=limit)


def test_authorized_adoption_preserves_active_source(source_repository, tmp_path):
    import shutil
    from graphify.workspace.identity import IdentityAction, OperatorAuthorization
    from graphify.workspace.registry import RegistryStore

    root = source_repository
    _git(root, "remote", "add", "origin", "https://example.test/owner/repo.git")
    source = discover_source(root)
    store = RegistryStore(
        tmp_path.resolve() / "state",
        capabilities=RuntimeCapabilities.supported_test_fixture(),
    )
    store.enroll(source, OperatorAuthorization(
        IdentityAction.ENROLL, "fixture", "test enrollment", "2026-09-23T00:00:00Z", "enroll",
    ))
    clone = root.with_name("clone")
    shutil.copytree(root, clone)
    adopted = discover_source(clone)
    document = store.adopt(adopted, OperatorAuthorization(
        IdentityAction.ADOPT, "fixture", "test adoption", "2026-09-23T00:00:00Z", "adopt",
    ))
    assert adopted.registry_source in document.to_dict()["workspaces"][0]["aliases"]
    assert store.resolve_active_source(source.repo_uuid) == source
