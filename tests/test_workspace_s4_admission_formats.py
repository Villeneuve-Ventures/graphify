"""Unsupported source object formats refuse before enrollment writes."""
import subprocess

import pytest

from graphify.workspace.identity import SourceDiscoveryError, discover_source
from graphify.workspace.registry import RegistryStore
from tests.workspace_s3_helpers import (
    REPO_UUID, SUPPORTED, _workspace_toml, authorization, tree_snapshot,
)


@pytest.mark.parametrize("packed", [False, True])
def test_sha256_repository_refuses_before_enrollment(tmp_path, packed):
    root = tmp_path.resolve()
    repo = root / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)

    git("init", "--object-format=sha256")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    (repo / ".graphify").mkdir()
    (repo / ".graphify/workspace.toml").write_text(_workspace_toml(REPO_UUID))
    (repo / "main.py").write_text("value = 1\n")
    git("add", ".")
    git("-c", "core.hooksPath=/dev/null", "commit", "--no-gpg-sign", "-m", "fixture")
    git("remote", "add", "origin", "https://example.test/source.git")
    if packed:
        git("pack-refs", "--all")
    oid = git("rev-parse", "HEAD").strip()
    assert len(oid) == 64
    state = root / "state"
    state.mkdir(mode=0o700)
    store = RegistryStore(state, capabilities=SUPPORTED)
    before_source, before_state = tree_snapshot(repo), tree_snapshot(state)
    with pytest.raises(SourceDiscoveryError, match="SHA-1"):
        source = discover_source(repo)
        store.enroll(source, authorization("sha256-enroll"), expected_revision=0)
    assert tree_snapshot(repo) == before_source
    assert tree_snapshot(state) == before_state
