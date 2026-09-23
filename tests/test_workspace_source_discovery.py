"""Source discovery accepts Git's annotated partial-clone fetch remotes."""

import os
import subprocess

import pytest

from graphify.workspace.identity import discover_source


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
