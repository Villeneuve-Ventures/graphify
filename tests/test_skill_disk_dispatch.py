"""Disk-host callbacks cross the prepared boundary only through exact-owner import."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from graphify import transaction as tx
from tests.skill_runbook_helpers import prepared_path, python_bodies, run_body
from tools.skillgen import gen

pytestmark = pytest.mark.skipif(os.name == "nt", reason="native POSIX transaction backend")

DISK_HOSTS = ["claude", "windows", "kilo", "copilot", "claw", "droid", "amp",
              "agents", "trae", "kiro", "pi", "vscode"]


def _body(reference: str, marker: str) -> str:
    bodies = python_bodies(reference)
    bodies += re.findall(r"\$GraphifyPythonSource = @'\n(.*?)\n'@", reference, re.S)
    selected = [body for body in bodies if marker in body]
    assert len(selected) == 1
    return selected[0]


def _shell_input(block: str, root: Path) -> str:
    quoted = shlex.quote(str(root))
    block = block.replace("GRAPHIFY_INPUT_PATH='INPUT_PATH'", "GRAPHIFY_INPUT_PATH=" + quoted)
    return re.sub(r"(?<![A-Z_])INPUT_PATH(?![A-Z_])", lambda _: quoted, block)


def _fixture(tmp_path, monkeypatch, host="pi"):
    root = tmp_path / "corpus 'quoted' $(literal)"
    root.mkdir()
    source = root / "note 'quoted' $literal.md"
    source.write_text("A complete source.\n")
    out = root / "graphify-out"
    out.mkdir()
    graph = out / "graph.json"
    graph.write_text(json.dumps({"nodes": [{"id": "baseline", "label": "Baseline"}], "links": []}))
    manifest = out / "manifest.json"
    manifest.write_text('{"baseline": true}')
    baseline = {p.name: p.read_bytes() for p in [graph, manifest]}
    owner = tx.begin_transaction("update", root, output=out,
                                 expected_snapshot=tx.open_graph_snapshot(graph, purpose="disk-staging-test"))
    token = tx.stage_transaction_handoff(owner)
    prepared = prepared_path(token.path)
    artifacts = gen.render(gen.load_platforms()[host])
    core = artifacts[0].content
    spec = next(a.content for a in artifacts if a.path.endswith("/extraction-spec.md"))
    run = lambda body: run_body(body, token=token.path, root=root)
    run("from pathlib import Path; Path('.graphify_detect.json').write_text(" + repr(json.dumps(
        {"files": {"document": [str(source)]}})) + ")")
    b0 = _body(core, "cached_nodes, cached_edges, cached_hyperedges, uncached =")
    run(b0)
    intakes = tmp_path / "intakes"
    intakes.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(intakes))
    monkeypatch.setenv("GRAPHIFY_CHUNK_NUMBER", "01")
    monkeypatch.setenv("GRAPHIFY_CHUNK_FILES_JSON", json.dumps([str(source)]))
    monkeypatch.setenv("GRAPHIFY_CHUNK_USAGE_JSON", json.dumps({"input_tokens": 7, "output_tokens": 3}))
    setup = _body(spec, "tempfile.mkdtemp")
    collect = _body(spec, "# Existing native no-follow regular-file reader")
    run(setup)
    plan = json.loads((prepared / ".graphify_intake_plan_01.json").read_text())
    intake = Path(plan["intake"]) / "result.json"
    assert not intake.is_relative_to(out) and not intake.is_relative_to(prepared)
    return root, source, out, baseline, owner, prepared, run, intake, collect, b0, core


def _payload(source) -> dict:
    return {"nodes": [{"id": "fact", "label": "A fact", "source_file": str(source)}],
            "edges": [], "hyperedges": [], "completed_sources": [str(source)],
            "coverage": {str(source): 1}, "input_tokens": 7, "output_tokens": 3}


@pytest.mark.parametrize("host", DISK_HOSTS)
def test_disk_host_stages_and_registers_only_with_exact_prepared_owner(tmp_path, monkeypatch, host):
    root, source, out, baseline, owner, prepared, run, intake, collect, _, core = _fixture(
        tmp_path, monkeypatch, host)
    assert "Disk result staging" in core
    b2 = core.split("**Step B2", 1)[1].split("**Step B3", 1)[0]
    assert "PROJECT_ROOT" not in b2
    assert "graphify-out/.graphify_chunk" not in b2
    payload = _payload(source)
    # Synthetic disk callback, including the manual-host file-editor path; no provider call.
    intake.write_text(json.dumps(payload))
    with pytest.raises(tx.PendingTransactionError):
        tx.commit_prepared_bytes(owner, ".graphify_chunk_01.json", b"{}")
    assert not (prepared / ".graphify_chunk_01.json").exists()
    run(collect)
    assert json.loads((prepared / ".graphify_chunk_01.json").read_text()) == payload
    assert json.loads((prepared / ".graphify_completed_chunks.json").read_text()) == [
        str(prepared / ".graphify_chunk_01.json")]
    b3 = core.split("**Step B3", 1)[1].split("#### Part C", 1)[0]
    run(_body(b3, "Merged {len(chunks)} chunks:"))
    merged = json.loads((prepared / ".graphify_semantic_new.json").read_text())
    assert merged["nodes"] == payload["nodes"]
    assert merged["completed_sources"] == [str(source)]
    tx.abort_transaction(owner)
    assert {name: (out / name).read_bytes() for name in baseline} == baseline
    assert not list(out.glob(".graphify_chunk*.json"))
    assert intake.read_text() == json.dumps(payload)


def test_disk_result_can_exceed_inline_tool_limits_and_cannot_replay_prior_batch(tmp_path, monkeypatch):
    _, source, out, baseline, _, prepared, run, intake, collect, b0, _ = _fixture(tmp_path, monkeypatch)
    payload = _payload(source)
    payload["nodes"][0]["label"] = "large disk result " * 300_000
    intake.write_text(json.dumps(payload))
    run(collect)
    assert (prepared / ".graphify_chunk_01.json").stat().st_size > 4 * 1024 * 1024
    run(b0)
    with pytest.raises(ValueError, match="different owner or dispatch batch"):
        run(collect)
    assert json.loads((prepared / ".graphify_completed_chunks.json").read_text()) == []
    assert {name: (out / name).read_bytes() for name in baseline} == baseline
    assert intake.exists(), "failed/prior intake evidence must survive replay refusal"


@pytest.mark.parametrize("bad", ["missing", "symlink", "fifo", "oversized", "invalid-json", "directory-substitution"])
def test_untrusted_disk_intake_refuses_before_registration_and_keeps_diagnostics(tmp_path, monkeypatch, bad):
    _, _, out, baseline, _, prepared, run, intake, collect, _, _ = _fixture(tmp_path, monkeypatch)
    if bad == "symlink":
        target = tmp_path / "foreign.json"
        target.write_text('{"nodes": [], "edges": []}')
        intake.symlink_to(target)
    elif bad == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("native FIFO fixture unavailable")
        os.mkfifo(intake)
    elif bad == "oversized":
        with intake.open("wb") as handle:
            handle.truncate(512 * 1024 * 1024 + 1)
    elif bad == "invalid-json":
        intake.write_text("{broken")
    elif bad == "directory-substitution":
        intake.parent.rename(intake.parent.with_name("retained-original-intake"))
        intake.parent.mkdir()
        intake.write_text('{"nodes": [], "edges": []}')
    with pytest.raises((tx.PendingTransactionError, OSError, ValueError)):
        run(collect)
    assert not (prepared / ".graphify_chunk_01.json").exists()
    assert json.loads((prepared / ".graphify_completed_chunks.json").read_text()) == []
    assert (prepared / ".graphify_intake_plan_01.json").exists()
    assert intake.exists() or bad == "missing"
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


@pytest.mark.parametrize("kind", ["empty", "foreign", "truncated", "partial", "unowned", "incomplete", "ownership-only"])
def test_disk_completion_uses_existing_assignment_and_partial_validators(tmp_path, monkeypatch, kind):
    _, source, _, _, _, prepared, run, intake, collect, _, _ = _fixture(tmp_path, monkeypatch)
    payload = _payload(source)
    if kind == "empty":
        payload["nodes"] = []
    elif kind == "foreign":
        payload["nodes"][0]["source_file"] = str(source.parent / "unassigned.md")
    elif kind == "truncated":
        payload["finish_reason"] = "length"
    elif kind == "partial":
        payload["nodes"][0]["_partial"] = True
    elif kind == "unowned":
        del payload["nodes"][0]["source_file"]
    elif kind == "incomplete":
        payload["incomplete_sources"] = [str(source)]
    elif kind == "ownership-only":
        del payload["completed_sources"]
    intake.write_text(json.dumps(payload))
    if kind in ("foreign", "truncated"):
        with pytest.raises(ValueError, match="ownership is outside|failed chunk"):
            run(collect)
        assert not (prepared / ".graphify_chunk_01.json").exists()
        assert json.loads((prepared / ".graphify_completed_chunks.json").read_text()) == []
        return
    run(collect)
    staged = json.loads((prepared / ".graphify_chunk_01.json").read_text())
    assert staged["completed_sources"] == ([str(source)] if kind == "empty" else [])
    assert intake.read_text() == json.dumps(payload), "retain raw completion/partial evidence"


@pytest.mark.parametrize("assignment", [[], ["outside.md"], ["duplicate", "duplicate"], "not-a-list"])
def test_allocation_refuses_invalid_current_assignment(tmp_path, monkeypatch, assignment):
    _, _, _, _, _, prepared, run, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    spec = next(a.content for a in gen.render(gen.load_platforms()["pi"])
                if a.path.endswith("/extraction-spec.md"))
    monkeypatch.setenv("GRAPHIFY_CHUNK_NUMBER", "02")
    monkeypatch.setenv("GRAPHIFY_CHUNK_FILES_JSON", json.dumps(assignment))
    with pytest.raises(ValueError, match="current uncached"):
        run(_body(spec, "tempfile.mkdtemp"))
    assert not (prepared / ".graphify_intake_plan_02.json").exists()


@pytest.mark.parametrize("field", ["owner", "number", "batch"])
def test_import_refuses_plan_from_other_owner_or_assignment(tmp_path, monkeypatch, field):
    _, source, _, _, _, prepared, run, intake, collect, _, _ = _fixture(tmp_path, monkeypatch)
    intake.write_text(json.dumps(_payload(source)))
    plan_path = prepared / ".graphify_intake_plan_01.json"
    plan = json.loads(plan_path.read_text())
    plan[field] = "different"
    run("from pathlib import Path; Path('.graphify_intake_plan_01.json').write_text(" + repr(json.dumps(plan)) + ")")
    with pytest.raises(ValueError, match="different owner or dispatch batch"):
        run(collect)
    assert not (prepared / ".graphify_chunk_01.json").exists()
    assert json.loads((prepared / ".graphify_completed_chunks.json").read_text()) == []
    assert intake.is_file()


@pytest.mark.parametrize("location", ["public", "prepared"])
def test_intake_storage_cannot_be_inside_either_output(tmp_path, monkeypatch, location):
    _, _, out, _, _, prepared, run, _, _, _, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(tempfile, "tempdir", str(out if location == "public" else prepared))
    monkeypatch.setenv("GRAPHIFY_CHUNK_NUMBER", "02")
    run("from pathlib import Path; Path('.graphify_dispatch_batch.txt').write_text('fresh-batch')")
    spec = next(a.content for a in gen.render(gen.load_platforms()["pi"])
                if a.path.endswith("/extraction-spec.md"))
    with pytest.raises(ValueError, match="outside public and prepared"):
        run(_body(spec, "tempfile.mkdtemp"))
    assert not (prepared / ".graphify_intake_plan_02.json").exists()
    assert not list(out.glob("graphify-chunk-*"))
    assert not list(prepared.glob("graphify-chunk-*"))


@pytest.mark.parametrize("damage", ["failed", "error", "failed-finish", "bad-records", "bad-completion"])
def test_failed_or_malformed_result_never_registers_as_success(tmp_path, monkeypatch, damage):
    _, source, out, baseline, _, prepared, run, intake, collect, _, _ = _fixture(tmp_path, monkeypatch)
    payload = _payload(source)
    if damage == "failed":
        payload["failed_chunks"] = 1
    elif damage == "error":
        payload["error"] = "source read failed"
    elif damage == "failed-finish":
        payload["finish_reason"] = "error"
    elif damage == "bad-records":
        payload["nodes"] = [None]
    else:
        payload["completed_sources"] = "not-a-list"
    intake.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        run(collect)
    assert not (prepared / ".graphify_chunk_01.json").exists()
    assert json.loads((prepared / ".graphify_completed_chunks.json").read_text()) == []
    assert not (prepared / ".graphify_semantic_new.json").exists()
    assert not list(out.glob("cache/semantic/*.json"))
    assert {name: (out / name).read_bytes() for name in baseline} == baseline
    assert intake.read_text() == json.dumps(payload)


@pytest.mark.parametrize("kind", ["empty", "partial", "ownership-only"])
def test_staging_and_v8_cache_completion_do_not_publish_graph(tmp_path, monkeypatch, kind):
    root, source, out, baseline, _, prepared, run, intake, collect, b0, core = _fixture(tmp_path, monkeypatch)
    payload = _payload(source)
    if kind == "empty":
        payload["nodes"] = []
    elif kind == "partial":
        payload["nodes"][0]["_partial"] = True
    else:
        payload.pop("completed_sources")
    intake.write_text(json.dumps(payload))
    run(collect)
    assert not list(out.glob("cache/semantic/*.json")), "import is separate from cache save"
    b3 = core.split("**Step B3", 1)[1].split("#### Part C", 1)[0]
    run(_body(b3, "Merged {len(chunks)} chunks:"))
    run(_body(b3, "saved = save_semantic_cache("))
    run(b0)
    uncached = (prepared / ".graphify_uncached.txt").read_text().splitlines()
    assert uncached == ([] if kind == "empty" else [str(source)])
    assert {name: (out / name).read_bytes() for name in baseline} == baseline
    assert not list(out.glob(".graphify_chunk*.json"))


def test_windows_disk_staging_recipe_keeps_native_literal_python_and_exact_runner():
    import tree_sitter_powershell
    from tree_sitter import Language, Parser
    artifacts = gen.render(gen.load_platforms()["windows"])
    reference = next(a.content for a in artifacts if a.path.endswith("/extraction-spec.md"))
    blocks = re.findall(r"```powershell\n(.*?)\n```", reference, re.S)
    assert len(blocks) == 2
    parser = Parser(Language(tree_sitter_powershell.language()))
    for block in blocks:
        assert not parser.parse(block.encode()).root_node.has_error
        assert "run-prepared-token $Env:GRAPHIFY_TRANSACTION_TOKEN '--' -c" in block
        assert "$GraphifyPythonSource = @'" in block


@pytest.mark.parametrize("host", ["pi", "vscode"])
def test_disk_parent_runs_actual_quoted_shell_import_with_no_public_chunk(tmp_path, monkeypatch, host):
    root, _, out, baseline, _, prepared, run, _, _, _, _ = _fixture(tmp_path, monkeypatch, host)
    source = root / "second 'quoted' $(literal).md"
    source.write_text("Another complete source.\n")
    run("from pathlib import Path; Path('.graphify_uncached.txt').write_text(" + repr(str(source)) + ")")
    payload = _payload(source)
    reference = next(a.content for a in gen.render(gen.load_platforms()[host])
                     if a.path.endswith("/extraction-spec.md"))
    environment = {**os.environ, "VIRTUAL_ENV": sys.prefix, "TMPDIR": str(tmp_path / "intakes"),
                   "GRAPHIFY_CHUNK_NUMBER": "02", "GRAPHIFY_CHUNK_FILES_JSON": json.dumps([str(source)]),
                   "GRAPHIFY_TRANSACTION_TOKEN": str(tx.active_transaction_token_path(out))}
    allocation = next(b for b in re.findall(r"```bash\n(.*?)\n```", reference, re.S)
                      if "tempfile.mkdtemp" in b)
    allocated = subprocess.run(["/bin/bash", "-c", _shell_input(allocation, root)], cwd=root, text=True,
                               capture_output=True, env=environment, check=False, timeout=30)
    assert allocated.returncode == 0, allocated.stderr
    intake = Path(json.loads(allocated.stdout)["CHUNK_PATH"])
    assert not intake.is_relative_to(out) and not intake.is_relative_to(prepared)
    intake.write_text(json.dumps(payload))
    block = next(b for b in re.findall(r"```bash\n(.*?)\n```", reference, re.S)
                 if "# Existing native no-follow regular-file reader" in b)
    result = subprocess.run(
        ["/bin/bash", "-c", _shell_input(block, root)], cwd=root, text=True, capture_output=True,
        env=environment,
        check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Staged and registered chunk 02" in result.stdout
    assert json.loads((prepared / ".graphify_chunk_02.json").read_text()) == payload
    assert {name: (out / name).read_bytes() for name in baseline} == baseline
    assert not list(out.glob(".graphify_chunk*.json"))


@pytest.mark.parametrize("host", ["pi", "vscode"])
@pytest.mark.parametrize("configured_output", [False, True])
def test_staging_rebinds_live_token_in_separate_shell_calls(tmp_path, monkeypatch, host, configured_output):
    root, source, out, baseline, _, prepared, run, _, _, _, _ = _fixture(tmp_path, monkeypatch, host)
    reference = next(a.content for a in gen.render(gen.load_platforms()[host])
                     if a.path.endswith("/extraction-spec.md"))
    blocks = re.findall(r"```bash\n(.*?)\n```", reference, re.S)
    allocation = next(b for b in blocks if "tempfile.mkdtemp" in b)
    collect = next(b for b in blocks if "# Existing native no-follow regular-file reader" in b)
    # Earlier tool-call exports do not reach either of these fresh shells.
    environment = {**os.environ, "VIRTUAL_ENV": sys.prefix, "TMPDIR": str(tmp_path / "intakes"),
                   "GRAPHIFY_CHUNK_NUMBER": "02", "GRAPHIFY_CHUNK_FILES_JSON": json.dumps([str(source)])}
    environment.pop("GRAPHIFY_TRANSACTION_TOKEN", None)
    if configured_output:
        environment["GRAPHIFY_OUT"] = str(out)
    else:
        environment.pop("GRAPHIFY_OUT", None)
    run("from pathlib import Path; Path('.graphify_dispatch_batch.txt').write_text('fresh-shell-batch')")

    def shell(block):
        return subprocess.run(["/bin/bash", "-c", _shell_input(block, root)], cwd=root, env=environment,
                              capture_output=True, text=True, check=False, timeout=30)

    allocated = shell(allocation)
    assert allocated.returncode == 0, allocated.stderr
    intake = Path(json.loads(allocated.stdout)["CHUNK_PATH"])
    intake.write_text(json.dumps(_payload(source)))
    imported = shell(collect)
    assert imported.returncode == 0, imported.stderr
    assert json.loads((prepared / ".graphify_completed_chunks.json").read_text()) == [
        str(prepared / ".graphify_chunk_02.json")]
    # An explicitly retained wrong token must fail, never switch to the live owner.
    environment["GRAPHIFY_TRANSACTION_TOKEN"] = str(root / "wrong-token")
    refused = shell(collect)
    assert refused.returncode != 0
    assert {name: (out / name).read_bytes() for name in baseline} == baseline
    assert intake.is_file()


def test_manual_prompt_substitutes_allocated_intake_path():
    core = gen.render(gen.load_platforms()["vscode"])[0].content
    dispatch = core.split("Subagent prompt template:", 1)[1].split("**Step B3", 1)[0]
    assert "DEEP_MODE, and CHUNK_PATH substituted" in dispatch


@pytest.mark.parametrize("records", [
    {"nodes": [{}]}, {"nodes": [{"id": []}]}, {"nodes": [{"id": 7}]},
    {"edges": [{}]}, {"edges": [{"source": "fact"}]},
    {"edges": [{"source": None, "target": "fact"}]},
    {"edges": [{"from": "fact", "to": False}]},
    {"hyperedges": [{"id": [], "nodes": ["fact"]}]},
    {"hyperedges": [{"id": "group"}]},
    {"hyperedges": [{"id": "group", "nodes": [None]}]},
    {"hyperedges": [{"members": [7]}]},
])
def test_malformed_record_identities_refuse_before_commit(tmp_path, monkeypatch, records):
    _, source, out, baseline, _, prepared, run, intake, collect, _, _ = _fixture(tmp_path, monkeypatch)
    payload = _payload(source)
    payload.update(records)
    intake.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="identit|hyperedge"):
        run(collect)
    assert not (prepared / ".graphify_chunk_01.json").exists()
    assert json.loads((prepared / ".graphify_completed_chunks.json").read_text()) == []
    assert intake.read_text() == json.dumps(payload)
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


@pytest.mark.parametrize("group_id", [None, "", 7, False])
@pytest.mark.parametrize("member_key", ["nodes", "members", "node_ids"])
def test_intake_preserves_v8_identity_aliases_and_anonymous_groups(tmp_path, monkeypatch, group_id, member_key):
    _, source, _, _, _, prepared, run, intake, collect, _, _ = _fixture(tmp_path, monkeypatch)
    payload = _payload(source)
    payload["nodes"][0]["id"] = ""  # v8 admits empty string identities.
    payload["edges"] = [{"from": "", "to": "", "source_file": str(source)}]
    payload["hyperedges"] = [{"id": group_id, member_key: [""], "source_file": str(source)}]
    intake.write_text(json.dumps(payload))
    run(collect)
    assert json.loads((prepared / ".graphify_chunk_01.json").read_text()) == payload


def test_disk_import_commits_one_captured_immutable_payload(tmp_path, monkeypatch):
    _, source, _, _, _, prepared, run, intake, collect, _, _ = _fixture(tmp_path, monkeypatch)
    payload = _payload(source)
    intake.write_text(json.dumps(payload))
    original = tx._read_relative_bytes
    reads = []

    def capture(capability, name, limit=512 * 1024 * 1024):
        data = original(capability, name, limit)
        if capability.path == intake.parent and name == "result.json":
            reads.append(data)
            intake.write_text('{"nodes": [], "edges": []}')
        return data

    monkeypatch.setattr(tx, "_read_relative_bytes", capture)
    run(collect)
    assert len(reads) == 1
    assert json.loads((prepared / ".graphify_chunk_01.json").read_text()) == payload
    assert json.loads(intake.read_text()) == {"nodes": [], "edges": []}


def test_intake_identity_and_reads_request_no_mutation_authority(tmp_path, monkeypatch):
    original = tx.pin_output
    calls = []

    def pin(path, **kwargs):
        if Path(path).name.startswith("graphify-chunk-"):
            calls.append(kwargs)
            assert kwargs.get("mutation") is False
        return original(path, **kwargs)

    monkeypatch.setattr(tx, "pin_output", pin)
    _, source, _, _, _, _, run, intake, collect, _, _ = _fixture(tmp_path, monkeypatch)
    intake.write_text(json.dumps(_payload(source)))
    run(collect)
    assert calls == [{"mutation": False}, {"mutation": False}]


@pytest.mark.parametrize("host", ["claw", "kiro", "pi"])
def test_compact_disk_prompt_itself_contains_intake_write_and_notice(host):
    reference = next(a.content for a in gen.render(gen.load_platforms()[host])
                     if a.path.endswith("/extraction-spec.md"))
    prompt = reference.split("```", 2)[1]
    assert "CHUNK_PATH" in prompt
    assert "Write" in prompt and "completion notice" in prompt
    assert "Output ONLY valid JSON" not in prompt
    assert "Output exactly this JSON" not in prompt


@pytest.mark.parametrize("host", DISK_HOSTS)
def test_disk_collection_uses_prepared_manifest_and_imported_usage(host):
    core = gen.render(gen.load_platforms()[host])[0].content
    b3 = core.split("**Step B3", 1)[1].split("#### Part C", 1)[0]
    assert "graphify-out/.graphify_chunk_NN.json" not in b3
    assert "skip that chunk" not in b3
    assert "write them back into the chunk JSON" not in b3
    assert "Disk result staging" in b3
    assert "GRAPHIFY_CHUNK_USAGE_JSON" in b3


def test_allocation_rejects_overlapping_chunks_only_in_current_batch(tmp_path, monkeypatch):
    _, source, out, baseline, _, prepared, run, intake, _, b0, _ = _fixture(tmp_path, monkeypatch)
    reference = next(a.content for a in gen.render(gen.load_platforms()["pi"])
                     if a.path.endswith("/extraction-spec.md"))
    allocate = _body(reference, "tempfile.mkdtemp")
    before = sorted(intake.parent.parent.iterdir())
    monkeypatch.setenv("GRAPHIFY_CHUNK_NUMBER", "02")
    with pytest.raises(ValueError, match="already assigned"):
        run(allocate)
    assert not (prepared / ".graphify_intake_plan_02.json").exists()
    assert sorted(intake.parent.parent.iterdir()) == before
    # Disjoint assignments in this batch still work.
    other = source.with_name("another.md")
    other.write_text("Another source")
    run("from pathlib import Path; Path('.graphify_uncached.txt').write_text(" +
        repr(str(source) + "\n" + str(other)) + ")")
    monkeypatch.setenv("GRAPHIFY_CHUNK_FILES_JSON", json.dumps([str(other)]))
    run(allocate)
    # A new B0 batch can assign the original file again; old plans remain evidence.
    run(b0)
    monkeypatch.setenv("GRAPHIFY_CHUNK_NUMBER", "03")
    monkeypatch.setenv("GRAPHIFY_CHUNK_FILES_JSON", json.dumps([str(source)]))
    run(allocate)
    assert (prepared / ".graphify_intake_plan_03.json").exists()
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


@pytest.mark.parametrize("usage", [{"input_tokens": 101, "output_tokens": 29},
                                  {"input_tokens": 0, "output_tokens": 0}, None])
def test_parent_usage_is_applied_before_prepared_commit_and_merge(tmp_path, monkeypatch, usage, capsys):
    _, source, out, baseline, _, prepared, run, intake, collect, _, core = _fixture(tmp_path, monkeypatch)
    payload = _payload(source)
    payload.update(input_tokens=999, output_tokens=999)
    intake.write_text(json.dumps(payload))
    monkeypatch.setenv("GRAPHIFY_CHUNK_USAGE_JSON", json.dumps(usage))
    run(collect)
    staged = json.loads((prepared / ".graphify_chunk_01.json").read_text())
    expected = usage or {"input_tokens": 0, "output_tokens": 0}
    assert {key: staged[key] for key in expected} == expected
    if usage is None:
        assert "unavailable" in capsys.readouterr().out
    b3 = core.split("**Step B3", 1)[1].split("#### Part C", 1)[0]
    run(_body(b3, "Merged {len(chunks)} chunks:"))
    merged = json.loads((prepared / ".graphify_semantic_new.json").read_text())
    assert {key: merged[key] for key in expected} == expected
    assert intake.read_text() == json.dumps(payload)
    assert {name: (out / name).read_bytes() for name in baseline} == baseline


@pytest.mark.parametrize("usage", ["missing", [], {}, {"input_tokens": -1, "output_tokens": 3},
                                  {"input_tokens": True, "output_tokens": 3},
                                  {"input_tokens": 1.5, "output_tokens": 3}])
def test_invalid_parent_usage_refuses_before_registration(tmp_path, monkeypatch, usage):
    _, source, _, _, _, prepared, run, intake, collect, _, _ = _fixture(tmp_path, monkeypatch)
    intake.write_text(json.dumps(_payload(source)))
    if usage == "missing":
        monkeypatch.delenv("GRAPHIFY_CHUNK_USAGE_JSON")
    else:
        monkeypatch.setenv("GRAPHIFY_CHUNK_USAGE_JSON", json.dumps(usage))
    with pytest.raises((ValueError, KeyError)):
        run(collect)
    assert not (prepared / ".graphify_chunk_01.json").exists()
    assert json.loads((prepared / ".graphify_completed_chunks.json").read_text()) == []
