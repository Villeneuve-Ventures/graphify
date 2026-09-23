"""Source discovery accepts Git's annotated partial-clone fetch remotes."""

import os
import subprocess
import traceback

import pytest

from graphify.workspace.identity import SourceDiscoveryError, discover_source


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
