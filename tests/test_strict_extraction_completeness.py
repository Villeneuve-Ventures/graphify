"""Operational failures must not become successful partial strict AST refreshes."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

import pytest


# A file-backed main module keeps initializer functions importable under spawn.
# The real executor, worker entrypoint, extractor and publisher remain in use.
_PUBLIC_DRIVER = r'''
import concurrent.futures
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time

ORIGINAL_POOL = concurrent.futures.ProcessPoolExecutor
READ_BYTES = Path.read_bytes
READ_TEXT = Path.read_text
STAT = os.stat
SCANDIR = os.scandir

def record(event, **details):
    path = Path(os.environ["COMPLETENESS_EVENTS"]) / (str(os.getpid()) + ".jsonl")
    with path.open("a") as stream:
        stream.write(json.dumps(dict(event=event, pid=os.getpid(), **details)) + "\n")

def in_stack(name):
    frame = sys._getframe(1)
    while frame:
        if frame.f_code.co_name == name:
            return True
        frame = frame.f_back
    return False

def install():
    import graphify.llm as llm
    def forbidden_provider(*args, **kwargs):
        record("provider_call")
        raise AssertionError("provider invocation forbidden in code-only fixture")
    for name in ("_call_llm", "_call_openai_compat", "_call_claude", "_call_claude_cli",
                 "_call_azure", "_call_bedrock"):
        assert callable(getattr(llm, name))
        setattr(llm, name, forbidden_provider)
    target = Path(os.environ["COMPLETENESS_TARGET"])
    parallel = os.environ["COMPLETENESS_PARALLEL"] == "1"
    fail = os.environ["COMPLETENESS_FAULT"] == "1"
    family = os.environ["COMPLETENESS_KIND"]
    classifier = {"cpp": "_is_cpp_header", "groovy": "_is_spock_file",
                  "baseline_stat": "_is_cpp_header", "tsconfig": "_read_tsconfig_aliases",
                  "svelte_supplement": "_read_tsconfig_aliases",
                  "xaml": "_xaml_codebehind_symbols", "apex": "extract_apex"}.get(family, "")
    if family in {"tsconfig", "workspace", "xaml", "pascal"}:
        import graphify.extract as extraction
        import graphify.extractors.resolution as resolution
        root = Path.cwd()
        if family == "tsconfig":
            warm = resolution._load_tsconfig_aliases(root)
        elif family == "workspace":
            warm = resolution._load_workspace_packages(root)
        elif family == "pascal":
            warm = resolution._pascal_resolve_unit(root / "Main.pas", "Peer")
        else:
            extraction._XAML_ACTIVE_EXTRACT_ROOT = root
            warm = extraction._xaml_csharp_class_nodes(root / "View.xaml")
        assert warm
        record("cache_warm", family=family, keys=sorted(warm) if isinstance(warm, dict) else warm)
    def scheduled():
        worker = os.getpid() != int(os.environ["COMPLETENESS_PARENT"])
        return worker if parallel else in_stack("_extract_sequential")
    cold_config_prepared = False
    def read(path, original, *args, **kwargs):
        nonlocal cold_config_prepared
        worker = os.getpid() != int(os.environ["COMPLETENESS_PARENT"])
        if (family == "svelte_supplement" and parallel and worker
                and path == Path.cwd() / "Page.svelte" and original is READ_BYTES
                and in_stack("_extract_generic") and not cold_config_prepared):
            import graphify.extractors.resolution as resolution
            resolution._TSCONFIG_ALIAS_CACHE.clear()
            cold_config_prepared = True
            record("cold_config_setup", path=str(path), entries=len(resolution._TSCONFIG_ALIAS_CACHE))
        selected = path == target and bool(classifier) and in_stack(classifier)
        if family == "svelte_supplement":
            frame = sys._getframe(1)
            component = None
            while frame:
                if frame.f_code.co_name == "extract_svelte":
                    component = frame.f_locals.get("path")
                    break
                frame = frame.f_back
            selected = selected and (not parallel or component == Path.cwd() / "Page.svelte") and not in_stack("_extract_generic")
        if selected:
            record("classification", path=str(path), helper=classifier, worker=worker)
        if fail and selected and scheduled() and family != "baseline_stat":
            record("fault", path=str(path), helper=classifier)
            raise OSError(5, "injected selected classifier read")
        value = original(path, *args, **kwargs)
        if path.suffix == ".py" and in_stack("_extract_generic"):
            record("healthy_source", path=str(path), worker=worker)
            if worker:
                time.sleep(0.02)
        return value
    Path.read_bytes = lambda path, *a, **kw: read(path, READ_BYTES, *a, **kw)
    Path.read_text = lambda path, *a, **kw: read(path, READ_TEXT, *a, **kw)
    if family in {"workspace", "pascal"}:
        helper = "_load_workspace_packages" if family == "workspace" else "_pascal_resolve_unit"
        class PartialScan:
            def __init__(self, path):
                self.iterator = SCANDIR(path)
                self.yielded = False
            def __enter__(self):
                self.iterator.__enter__()
                return self
            def __exit__(self, *args):
                return self.iterator.__exit__(*args)
            def __iter__(self):
                return self
            def __next__(self):
                if self.yielded:
                    record("fault", path=str(target), helper=helper)
                    raise OSError(5, "injected selected partial enumeration")
                entry = next(self.iterator)
                self.yielded = True
                record("partial_yield", path=str(target), name=entry.name)
                return entry
        def scan(path):
            if fail and Path(path) == target and in_stack(helper) and scheduled():
                return PartialScan(path)
            return SCANDIR(path)
        os.scandir = scan
    if family == "dynamic":
        def dynamic_stat(path, *args, **kwargs):
            if fail and Path(path) == target and in_stack("_dynamic_import_js") and scheduled():
                record("fault", path=str(path), helper="_dynamic_import_js")
                raise OSError(5, "injected dynamic import target metadata")
            return STAT(path, *args, **kwargs)
        os.stat = dynamic_stat
    if family == "baseline_stat":
        def stat(path, *args, **kwargs):
            if fail and isinstance(path, (str, os.PathLike)) and Path(path).name == "graph.json":
                frame = sys._getframe(1)
                while frame:
                    if (frame.f_code.co_name == "_dispatch_command"
                            and frame.f_locals.get("existing_graph_path") == Path(path)
                            and "incremental_mode" not in frame.f_locals):
                        record("fault", path=str(path), helper="baseline_exists_stat")
                        raise OSError(5, "injected baseline availability stat")
                    frame = frame.f_back
            return STAT(path, *args, **kwargs)
        os.stat = stat

def initialize():
    record("initializer", context=multiprocessing.get_start_method())
    install()

class ObservedPool(ORIGINAL_POOL):
    def __init__(self, *args, **kwargs):
        assert kwargs.get("max_workers") == 2
        super().__init__(*args, initializer=initialize, **kwargs)

if __name__ == "__main__":
    os.environ["COMPLETENESS_PARENT"] = str(os.getpid())
    record("driver", context=multiprocessing.get_start_method())
    install()
    concurrent.futures.ProcessPoolExecutor = ObservedPool
    from graphify.__main__ import main
    sys.argv = ["graphify", "extract", ".", "--code-only", "--no-viz"]
    main()
'''


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _events(directory):
    return [json.loads(line) for path in directory.glob("*.jsonl")
            for line in path.read_text().splitlines()]


def _public_run(repo, driver, target, parallel, fault, events, family):
    events.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.endswith("API_KEY") and key not in {"OPENAI_BASE_URL"}}
    env.update(GRAPHIFY_OUT=str(repo / "graphify-out"), GRAPHIFY_MAX_WORKERS="2",
               COMPLETENESS_TARGET=str(target), COMPLETENESS_EVENTS=str(events),
               COMPLETENESS_PARALLEL=str(int(parallel)), COMPLETENESS_FAULT=str(int(fault)),
               COMPLETENESS_KIND=family)
    result = subprocess.run([sys.executable, str(driver)], cwd=repo, env=env,
                            capture_output=True, text=True, timeout=120)
    (events / "stdout.txt").write_text(result.stdout)
    (events / "stderr.txt").write_text(result.stderr)
    return result


def _logical_reference(repo, paths):
    from graphify.build import build
    from graphify.extract import extract

    graph = build([extract(paths, cache_root=repo, parallel=False, strict=True)], root=repo)
    provenance = ("relation", "confidence", "source_file", "source_location", "_origin")
    nodes = set(graph.nodes)
    edges = {(attrs["_src"], attrs["_tgt"], *(attrs.get(k) for k in provenance))
             for _, _, attrs in graph.edges(data=True)}
    return nodes, edges


def _assert_refused_publication(before, after, repo, edited_source, driver_pid):
    """Bind the three native failed-attempt exceptions; reject all other writes."""
    from graphify.transaction import RECEIPT_FILE

    allowed = {".graphify_protocol.json", ".graphify_drainer.json", "cache/stat-index.json"}
    transaction_name = ".graphify_transaction.json"
    assert after.keys() - before.keys() == {transaction_name}
    assert not before.keys() - after.keys()
    for name in before.keys() - allowed:
        assert before[name] == after[name], f"committed/existing artifact changed: {name}"
    receipt = json.loads(before[RECEIPT_FILE])
    assert after[RECEIPT_FILE] == before[RECEIPT_FILE]
    for name, digest in receipt["artifact_digests"].items():
        assert hashlib.sha256(after[name]).hexdigest() == digest
    generation = receipt["generation"] + 1
    protocol = json.loads(after[".graphify_protocol.json"])
    drainer = json.loads(after[".graphify_drainer.json"])
    transaction = json.loads(after[transaction_name])
    old_protocol = json.loads(before[".graphify_protocol.json"])
    old_drainer = json.loads(before[".graphify_drainer.json"])
    protocol_fields = {"state", "generation", "transaction_id", "owner_capability_digest",
                       "bootstrap_nonce", "lease_deadline", "receipt_digest"}
    assert protocol.keys() == old_protocol.keys() - {"receipt_digest"}
    assert {k: v for k, v in protocol.items() if k not in protocol_fields} == {
        k: v for k, v in old_protocol.items() if k not in protocol_fields}
    assert protocol["state"] == "INCOMPLETE" and protocol["generation"] == generation
    assert protocol["transaction_id"] == transaction["id"] != old_protocol["transaction_id"]
    assert protocol["owner_capability_digest"] == transaction["token_digest"]
    for key, length in (("transaction_id", 64), ("owner_capability_digest", 64), ("bootstrap_nonce", 32)):
        assert re.fullmatch(rf"[0-9a-f]{{{length}}}", protocol[key])
        assert protocol[key] != old_protocol[key]
    assert protocol["lease_deadline"] > 0
    assert drainer.keys() == {"acked_ids", "claim_epoch", "generation", "launch_nonce",
                              "lease_deadline", "protocol_epoch", "schema", "state"}
    for key in ("acked_ids", "claim_epoch", "protocol_epoch", "schema"):
        assert drainer[key] == old_drainer[key]
    assert drainer["state"] == "claimed" and drainer["generation"] == generation
    assert re.fullmatch(r"[0-9a-f]{32}", drainer["launch_nonce"])
    assert drainer["launch_nonce"] != old_drainer["launch_nonce"]
    assert drainer["lease_deadline"] > 0
    claim = {key: drainer[key] for key in ("claim_epoch", "generation", "launch_nonce")}
    assert transaction == {
        "schema": 1, "protocol_epoch": receipt["protocol_epoch"], "phase": "building",
        "generation": generation, "id": protocol["transaction_id"], "kind": protocol["kind"],
        "pid": driver_pid, "root": str(repo), "output": str(repo / "graphify-out"),
        "output_identity": receipt["output_identity"], "token_identity": protocol["token_identity"],
        "token_digest": protocol["owner_capability_digest"], "drainer": claim,
    }
    stat_name = "cache/stat-index.json"
    old_index, new_index = json.loads(before[stat_name]), json.loads(after[stat_name])
    assert old_index.keys() == new_index.keys()
    for key in old_index:
        if key != str(edited_source) or old_index[key] == new_index[key]:
            assert old_index[key] == new_index[key]
        else:
            info = edited_source.stat()
            assert new_index[key] == {**old_index[key], "size": info.st_size,
                                      "mtime_ns": info.st_mtime_ns,
                                      "word_count": len(edited_source.read_text().split())}


_PUBLIC_FAMILIES = {"cpp": "P01", "groovy": "P02", "tsconfig": "P03", "workspace": "P04",
                    "xaml": "P05", "pascal": "P06", "dynamic": "P07", "apex": "P08"}


@pytest.mark.parametrize("family,parallel", [
    (family, parallel) for family in _PUBLIC_FAMILIES for parallel in (False, True)
] + [("baseline_stat", False), ("svelte_supplement", False), ("svelte_supplement", True)], ids=[
    f"{case}-{'P' if parallel else 'S'}" for case in _PUBLIC_FAMILIES.values() for parallel in (False, True)
] + ["F072", "R001", "R005"])
def test_public_classifier_failure_withholds_growing_refresh(tmp_path, family, parallel):
    from graphify.build import build
    from graphify.detect import save_manifest
    from graphify.export import to_json
    from graphify.extract import extract
    from graphify.transaction import RECEIPT_FILE

    repo = tmp_path / "corpus"
    repo.mkdir()
    if family in {"cpp", "groovy", "baseline_stat"}:
        target = _write(repo / ("Feature.groovy" if family == "groovy" else "widget.h"),
                        "class Widget { public: int calculate(); };\n" if family != "groovy" else
                        'class Feature {\n  def "keeps selected evidence"() {\n    expect: true\n  }\n}\n')
        expected_label = '\"keeps selected evidence\"' if family == "groovy" else "Widget"
        paths = [target]
    elif family == "svelte_supplement":
        source, child, target = _supplement_fixture(repo, "svelte")
        paths = [source, child, target]
        expected_label = "Page.svelte"
    elif family in {"tsconfig", "workspace", "dynamic"}:
        module = _write(repo / "packages/types/index.ts", "export type Helper = string\n")
        source = _write(repo / "page.ts", "import type { Helper } from '@types'\nconst value: Helper = 'x'\n")
        paths = [source, module]
        if family == "tsconfig":
            target = _write(repo / "tsconfig.json", json.dumps({"compilerOptions": {"paths": {"@types": ["packages/types/index.ts"]}}}))
        elif family == "workspace":
            _write(repo / "package.json", '{"workspaces":["packages/*"]}')
            _write(repo / "packages/types/package.json", '{"name":"@types","exports":"./index.ts"}')
            target = repo / "packages"
        else:
            source.write_text("export async function load() { return import('./packages/types/index.ts'); }\n")
            target = module
        expected_label = "page.ts"
    elif family == "xaml":
        source, target, vm = _xaml_fixture(repo)
        paths = [source, target, vm]
        expected_label = "View"
    elif family == "pascal":
        source = _write(repo / "Main.pas", "unit Main;\ninterface\nuses Peer;\nimplementation\nend.")
        peer = _write(repo / "sub/Peer.pas", "unit Peer;\ninterface\nimplementation\nend.")
        target = peer.parent
        paths = [source, peer]
        expected_label = "Main"
    else:
        target = _write(repo / "Account.cls", "public class Account { public void save() {} }\n")
        paths = [target]
        expected_label = "Account"
    for index in range(24 if parallel else 2):
        paths.append(_write(repo / f"peer_{index:02}.py", f"def peer_{index}():\n    return {index}\n"))
    paths.extend(path for path in repo.rglob("*")
                 if path.is_file() and path not in paths)
    _write(repo / ".gitignore", "graphify-out/\n")
    document = _write(repo / "facts.md", "Retained observation and supporting evidence.\n")
    for args in (("init", "-q"), ("add", "."),
                 ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "baseline")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    output = repo / "graphify-out"
    output.mkdir()
    graph_path = output / "graph.json"
    baseline = extract(paths, cache_root=repo, parallel=False, strict=True)
    assert any(node["label"] == expected_label for node in baseline["nodes"])
    if family == "svelte_supplement":
        _assert_supplement_edge(baseline, "Page.svelte", "Child.svelte")
    to_json(build([baseline], root=repo), {}, str(graph_path))
    seed = json.loads(graph_path.read_text())
    semantic_nodes = [{"id": name, "label": name, "file_type": "concept",
                       "source_file": "facts.md", "source_location": "L1",
                       "confidence": "EXTRACTED", "quotation": document.read_text().strip()}
                      for name in ("retained_observation", "retained_evidence")]
    semantic_edge = {"source": "retained_evidence", "target": "retained_observation",
                     "relation": "supports", "confidence": "EXTRACTED",
                     "source_file": "facts.md", "source_location": "L1"}
    semantic_hyperedge = {"id": "retained_pair", "nodes": [n["id"] for n in semantic_nodes],
                         "source_file": "facts.md", "confidence": "EXTRACTED"}
    seed["nodes"].extend(semantic_nodes)
    seed["links"].append(semantic_edge)
    seed["hyperedges"] = [semantic_hyperedge]
    graph_path.write_text(json.dumps(seed))
    save_manifest({"code": [str(path) for path in paths], "document": [str(document)]},
                  str(output / "manifest.json"), root=repo)
    driver = _write(tmp_path / "driver.py", textwrap.dedent(_PUBLIC_DRIVER))
    healthy = _public_run(repo, driver, target, parallel, False, tmp_path / "healthy", family)
    assert healthy.returncode == 0, healthy.stdout + healthy.stderr
    assert (output / RECEIPT_FILE).is_file(), "real public prepared baseline receipt required"
    published = json.loads(graph_path.read_text())
    assert any(node["label"] == expected_label for node in published["nodes"])
    if family == "svelte_supplement":
        _assert_supplement_edge({"nodes": published["nodes"], "edges": published["links"]}, "Page.svelte", "Child.svelte")
    expected_nodes, expected_edges = _logical_reference(repo, paths)
    provenance = ("relation", "confidence", "source_file", "source_location", "_origin")
    retained_ids = {node["id"] for node in semantic_nodes}
    assert {node["id"] for node in published["nodes"]} - retained_ids == expected_nodes
    assert {(edge["source"], edge["target"], *(edge.get(k) for k in provenance))
            for edge in published["links"] if edge["source"] not in retained_ids} == expected_edges
    for expected in semantic_nodes:
        actual = next(node for node in published["nodes"] if node["id"] == expected["id"])
        assert {key: actual[key] for key in expected} == expected
    assert any(all(edge.get(k) == v for k, v in semantic_edge.items()) for edge in published["links"])
    assert published["hyperedges"] == [semantic_hyperedge]
    # Inventory every output byte; the approved native failure-state exceptions
    # are checked relationally below rather than excluded from observation.
    receipt = json.loads((output / RECEIPT_FILE).read_text())
    managed = set(receipt["artifact_digests"]) | {RECEIPT_FILE}
    assert {"graph.json", "manifest.json", ".graphify_analysis.json"} <= managed
    before = {str(path.relative_to(output)): path.read_bytes()
              for path in output.rglob("*") if path.is_file()}
    inventory_before = {name: hashlib.sha256(data).hexdigest() for name, data in before.items()}
    # More new nodes than the faulty classifier can lose: shrink protection alone
    # cannot establish that the selected source was completely observed.
    growth = next(path for path in paths if path.suffix == ".py")
    with growth.open("a") as stream:
        for index in range(30):
            stream.write(f"\ndef growth_{index}():\n    return {index}\n")
    fault_dir = tmp_path / "fault"
    failed = _public_run(repo, driver, target, parallel, True, fault_dir, family)
    events = _events(fault_dir)
    faults = [event for event in events if event["event"] == "fault"]
    assert faults, "underlying selected classifier read must actually fail"
    assert not any(event["event"] == "provider_call" for event in [*events, *_events(tmp_path / "healthy")])
    driver_pid = next(event["pid"] for event in events if event["event"] == "driver")
    if parallel:
        initialized = {event["pid"] for event in events if event["event"] == "initializer"}
        assert len(initialized) == 2 and driver_pid not in initialized
        assert {event["pid"] for event in faults} <= initialized
        healthy_workers = {event["pid"] for event in events if event["event"] == "healthy_source"}
        assert healthy_workers - {event["pid"] for event in faults}, events
        if family == "svelte_supplement":
            setup = [event for event in events if event["event"] == "cold_config_setup"]
            assert len(setup) == 1 and setup[0]["entries"] == 0
            assert {event["pid"] for event in faults} == {setup[0]["pid"]}
            worker_events = [event["event"] for event in events if event["pid"] == setup[0]["pid"]]
            assert worker_events.index("cold_config_setup") < worker_events.index("fault")
            healthy_setup = [event for event in _events(tmp_path / "healthy") if event["event"] == "cold_config_setup"]
            assert len(healthy_setup) == 1 and healthy_setup[0]["entries"] == 0
        if family == "cpp":
            assert any(event["event"] == "classification" and event["pid"] == driver_pid
                       for event in events)
        if family in {"tsconfig", "workspace", "xaml", "pascal"}:
            assert {event["pid"] for event in events if event["event"] == "cache_warm"} == initialized | {driver_pid}
            for pid in initialized:
                pid_events = [event["event"] for event in events if event["pid"] == pid]
                assert pid_events.index("initializer") < pid_events.index("cache_warm")
                if "fault" in pid_events:
                    assert pid_events.index("cache_warm") < pid_events.index("fault")
    else:
        assert {event["pid"] for event in faults} == {driver_pid}
        assert not any(event["event"] == "initializer" for event in events)
    all_after = {str(path.relative_to(output)): path.read_bytes()
                 for path in output.rglob("*") if path.is_file()}
    after = {name: all_after.get(name) for name in before}
    current_graph = json.loads(graph_path.read_text())
    case = "F072" if family == "baseline_stat" else f"{_PUBLIC_FAMILIES.get(family, 'R005' if parallel else 'R001')}-{'P' if parallel else 'S'}"
    if family == "svelte_supplement":
        case = "R005" if parallel else "R001"
    contribution_present = any(node["label"] == expected_label for node in current_graph["nodes"])
    if family == "svelte_supplement":
        contribution_present = any(edge.get("source") == "page" and edge.get("target") == "child"
                                   and edge.get("relation") == "dynamic_import"
                                   and edge.get("confidence") == "EXTRACTED"
                                   for edge in current_graph["links"])
    observation = {"case": case,
                   "returncode": failed.returncode, "events": events,
                   "baseline_nodes": len(published["nodes"]),
                   "published_nodes": len(current_graph["nodes"]),
                   "expected_contribution_present": contribution_present,
                   "healthy_controls": {"real_receipt": True, "full_reference_parity": True,
                                        "retained_semantic_payloads": True},
                   "receipt_managed_paths": sorted(managed),
                   "full_output_inventory_before": inventory_before,
                   "full_output_inventory_after": {name: hashlib.sha256(data).hexdigest()
                                                   for name, data in all_after.items()},
                   "changed_output_paths": sorted(name for name in before.keys() | all_after.keys()
                                                  if before.get(name) != all_after.get(name)),
                   "semantic_source_sha256": hashlib.sha256(document.read_bytes()).hexdigest(),
                   "changed_publication_files": [name for name in before if before[name] != after[name]],
                   "provider_calls": 0}
    (tmp_path / "observation.json").write_text(json.dumps(observation, indent=2))
    if failed.returncode == 0 and family != "baseline_stat":
        assert observation["published_nodes"] > observation["baseline_nodes"], "growth control required"
        assert not observation["expected_contribution_present"], "partial contribution loss required"
    assert failed.returncode != 0, observation
    _assert_refused_publication(before, all_after, repo, growth, driver_pid)
    assert any(word in (failed.stdout + failed.stderr).lower() for word in ("failed", "incomplete", "oserror"))


@pytest.mark.parametrize("family", ["tsconfig", "workspace_partial", "apex"],
                         ids=["F033", "F043", "F013"])
def test_strict_consumed_operation_failure(tmp_path, monkeypatch, family):
    from graphify.extract import extract, _file_node_id
    import graphify.extractors.resolution as resolution

    if family == "apex":
        selected = _write(tmp_path / "Account.cls", "public class Account {\n}\n")
        paths = [selected]
    else:
        selected = _write(tmp_path / "tsconfig.json", json.dumps({"compilerOptions": {
            "baseUrl": ".", "paths": {"@types": ["packages/types/index.ts"]}}}))
        target = _write(tmp_path / "packages/types/index.ts", "export type Helper = string\n")
        importer = _write(tmp_path / "page.ts",
                          "import type { Helper } from '@types'\nconst value: Helper = 'x'\n")
        paths = [importer, target]
        if family == "workspace_partial":
            selected.unlink()
            selected = _write(tmp_path / "package.json", '{"workspaces": ["packages/*"]}')
            _write(tmp_path / "packages/types/package.json", '{"name": "@types", "exports": "./index.ts"}')
            _write(tmp_path / "packages/other/package.json", '{"name": "@other"}')
    resolution._TSCONFIG_ALIAS_CACHE.clear()
    healthy = extract(paths, cache_root=tmp_path, parallel=False, strict=True)
    if family == "apex":
        assert any(node["label"] == "Account" for node in healthy["nodes"])
    else:
        expected = (_file_node_id(Path("page.ts")), _file_node_id(Path("packages/types/index.ts")))
        assert any((edge["source"], edge["target"]) == expected
                   and edge["relation"] == "imports_from" for edge in healthy["edges"])
    resolution._TSCONFIG_ALIAS_CACHE.clear()
    observed = []
    if family == "workspace_partial":
        original = os.scandir

        class PartialScan:
            def __init__(self, path):
                self.scan = original(path)
                self.count = 0

            def __enter__(self):
                self.scan.__enter__()
                return self

            def __exit__(self, *args):
                return self.scan.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                if self.count:
                    observed.append({"operation": "scandir_next", "after_valid_entry": self.count})
                    raise OSError(5, "injected partial workspace enumeration")
                entry = next(self.scan)
                assert entry.is_dir()
                self.count += 1
                observed.append({"operation": "scandir_yield", "name": entry.name})
                return entry

        def scandir(path):
            return PartialScan(path) if Path(path) == tmp_path / "packages" else original(path)

        monkeypatch.setattr(os, "scandir", scandir)
    else:
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path == selected:
                observed.append({"operation": "read_text", "path": str(path)})
                raise OSError(5, "injected consumed source read")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    error = None
    result = None
    try:
        result = extract(paths, cache_root=tmp_path, parallel=False, strict=True)
    except (OSError, RuntimeError) as exc:
        error = str(exc)
    assert observed, "selected underlying operation must fire before claiming red"
    if family == "workspace_partial":
        assert any(row["operation"] == "scandir_next" for row in observed)
    observation = {"case": {"tsconfig": "F033", "workspace_partial": "F043", "apex": "F013"}[family],
                   "operations": observed, "error": error,
                   "healthy_nodes": len(healthy["nodes"]), "healthy_edges": len(healthy["edges"]),
                   "partial_nodes": len(result["nodes"]) if result is not None else None,
                   "partial_edges": len(result["edges"]) if result is not None else None}
    (tmp_path / "observation.json").write_text(json.dumps(observation, indent=2))
    assert error is not None, observation


@pytest.mark.parametrize("case,pattern", [
    ("G001", "a/pkg/x.py"), ("G002", "*"), ("G003", "?/pkg"),
    ("G004", "[ab]/*"), ("G005", ".*"), ("G006", "**/*.py"),
    ("G007", "**/x.py"), ("G008", "**"), ("G009", "*/"),
    ("G010", "link/pkg/*.py"), ("G011", "lin*/pkg/*.py"),
    ("G012", "**/x.py"), ("G013", "no-match*"),
    ("G014", "./a//pkg/./*.py"), ("G015", "a/../b/*.py"),
    ("G016", ""), ("G017", "/absolute/*.py"), ("G018", "**/**/x.py"),
    ("G019", "*.PY"), ("G020", "CASE.py"), ("G021", "*.py"),
    ("G023", "**/pkg/**/x*.py"), ("G024", "dangling"),
], ids=lambda value: value if value.startswith("G") else None)
def test_checked_glob_matches_native_sequence(tmp_path, monkeypatch, case, pattern):
    from graphify.extractors.base import checked_glob, checked_rglob

    root = tmp_path / "root"
    for name in ("x.py", "CASE.py", ".hidden.py", "a/pkg/x.py", "a/pkg/deep/x2.py",
                 "a/pkg/nested/pkg/x.py", "b/x.py", "b/pkg/x.py", "irrelevant/deep/data.txt"):
        _write(root / name, "")
    (root / "link").symlink_to(root / "a", target_is_directory=True)
    (root / "dangling").symlink_to(root / "missing")
    native = root.rglob if case == "G021" else root.glob
    checked = checked_rglob if case == "G021" else checked_glob
    if case in {"G016", "G017"}:
        with pytest.raises((ValueError, NotImplementedError)) as native_error:
            list(native(pattern))
        with pytest.raises(type(native_error.value)):
            list(checked(root, pattern, strict=True))
        return
    expected = list(native(pattern))
    if case == "G001":
        original = os.scandir
        def scandir(path):
            assert Path(path) != root / "irrelevant", "literal selector visited unrelated directory"
            return original(path)
        monkeypatch.setattr(os, "scandir", scandir)
    assert list(checked(root, pattern, strict=True)) == expected


@pytest.mark.parametrize("case,marked,strict", [
    ("K001", True, False), ("K002", False, False),
    ("K003", True, True), ("K004", False, True),
])
def test_strict_callback_keeps_seven_positional_contract(case, marked, strict):
    from graphify.extractors.base import call_with_strict, strict_aware

    seen = []
    def callback(*args, **kwargs):
        seen.append((args, kwargs))
        return "result"
    function = strict_aware(callback) if marked else callback
    args = tuple(range(7))
    assert call_with_strict(function, *args, strict=strict) == "result"
    assert seen == [(args, {"strict": True} if marked and strict else {})]


def test_strict_callback_typeerror_not_retried_K005():
    from graphify.extractors.base import call_with_strict, strict_aware

    changes = []
    @strict_aware
    def callback(*args, strict=False):
        changes.append(args)
        raise TypeError("failure after mutation")
    with pytest.raises(TypeError, match="failure after mutation"):
        call_with_strict(callback, *range(7), strict=True)
    assert len(changes) == 1


def test_strict_marker_preserves_function_identity_K006():
    from graphify.extractors.base import strict_aware

    def extractor(path):
        return {"nodes": [], "edges": []}
    assert strict_aware(extractor) is extractor


def _inside(function):
    frame = sys._getframe(1)
    while frame:
        if frame.f_code.co_name == function:
            return True
        frame = frame.f_back
    return False


def _inject_operation(monkeypatch, target, operation, helper, observed):
    """Fail one actual consumed operation, leaving admission and other I/O intact."""
    owner = os if operation in {"stat", "scandir"} else Path
    original = getattr(owner, operation)

    def fail(path, *args, **kwargs):
        selected = isinstance(path, (str, os.PathLike)) and Path(path) == target
        if selected and (not helper or _inside(helper)):
            observed.append({"operation": operation, "helper": helper, "path": str(path)})
            raise OSError(5, f"injected {helper} {operation}")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(owner, operation, fail)


@pytest.mark.parametrize("case,suffix,text,helper,operation", [
    ("F001", ".h", "@interface Account\n@end\n", "_is_objc_header", "read_bytes"),
    ("F002", ".h", "class Account { public: int value(); };\n", "_is_cpp_header", "read_bytes"),
    ("F003", ".m", "@implementation Account\n@end\n", "_is_objc_header", "read_bytes"),
    ("F004", ".groovy", 'class Account {\n def "feature evidence"() { expect: true }\n}\n', "_is_spock_file", "read_text"),
    ("F005", ".groovy", 'class Account {\n def "feature evidence"() { expect: true }\n}\n', "_extract_generic", "read_bytes"),
    ("F006", "", "#!/usr/bin/env python3\ndef value(): return 1\n", "_shebang_interpreter", "open"),
    ("F007", "", "#!/usr/bin/env python3\ndef value(): return 1\n", "_shebang_interpreter", "open"),
    ("F008", ".py", "def value(): return 1\n", "_resolves_under_root", "resolve"),
], ids=['F001', 'F002', 'F003', 'F004', 'F005', 'F006', 'F007', 'F008'])
def test_strict_admission_and_classifier_operations(tmp_path, monkeypatch, case, suffix, text, helper, operation):
    from graphify.detect import detect
    from graphify.extract import extract

    source = _write(tmp_path / ("account" + suffix), text)
    if case in {"F006", "F008"}:
        action = lambda: detect(tmp_path, strict=True)
        assert str(source) in action()["files"]["code"]
    else:
        action = lambda: extract([source], cache_root=tmp_path, parallel=False, strict=True)
        healthy = action()
        assert any(node.get("source_file") == source.name for node in healthy["nodes"])
        if case in {"F004", "F005"}:
            assert any(node["label"] == '"feature evidence"' for node in healthy["nodes"])
    observed = []
    _inject_operation(monkeypatch, source, operation, helper, observed)
    with pytest.raises((OSError, RuntimeError)):
        action()
    assert observed


@pytest.mark.parametrize("case,operation,helper,source_name,source_text,target_name,target_text", [
    ("F018", "resolve", "extract_dart", "piece.dart", "part of 'main.dart';\nclass Piece {}\n", "main.dart", "library app;\n"),
    ("F084", "stat", "extract_dart", "piece.dart", "part of 'main.dart';\nclass Piece {}\n", "main.dart", "library app;\n"),
    ("F019", "resolve", "extract_sln", "app.sln", 'Project("guid") = "App", "App.csproj", "{abc}"\nEndProject\n', "App.csproj", "<Project/>"),
    ("F020", "resolve", "extract_slnx", "app.slnx", '<Solution><Project Path="App.csproj" /></Solution>', "App.csproj", "<Project/>"),
    ("F021", "resolve", "extract_csproj", "app.csproj", '<Project><ItemGroup><ProjectReference Include="Peer.csproj" /></ItemGroup></Project>', "Peer.csproj", "<Project/>"),
    ("F029", "stat", "extract_bash", "app.sh", "#!/bin/bash\nsource ./peer.sh\n", "peer.sh", "echo peer\n"),
    ("F030", "stat", "extract_bash", "app.sh", "#!/bin/bash\nbash ./peer.sh\n", "peer.sh", "echo peer\n"),
    ("F031", "stat", "extract_dm", "app.dm", '#include "peer.dm"\n/obj/item\n', "peer.dm", "/obj/peer\n"),
    ("F032", "stat", "extract_dm", "app.dm", '#include "icon.dmi"\n/obj/item\n', "icon.dmi", "resource include fixture\n"),
    ("F048", "stat", "_import_js", "app.js", "import {value} from './peer.js';\nvalue();\n", "peer.js", "export function value() {}\n"),
    ("F049", "stat", "_resolve_js_import_path", "app.js", "import {value} from './lib';\nvalue();\n", "lib/index.js", "export function value() {}\n"),
    ("F050", "stat", "_dynamic_import_js", "app.js", "async function load() { return import('./peer.js'); }\n", "peer.js", "export const value=1;\n"),
    ("F051", "stat", "_require_imports_js", "app.js", "const peer = require('./peer.js');\n", "peer.js", "exports.value=1;\n"),
    ("F052", "stat", "_resolve_python_module_path", "app.py", "from peer import Value\n", "peer.py", "class Value: pass\n"),
    ("F053", "stat", "_import_c", "app.c", '#include "peer.h"\nint value(void) { return 1; }\n', "peer.h", "int value(void);\n"),
    ("F054", "stat", "extract_objc", "app.m", '#import "peer.h"\n@implementation App\n@end\n', "peer.h", "@interface Peer\n@end\n"),
    ("F055", "stat", "_import_lua", "app.lua", 'local peer = require("peer")\n', "peer.lua", "return {}\n"),
], ids=['F018', 'F084', 'F019', 'F020', 'F021', 'F029', 'F030', 'F031', 'F032', 'F048', 'F049', 'F050', 'F051', 'F052', 'F053', 'F054', 'F055'])
def test_strict_leaf_and_resolver_target_operations(tmp_path, monkeypatch, case, operation, helper,
                                                   source_name, source_text, target_name, target_text):
    from graphify.extract import extract

    source = _write(tmp_path / source_name, source_text)
    target = _write(tmp_path / target_name, target_text)
    paths = [source, target] if case == "F052" else [source]
    action = lambda: extract(paths, cache_root=tmp_path, parallel=False, strict=True)
    healthy = action()
    assert healthy["nodes"] and healthy["edges"], case
    if case in {"F029", "F031", "F032", "F048", "F049", "F050", "F051", "F053", "F054", "F055"}:
        assert any(edge["relation"] in {"imports", "imports_from"} for edge in healthy["edges"]), case
    observed = []
    if case == "F049":
        target = target.parent
    _inject_operation(monkeypatch, target, operation, helper, observed)
    with pytest.raises((OSError, RuntimeError)):
        action()
    assert observed, case


@pytest.mark.parametrize("case,helper,target_kind,operation", [
    ("F034", "_read_tsconfig_aliases", "tsconfig", "parse"),
    ("F035", "_read_tsconfig_aliases", "extends", "read_text"),
    ("F036", "_read_tsconfig_aliases", "extends", "parse"),
    ("F037", "_load_tsconfig_aliases", "tsconfig", "stat"),
    ("F038", "_find_workspace_root", "workspace", "read_text"),
    ("F039", "_find_workspace_root", "workspace", "parse"),
    ("F040", "_workspace_globs", "workspace", "read_text"),
    ("F041", "_workspace_globs", "workspace", "parse"),
    ("F042", "_pnpm_workspace_globs", "pnpm", "read_text"),
    ("F044", "_load_workspace_packages", "package", "read_text"),
    ("F045", "_load_workspace_packages", "package", "parse"),
    ("F046", "_package_entry_candidates", "package", "read_text"),
    ("F047", "_package_entry_candidates", "package", "parse"),
], ids=['F034', 'F035', 'F036', 'F037', 'F038', 'F039', 'F040', 'F041', 'F042', 'F044', 'F045', 'F046', 'F047'])
def test_strict_consumed_config_operations(tmp_path, monkeypatch, case, helper, target_kind, operation):
    import graphify.extractors.resolution as resolution
    from graphify.extract import extract

    target = _write(tmp_path / "packages/types/index.ts", "export type Helper = string\n")
    importer = _write(tmp_path / "page.ts", "import type { Helper } from '@types'\nconst value: Helper = 'x'\n")
    if target_kind in {"tsconfig", "extends"}:
        selected = _write(tmp_path / "tsconfig.json", json.dumps({"compilerOptions": {
            "baseUrl": ".", "paths": {"@types": ["packages/types/index.ts"]}}}))
        if target_kind == "extends":
            selected.rename(tmp_path / "base.json")
            selected = tmp_path / "base.json"
            _write(tmp_path / "tsconfig.json", '{"extends": "./base.json"}')
    else:
        workspace = _write(tmp_path / "package.json", '{"workspaces": ["packages/*"]}')
        package = _write(tmp_path / "packages/types/package.json", '{"name": "@types", "exports": "./index.ts"}')
        selected = package if target_kind == "package" else workspace
        if target_kind == "pnpm":
            selected = _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    resolution._TSCONFIG_ALIAS_CACHE.clear()
    action = lambda: extract([importer, target], cache_root=tmp_path, parallel=False, strict=True)
    healthy = action()
    assert any(edge["relation"] == "imports_from" for edge in healthy["edges"]), case
    observed = []
    if operation == "parse":
        original = json.loads
        selected_bytes = selected.read_text()
        def parse(raw, *args, **kwargs):
            if raw == selected_bytes and _inside(helper):
                observed.append({"operation": "json.loads", "helper": helper})
                return original("{injected invalid config")
            return original(raw, *args, **kwargs)
        monkeypatch.setattr(json, "loads", parse)
    else:
        _inject_operation(monkeypatch, selected, operation, helper, observed)
    with pytest.raises((OSError, RuntimeError, ValueError)):
        action()
    assert observed, case


@pytest.mark.parametrize("case", ["F009", "F010", "F073", "F074", "F075", "F076", "F077", "F078", "F079", "F080", "F081", "F082"])
def test_strict_policy_and_admission_metadata(tmp_path, monkeypatch, case):
    import graphify.detect as detection

    root = tmp_path / "corpus"
    root.mkdir()
    _write(root / "app.py", "def value(): return 1\n")
    linked = case in {"F009", "F010", "F075", "F076"}
    gitdir = tmp_path / "metadata" if linked else root / ".git"
    exclude = _write(gitdir / "info/exclude", "ignored.py\n")
    if linked:
        _write(root / ".git", f"gitdir: {gitdir}\n")
        _write(gitdir / "commondir", ".\n")
    action = lambda: detection._git_info_exclude(root, strict=True)
    target, operation, helper = root / ".git", "stat", "checked_is_dir"
    if case in {"F009", "F010"}:
        target = root / ".git" if case == "F009" else gitdir / "commondir"
        operation, helper = "read_text", "_git_info_exclude"
    elif case == "F073":
        action = lambda: detection._find_vcs_root(root, strict=True)
        helper = "_find_vcs_root"
    elif case == "F075":
        helper = "checked_is_file"
    elif case == "F076":
        target, helper = gitdir / "commondir", "_git_info_exclude"
    elif case == "F077":
        target, helper = exclude, "_git_info_exclude"
    elif case in {"F078", "F079", "F080"}:
        name = {"F078": ".gitignore", "F079": ".graphifyignore", "F080": ".graphifyinclude"}[case]
        target = _write(root / name, "ignored.py\n")
        helper = "_load_graphifyinclude" if case == "F080" else "_load_dir_own_ignore"
        action = lambda: getattr(detection, helper)(root, strict=True)
    elif case == "F081":
        _write(root / "snapshots/a.snap", "snapshot")
        target, operation, helper = root / "snapshots", "scandir", "_is_noise_dir"
        action = lambda: detection._is_noise_dir("snapshots", root, strict=True)
    elif case == "F082":
        _write(root / "graphify-out/memory/recall.md", "retained recall")
        target, helper = root / "graphify-out/memory", "detect"
        action = lambda: detection.detect(root, strict=True)
    assert action(), case
    observed = []
    _inject_operation(monkeypatch, target, operation, helper, observed)
    with pytest.raises(OSError):
        action()
    assert observed, case


@pytest.mark.parametrize("case", ["F011", "F012", "F083"])
def test_strict_incremental_identity_observations(tmp_path, monkeypatch, case):
    from graphify.cli import _stale_graph_sources
    from graphify.detect import detect_incremental, save_manifest

    source = _write(tmp_path / "app.py", "def value(): return 1\n")
    old = _write(tmp_path / "old.py", "def old(): return 2\n")
    _write(tmp_path / ".graphifyignore", "old.py\n")
    output = tmp_path / "graphify-out"
    output.mkdir()
    manifest = output / "manifest.json"
    save_manifest({"code": [str(source), str(old)]}, str(manifest), root=tmp_path)
    graph = _write(output / "graph.json", json.dumps({"nodes": [
        {"id": "old", "source_file": "old.py", "label": "old"}], "links": []}))
    if case == "F012":
        action = lambda: _stale_graph_sources(graph, tmp_path, {str(source)}, strict=True)
        assert action() == ["old.py"]
    else:
        action = lambda: detect_incremental(tmp_path, str(manifest), strict=True)
        assert str(old) in action()["excluded_files"]
    observed = []
    if case == "F012":
        _inject_operation(monkeypatch, old, "resolve", "_in_seen", observed)
    elif case == "F011":
        _inject_operation(monkeypatch, old, "stat", "checked_exists", observed)
    else:
        original = os.stat
        def stat(path, *args, **kwargs):
            if (isinstance(path, (str, os.PathLike)) and Path(path) == source
                    and _inside("detect_incremental") and not _inside("detect")):
                observed.append(path)
                raise OSError(5, "unneeded strict mtime observation")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(os, "stat", stat)
        assert str(source) in action()["new_files"]["code"]
        assert not observed, "strict complete refresh must bypass the unneeded mtime comparison"
        return
    with pytest.raises(OSError):
        action()
    assert observed


@pytest.mark.parametrize("case", ["F014", "F015", "F016", "F017", "F062"])
def test_strict_selected_backend_failure(tmp_path, monkeypatch, case):
    from graphify.extract import extract

    if case in {"F014", "F015"}:
        import tree_sitter_pascal  # Required CI extra: absence is a setup failure, not a skip.
        assert callable(tree_sitter_pascal.language)
        source = _write(tmp_path / "app.pas", "program App;\nprocedure Run;\nbegin end;\nbegin Run; end.\n")
    elif case in {"F016", "F017"}:
        import shutil
        assert shutil.which("cpp"), "selected preprocessor must exist for attempted-backend failure cases"
        source = _write(tmp_path / "app.f90", "program app\nprint *, 'hello'\nend program app\n")
    else:
        source = _write(tmp_path / "app.php", "<?php class App {}\n")
    action = lambda: extract([source], cache_root=tmp_path, parallel=False, strict=True)
    assert action()["nodes"]
    if case in {"F016", "F017"}:
        # Prove healthy Fortran parsing before selecting preprocessing. The host
        # cpp need not support Fortran; the cases test its attempted failure.
        source = source.rename(source.with_suffix(".F90"))
    observed = []
    if case == "F014":
        import tree_sitter
        original = tree_sitter.Parser
        class FailingParser:
            def __init__(self, *args, **kwargs):
                self.parser = original(*args, **kwargs)
            def parse(self, data):
                observed.append("Parser.parse")
                raise OSError(5, "injected Pascal parser operation")
        monkeypatch.setattr(tree_sitter, "Parser", FailingParser)
    elif case == "F015":
        _inject_operation(monkeypatch, source, "read_bytes", "extract_pascal", observed)
    elif case in {"F016", "F017"}:
        original = subprocess.run
        def run(args, *a, **kw):
            if args[0] == "cpp":
                observed.append("cpp process")
                if case == "F016":
                    raise OSError(5, "injected cpp process failure")
                return subprocess.CompletedProcess(args, 1, b"", b"injected cpp nonzero")
            return original(args, *a, **kw)
        monkeypatch.setattr(subprocess, "run", run)
    else:
        import tree_sitter_php
        monkeypatch.setattr(tree_sitter_php, "language", tree_sitter_php.language_php, raising=False)
        monkeypatch.setattr(tree_sitter_php, "language_php", None)
        observed.append("required language_php entrypoint absent; legacy fallback available")
    with pytest.raises((OSError, RuntimeError)):
        action()
    assert observed


def _xaml_fixture(root):
    companion = _write(root / "View.xaml.cs", "public class View {\n public void Save(object sender, EventArgs e) {}\n}\n")
    vm = _write(root / "ViewModel.cs", "public class ViewModel {\n [ObservableProperty]\n private string name;\n}\n")
    _write(root / "App.csproj", "<Project/>")
    view = _write(root / "View.xaml", '<Window xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" xmlns:local="clr-namespace:Demo" x:Class="View"><Window.DataContext><local:ViewModel /></Window.DataContext><Button Click="Save" Content="{Binding Name}" /></Window>')
    return view, companion, vm


def _inject_partial_scan(monkeypatch, target, helper, observed):
    original = os.scandir
    class PartialScan:
        def __init__(self, path):
            self.iterator = original(path)
            self.yielded = False
        def __enter__(self):
            self.iterator.__enter__()
            return self
        def __exit__(self, *args):
            return self.iterator.__exit__(*args)
        def __iter__(self):
            return self
        def __next__(self):
            if self.yielded:
                observed.append("failure after valid entry")
                raise OSError(5, "injected partial selected enumeration")
            entry = next(self.iterator)
            observed.append({"yielded": entry.name})
            self.yielded = True
            return entry
    def scan(path):
        if Path(path) == target and _inside(helper):
            return PartialScan(path)
        return original(path)
    monkeypatch.setattr(os, "scandir", scan)


@pytest.mark.parametrize("case", ["F022", "F023", "F024", "F025", "F026", "F027", "F028", "F071"])
def test_strict_xaml_selected_dependencies(tmp_path, monkeypatch, case):
    import graphify.extract as extraction

    view, companion, vm = _xaml_fixture(tmp_path)
    if case == "F022":
        companion = companion.rename(tmp_path / "View.XAML.CS")
        original_stat = os.stat
        def case_sensitive_stat(path, *args, **kwargs):
            if str(path) == str(tmp_path / "View.xaml.cs") and _inside("_xaml_codebehind_path"):
                raise FileNotFoundError(2, "exercise case-sensitive companion discovery")
            return original_stat(path, *args, **kwargs)
        monkeypatch.setattr(os, "stat", case_sensitive_stat)
    action = lambda: extraction.extract([view], cache_root=tmp_path, parallel=False, strict=True)
    healthy = action()
    assert any(edge.get("context") == "event" for edge in healthy["edges"])
    assert any(edge.get("context") == "view_model" for edge in healthy["edges"])
    if case == "F071":
        monkeypatch.setattr(extraction, "_XAML_ACTIVE_EXTRACT_ROOT", tmp_path)
        assert extraction._xaml_csharp_class_nodes(view)
        assert extraction._XAML_CSHARP_CLASS_CACHE
        action = lambda: extraction._xaml_csharp_class_nodes(view, strict=True)
    observed = []
    if case in {"F022", "F025", "F026"}:
        helper = {"F022": "_xaml_codebehind_path", "F025": "_xaml_project_root", "F026": "_xaml_csharp_class_nodes"}[case]
        _inject_partial_scan(monkeypatch, tmp_path, helper, observed)
    else:
        target = vm if case in {"F027", "F028"} else companion
        operation = "read_text" if case in {"F024", "F028"} else "read_bytes"
        helper = {"F024": "_xaml_codebehind_symbols", "F028": "_xaml_communitytoolkit_members"}.get(case, "_extract_generic")
        _inject_operation(monkeypatch, target, operation, helper, observed)
    with pytest.raises((OSError, RuntimeError)):
        action()
    assert observed, case
    if case in {"F022", "F025", "F026"}:
        assert "failure after valid entry" in observed


@pytest.mark.parametrize("case", ["F056", "F057", "F058", "F068", "F069"])
def test_strict_pascal_partial_enumeration_and_cache(tmp_path, monkeypatch, case):
    import graphify.extractors.resolution as resolution

    source = _write(tmp_path / "Main.pas", "unit Main;\ninterface\nimplementation\nend.")
    _write(tmp_path / "Other.pas", "unit Other;\ninterface\nimplementation\nend.")
    _write(tmp_path / "sub/Peer.pas", "unit Peer;\ninterface\ntype TPeer=class end;\nimplementation\nend.")
    if case == "F056":
        helper, args, target = resolution._pascal_project_root, (source,), tmp_path
    elif case in {"F057", "F068"}:
        helper, args, target = resolution._pascal_resolve_unit, (source, "Peer"), tmp_path / "sub"
    else:
        helper, args, target = resolution._pascal_resolve_class, (source, "TPeer"), tmp_path / "sub"
    assert helper(*args, strict=True)
    if case in {"F068", "F069"}:
        assert helper(*args), "ordinary resolver cache must be warmed before strict fault"
    observed = []
    _inject_partial_scan(monkeypatch, target, helper.__name__, observed)
    with pytest.raises(OSError):
        helper(*args, strict=True)
    assert "failure after valid entry" in observed


@pytest.mark.parametrize("case", ["F059", "F060", "F061"])
def test_strict_source_identity_failure(tmp_path, monkeypatch, case):
    import graphify.extractors.resolution as resolution
    from graphify.extract import extract

    source = _write(tmp_path / "app.py", "def value(): return 1\n")
    if case == "F059":
        action = lambda: resolution._source_key(str(source), tmp_path, strict=True)
    elif case == "F060":
        action = lambda: resolution._js_source_path(str(source), tmp_path, strict=True)
    else:
        action = lambda: extract([source], cache_root=tmp_path, parallel=False, strict=True)
    assert action()
    observed = []
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        selected = case != "F061"
        frame = sys._getframe(1)
        while frame and not selected:
            selected = frame.f_code.co_name == "extract" and "sym_remap" in frame.f_locals
            frame = frame.f_back
        if path == source and selected:
            observed.append(path)
            raise OSError(5, "injected selected identity resolution")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(OSError):
        action()
    assert observed


@pytest.mark.parametrize("case,field,value", [("F063", "nodes", None), ("F064", "edges", {}), ("F065", "nodes", {})], ids=["F063", "F064", "F065"])
def test_strict_result_collection_admission(tmp_path, monkeypatch, case, field, value):
    import graphify.extract as extraction

    source = _write(tmp_path / "input.fake", "fixture")
    result = {"nodes": [], "edges": []}
    monkeypatch.setitem(extraction._DISPATCH, ".fake", lambda path: result)
    assert extraction.extract([source], cache_root=tmp_path, parallel=False, strict=True)["nodes"] == []
    if value is None:
        result.pop(field)
    else:
        result[field] = value
    with pytest.raises(RuntimeError, match="incomplete"):
        extraction.extract([source], cache_root=tmp_path, parallel=False, strict=True)


@pytest.mark.parametrize("case", ["F066", "F067", "F070"])
def test_strict_config_cache_cannot_mask_failure(tmp_path, monkeypatch, case):
    import graphify.extractors.resolution as resolution

    _write(tmp_path / "pkg/index.ts", "export class Value {}\n")
    if case == "F070":
        _write(tmp_path / "package.json", '{"workspaces": ["pkg"]}')
        selected = _write(tmp_path / "pkg/package.json", '{"name": "@pkg", "exports": "index.ts"}')
        helper = resolution._load_workspace_packages
    else:
        selected = _write(tmp_path / "tsconfig.json", '{"compilerOptions": {"paths": {"@pkg": ["pkg/index.ts"]}}}')
        helper = resolution._load_tsconfig_aliases
    assert helper(tmp_path, strict=True)
    observed = []
    if case in {"F067", "F070"}:
        assert helper(tmp_path, strict=case == "F067")
    _inject_operation(monkeypatch, selected, "read_text", None, observed)
    if case == "F066":
        assert helper(tmp_path) == {}, "default failed cache must be populated first"
        assert str(selected) in resolution._TSCONFIG_ALIAS_CACHE
        observed.clear()
    with pytest.raises(OSError):
        helper(tmp_path, strict=True)
    assert observed


@pytest.mark.parametrize("case,suffix,source", [
    ("C003", ".fake", "valid empty custom extractor\n"),
    ("C005", ".py", "def intact(): return 1\ndef broken(\n"),
    ("C006", ".tsx", "export const View = () => <div>Hello</div>;\n"),
    ("C007", ".svelte", "<script>export let name = 'world';</script><h1>{name}</h1>"),
    ("C008", ".php", "<?php namespace A { class Item {} } namespace B { class Item {} }"),
    ("C009", ".py", "from unavailable_external import item\ndef value(): return item()\n"),
    ("C010", ".py", "from .unavailable_local import item\ndef value(): return item()\n"),
    ("C022", ".xaml", '<Window xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" x:Class="Absent"/>'),
], ids=lambda value: value if value.startswith("C0") else None)
def test_strict_tolerant_sources(tmp_path, monkeypatch, case, suffix, source):
    from graphify.extract import extract, _DISPATCH

    if case == "C003":
        monkeypatch.setitem(_DISPATCH, ".fake", lambda path: {"nodes": [], "edges": []})

    path = _write(tmp_path / f"source{suffix}", source)
    result = extract([path], cache_root=tmp_path, parallel=False, strict=True)
    assert isinstance(result["nodes"], list) and isinstance(result["edges"], list)
    assert not result.get("error")
    if case == "C003":
        assert result["nodes"] == result["edges"] == []
    if case == "C005":
        assert any(node["label"].startswith("intact(") for node in result["nodes"])


def test_default_classifier_fallback_C001(tmp_path, monkeypatch):
    from graphify.extract import _is_cpp_header

    path = _write(tmp_path / "header.h", "class Item {};\n")
    assert _is_cpp_header(path)
    observed = []
    _inject_operation(monkeypatch, path, "read_bytes", "_is_cpp_header", observed)
    assert _is_cpp_header(path) is False
    assert observed


def test_default_config_fallback_C002(tmp_path, monkeypatch):
    from graphify.extractors.resolution import _load_tsconfig_aliases

    path = _write(tmp_path / "tsconfig.json", '{"compilerOptions":{"paths":{"@x":["x.ts"]}}}')
    assert _load_tsconfig_aliases(tmp_path, strict=True)
    observed = []
    _inject_operation(monkeypatch, path, "read_text", "_read_tsconfig_aliases", observed)
    assert _load_tsconfig_aliases(tmp_path) == {}
    assert observed


def test_unsupported_classifier_C004(tmp_path):
    from graphify.extract import _get_extractor

    path = _write(tmp_path / "unhandled.unsupported", "opaque data")
    assert _get_extractor(path, strict=True) is None


@pytest.mark.parametrize("case,errno", [("C011", 2), ("C012", 20), ("C013", 13), ("C014", 5), ("C027", 2)])
def test_optional_metadata_absence(tmp_path, monkeypatch, case, errno):
    from graphify.extractors.base import checked_exists, checked_is_file

    target = tmp_path / ("tsconfig.json" if case == "C011" else "missing.ts")
    original = os.stat
    observed = []
    def stat(path, *args, **kwargs):
        if Path(path) == target:
            observed.append(errno)
            raise OSError(errno, "selected metadata outcome")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", stat)
    helper = checked_exists if case == "C011" else checked_is_file
    if errno in {2, 20}:
        assert helper(target, strict=True) is False
    else:
        with pytest.raises(OSError) as failure:
            helper(target, strict=True)
        assert failure.value.errno == errno
    assert observed == [errno]


@pytest.mark.parametrize("case", ["C015", "C016", "C017", "C025"])
def test_strict_tsconfig_healthy_controls(tmp_path, case):
    from graphify.extractors.resolution import _load_tsconfig_aliases

    path = _write(tmp_path / "tsconfig.json", '{"compilerOptions":{"paths":{"@x":["one.ts"]}}}')
    if case == "C015":
        path.write_text('{// comment\n"compilerOptions":{"paths":{"@x":["one.ts",],},},}')
    elif case == "C016":
        path.write_text('{"extends":"./base.json","compilerOptions":{"paths":{"@x":["one.ts"]}}}')
        _write(tmp_path / "base.json", '{"extends":"./tsconfig.json"}')
    elif case == "C017":
        path.write_text('{"extends":"external-package/config","compilerOptions":{"paths":{"@x":["one.ts"]}}}')
    aliases = _load_tsconfig_aliases(tmp_path, strict=True)
    assert aliases == {"@x": [str(tmp_path / "one.ts")]}
    if case == "C025":
        path.write_text('{"compilerOptions":{"paths":{"@x":["two.ts"]}}}')
        assert _load_tsconfig_aliases(tmp_path, strict=True) == {"@x": [str(tmp_path / "two.ts")]}


@pytest.mark.parametrize("case", ["C018", "C019"])
def test_strict_workspace_absence_controls(tmp_path, case):
    from graphify.extractors.resolution import _load_workspace_packages

    _write(tmp_path / "package.json", '{"workspaces":["packages/*"]}')
    if case == "C019":
        _write(tmp_path / "packages/unnamed/package.json", '{}')
    assert _load_workspace_packages(tmp_path, strict=True) == {}


def test_pascal_missing_grammar_fallback_C020(tmp_path, monkeypatch):
    from graphify.extract import extract

    path = _write(tmp_path / "Main.pas", "unit Main;\ninterface\nimplementation\nend.")
    monkeypatch.setitem(sys.modules, "tree_sitter_pascal", None)
    result = extract([path], cache_root=tmp_path, parallel=False, strict=True)
    assert any(node["label"] == "Main" for node in result["nodes"])


def test_fortran_missing_cpp_fallback_C021(tmp_path, monkeypatch):
    import shutil
    from graphify.extract import extract

    path = _write(tmp_path / "Main.F90", "program Main\nprint *, 'hello'\nend program Main\n")
    original = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "cpp" else original(name))
    result = extract([path], cache_root=tmp_path, parallel=False, strict=True)
    assert any(node["label"].casefold() == "main" for node in result["nodes"])


def test_xaml_root_boundary_C023(tmp_path, monkeypatch):
    import graphify.extract as extraction

    _write(tmp_path / "Outer.csproj", "<Project/>")
    root = tmp_path / "selected"
    view = _write(root / "nested/View.xaml", "<Window/>")
    monkeypatch.setattr(extraction, "_XAML_ACTIVE_EXTRACT_ROOT", root)
    assert extraction._xaml_project_root(view, strict=True) == root


def test_sln_virtual_folder_C024(tmp_path, monkeypatch):
    from graphify.extract import extract

    path = _write(tmp_path / "App.sln", 'Microsoft Visual Studio Solution File, Format Version 12.00\nProject("{66A26720-8FB5-11D2-AA7E-00C04F688DDE}") = "Solution Items", "Solution Items", "{00000000-0000-0000-0000-000000000000}"\nEndProject\n')
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        assert not (path.name == "Solution Items" and _inside("extract_sln")), "virtual folder resolved as a filesystem project"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    result = extract([path], cache_root=tmp_path, parallel=False, strict=True)
    assert not result.get("error")


def test_pascal_strict_repeat_C026(tmp_path):
    from graphify.extractors.resolution import _pascal_resolve_unit
    from graphify.extractors.base import _make_id

    source = _write(tmp_path / "Main.pas", "unit Main; interface implementation end.")
    first = _write(tmp_path / "one/Peer.pas", "unit Peer; interface implementation end.")
    assert _pascal_resolve_unit(source, "Peer", strict=True) == _make_id(str(first))
    first.unlink()
    second = _write(tmp_path / "two/Peer.pas", "unit Peer; interface implementation end.")
    assert _pascal_resolve_unit(source, "Peer", strict=True) == _make_id(str(second))


def test_workspace_pattern_order_G022(tmp_path, monkeypatch):
    from graphify.extractors.resolution import _load_workspace_packages

    patterns = ["packages/*", "packages/a", "packages/b", "packages/a"]
    _write(tmp_path / "package.json", json.dumps({"workspaces": patterns}))
    for name in ("a", "b"):
        _write(tmp_path / f"packages/{name}/package.json", '{"name":"shared"}')
    expected = [directory / "package.json" for pattern in patterns for directory in tmp_path.glob(pattern)]
    original = Path.read_text
    seen = []
    def read(path, *args, **kwargs):
        if path in expected and _inside("_load_workspace_packages"):
            seen.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    assert _load_workspace_packages(tmp_path, strict=True) == {"shared": expected[-1].parent}
    assert seen == expected


def _supplement_fixture(root, language):
    if language == "svelte":
        source = _write(root / "Page.svelte", '<script>let ready = true;</script>\n{#await import("$lib/Child.svelte") then component}<svelte:component this={component.default}/>{/await}\n')
        child = _write(root / "Child.svelte", '<script>export let name = "child";</script><h1>{name}</h1>\n')
        config = _write(root / "tsconfig.json", '{"compilerOptions":{"paths":{"$lib/*":["*"]}}}')
    else:
        text = '<template><div>{{ import("@lib/dep") }}</div></template>\n' if language == "vue" else '<div>{import("@lib/dep")}</div>\n'
        source = _write(root / ("View." + language), text)
        child = _write(root / "lib/dep.ts", "export function dep() { return 1; }\n")
        config = _write(root / "tsconfig.json", '{"compilerOptions":{"baseUrl":".","paths":{"@lib/*":["lib/*"]}}}')
    return source, child, config


def _assert_supplement_edge(result, source_name, child_name):
    source = next(node["id"] for node in result["nodes"]
                  if node.get("source_file") == source_name and node.get("label") == Path(source_name).name)
    target = next(node["id"] for node in result["nodes"]
                  if node.get("source_file") == child_name and node.get("label") == Path(child_name).name)
    matches = [edge for edge in result["edges"] if edge.get("source") == source
               and edge.get("target") == target and edge.get("relation") == "dynamic_import"]
    assert matches, result
    assert all(edge["confidence"] == "EXTRACTED" and edge["source_file"] == source_name for edge in matches)
    return matches


@pytest.mark.parametrize("case,language", [("R002", "vue"), ("R003", "astro")], ids=["R002", "R003"])
def test_strict_component_supplement_config_failure(tmp_path, monkeypatch, case, language):
    import graphify.extract as extraction
    import graphify.extractors.resolution as resolution

    source, child, config = _supplement_fixture(tmp_path, language)
    action = lambda: extraction.extract([source, child], cache_root=tmp_path, parallel=False, strict=True)
    resolution._TSCONFIG_ALIAS_CACHE.clear()
    healthy = action()
    expected = _assert_supplement_edge(healthy, source.name, "lib/dep.ts")
    resolution._TSCONFIG_ALIAS_CACHE.clear()
    original = Path.read_text
    observed = []
    def read(path, *args, **kwargs):
        if path == config and _inside("extract_" + language) and _inside("_read_tsconfig_aliases") and not _inside("_extract_generic"):
            observed.append({"path": str(path), "operation": "Path.read_text", "helper": "extract_" + language})
            raise OSError(5, "selected component supplement config read")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    error, result = None, None
    try:
        result = action()
    except (OSError, RuntimeError) as exc:
        error = str(exc)
    observation = {"case": case, "healthy_edges": expected, "operations": observed,
                   "error": error, "fault_edges": result["edges"] if result else None,
                   "failed_alias_cache": dict(resolution._TSCONFIG_ALIAS_CACHE)}
    (tmp_path / "batch1-observation.json").write_text(json.dumps(observation, indent=2))
    assert observed
    assert error is not None, observation
    assert str(config) not in resolution._TSCONFIG_ALIAS_CACHE


def test_strict_svelte_supplement_target_metadata_R004(tmp_path, monkeypatch):
    import graphify.extract as extraction
    import graphify.extractors.resolution as resolution

    source, child, config = _supplement_fixture(tmp_path, "svelte")
    action = lambda: extraction.extract([source, child], cache_root=tmp_path, parallel=False, strict=True)
    resolution._TSCONFIG_ALIAS_CACHE.clear()
    expected = _assert_supplement_edge(action(), source.name, child.name)
    original = os.stat
    observed = []
    def stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == child and _inside("_resolve_tsconfig_alias") and _inside("extract_svelte"):
            observed.append({"path": str(path), "operation": "os.stat", "helper": "_resolve_tsconfig_alias"})
            raise OSError(5, "selected Svelte supplemental alias target metadata")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", stat)
    error, result = None, None
    try:
        result = action()
    except (OSError, RuntimeError) as exc:
        error = str(exc)
    observation = {"case": "R004", "healthy_edges": expected, "operations": observed,
                   "error": error, "fault_edges": result["edges"] if result else None}
    (tmp_path / "batch1-observation.json").write_text(json.dumps(observation, indent=2))
    assert observed
    assert error is not None, observation
