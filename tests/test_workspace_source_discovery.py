"""Source discovery accepts Git's annotated partial-clone fetch remotes."""

import subprocess

from graphify.workspace.identity import discover_source


def test_partial_clone_fetch_annotation_preserves_source_identity(tmp_path):
    root = tmp_path.resolve() / "source"
    root.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        ).stdout

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
