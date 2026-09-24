"""Legacy graph updates must tolerate large reports and unrelated checkouts."""

import json

import pytest

from graphify import transaction


def legacy_output(tmp_path):
    output = tmp_path / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_text(json.dumps({
        "directed": False, "multigraph": False, "graph": {}, "nodes": [], "links": [],
    }))
    return output


def test_large_legacy_analysis_is_retained(tmp_path):
    output = legacy_output(tmp_path)
    payload = json.dumps({"communities": {"0": ["node"] * 200_000}}).encode()
    assert len(payload) > 1024 * 1024
    (output / ".graphify_analysis.json").write_bytes(payload)

    snapshot = transaction.open_graph_snapshot(output / "graph.json", purpose="test")

    assert snapshot.artifacts[".graphify_analysis.json"] == payload


@pytest.mark.parametrize("git_directory", [False, True])
@pytest.mark.parametrize("vault_manifest", [False, True])
def test_legacy_discovery_stops_at_nested_repository(
    tmp_path, monkeypatch, git_directory, vault_manifest,
):
    output = legacy_output(tmp_path)
    repo = output / "worktrees" / "checkout"
    repo.mkdir(parents=True)
    marker = repo / ".git"
    if git_directory:
        marker.mkdir()
    else:
        marker.write_text("gitdir: /unrelated/worktree/metadata\n")
    (repo / "source").mkdir()
    (repo / "source" / "untouched.py").write_text("original\n")
    if vault_manifest:
        (repo / ".graphify_obsidian_manifest.json").write_text(
            json.dumps({"files": ["owned.md"]})
        )
        (repo / "owned.md").write_text("owned\n")
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    original_list = transaction._list_entries

    def guarded_list(capability):
        assert repo not in capability.path.parents
        return original_list(capability)

    monkeypatch.setattr(transaction, "_list_entries", guarded_list)
    snapshot = transaction.open_graph_snapshot(output / "graph.json", purpose="test")

    assert ("worktrees/checkout/owned.md" in snapshot.artifacts) == vault_manifest
    assert "worktrees/checkout/source/untouched.py" not in snapshot.artifacts
    assert before == {
        p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()
    }


def test_legacy_analysis_still_obeys_aggregate_budget(tmp_path, monkeypatch):
    output = legacy_output(tmp_path)
    (output / ".graphify_analysis.json").write_bytes(b"{}" * 128)
    monkeypatch.setattr(transaction, "_MAX_RECEIPT_AGGREGATE_BYTES", 128)

    with pytest.raises(transaction.PendingTransactionError):
        transaction.open_graph_snapshot(output / "graph.json", purpose="test")


@pytest.mark.parametrize("mutation", ["appearing_manifest", "changed_manifest", "changed_note"])
def test_selected_legacy_vault_is_bound_before_publication(tmp_path, mutation):
    output = legacy_output(tmp_path)
    vault = output / "repo" / "vault"
    vault.mkdir(parents=True)
    (vault.parent / ".git").mkdir()
    note = vault / "owned.md"
    note.write_text("original")
    manifest = vault / ".graphify_obsidian_manifest.json"
    if mutation != "appearing_manifest":
        manifest.write_text(json.dumps({"files": [note.name]}))
    snapshot = transaction.open_graph_snapshot(
        output / "graph.json", purpose="export-admission",
        retain_obsidian_vaults=("repo/vault",),
    )
    if mutation == "changed_note":
        note.write_text("changed")
    else:
        manifest.write_text(json.dumps({"files": []}))
    before = {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}

    with pytest.raises(transaction.PendingTransactionError, match="inventory changed"):
        transaction.begin_transaction("runtime", tmp_path, output=output, expected_snapshot=snapshot)

    assert before == {
        p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()
    }


def test_selected_legacy_vault_obeys_shared_aggregate_budget(tmp_path, monkeypatch):
    output = legacy_output(tmp_path)
    vault = output / "repo" / "vault"
    vault.mkdir(parents=True)
    (vault.parent / ".git").mkdir()
    (vault / "owned.md").write_bytes(b"x" * 256)
    (vault / ".graphify_obsidian_manifest.json").write_text(json.dumps({"files": ["owned.md"]}))
    monkeypatch.setattr(transaction, "_MAX_RECEIPT_AGGREGATE_BYTES", 256)

    with pytest.raises(transaction.PendingTransactionError):
        transaction.open_graph_snapshot(
            output / "graph.json", purpose="export-admission",
            retain_obsidian_vaults=("repo/vault",),
        )


def test_selected_legacy_vault_exhausted_count_rejects_before_read(tmp_path, monkeypatch):
    output = legacy_output(tmp_path)
    vault = output / "repo" / "vault"
    vault.mkdir(parents=True)
    (vault.parent / ".git").mkdir()
    (vault / ".graphify_obsidian_manifest.json").write_text('{"files": []}')
    original_read = transaction._read_relative_bytes

    def guarded_read(capability, name, *args, **kwargs):
        assert name != "repo/vault/.graphify_obsidian_manifest.json"
        return original_read(capability, name, *args, **kwargs)

    monkeypatch.setattr(transaction, "_read_relative_bytes", guarded_read)
    with transaction.pin_output(output) as capability:
        with pytest.raises(transaction.PendingTransactionError, match="exceeds bounds"):
            transaction._legacy_owned_dynamic_inventory(
                capability,
                budget=transaction._LegacyInventoryBudget(count=transaction._MAX_RECEIPT_ARTIFACTS),
                obsidian_vaults=("repo/vault",),
            )
