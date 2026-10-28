"""Markdown code-span mentions become heading --references--> symbol edges.

``extract_markdown``'s docstring promised ``heading --references--> other node``
for a backtick `Name`, but nothing implemented it: a docs corpus and its code
corpus shared no edge at all. Mentions now ride the ``raw_calls`` channel
(tagged ``language: "markdown"``) and the ``markdown_mentions`` language
resolver matches them against the merged corpus after the id-remap passes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graphify.extract import extract
from graphify.extractors.markdown import _code_span_mention, extract_markdown
from graphify.markdown_resolution import _match_cited_file

_WIDGET_PY = '''\
class Widget:
    def render(self):
        return "w"


def helper():
    return Widget()
'''

_GADGET_PY = '''\
class Gadget:
    def render(self):
        return "g"


def helper():
    return Gadget()
'''

_GUIDE_MD = '''\
# Guide

Start with `Widget`; `str` and `print()` are built-ins.

## Rendering

`Widget` draws through `Widget.render()`; `helper()` is defined twice.

```python
print(Widget)
```

## Pinned

See `src/widget.py::Widget::render` and `src/gadget.py::helper`.
Also `../src/widget.py::Widget` and the missing `src/nowhere.py::Widget`.
'''


def _extract(tmp_path, files: dict[str, str]):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        # v8 separates the logical source root from an external cache root.
        return extract([Path(n) for n in files], source_root=tmp_path,
                       cache_root=tmp_path / ".cache", parallel=False)
    finally:
        os.chdir(old)


def _node(r, label, source_contains=""):
    return next(n for n in r["nodes"]
                if n["label"] == label and source_contains in str(n.get("source_file", "")))


def _references(r):
    return {(e["source"], e["target"]): e for e in r["edges"]
            if e["relation"] == "references"}


def _native_rebuild(corpus: Path, changed_paths=None):
    """Exercise normal queue admission, publication and receipt closure."""
    from graphify import transaction as tx
    from graphify.watch import _rebuild_code

    assert _rebuild_code(
        corpus, changed_paths=changed_paths, no_cluster=True, block_on_lock=True,
    ) is True
    output = corpus / 'graphify-out'
    status = tx.transaction_status(output)
    assert status['protocol_state'] == 'COMPLETE'
    assert status['transaction'] is None
    assert status['drainer']['state'] == 'complete'
    assert status['queue']['count'] == 0
    assert status['inflight'] == {}
    with tx.pin_output(output, mutation=False) as capability, tx._locked(capability):
        tx._validate_receipt_locked(capability, require_closed=True)
    graph = tx.open_graph_snapshot(output / 'graph.json', purpose='markdown-test').data
    assert all('_callable' not in node and '_callable_class' not in node
               for node in graph['nodes'])
    documents = {node['id'] for node in graph['nodes']
                 if node.get('file_type') == 'document'}
    assert not [edge for edge in graph['links']
                if edge.get('source') in documents
                and edge.get('relation') in {'calls', 'indirect_call'}]
    return graph


def _native_mentions(graph):
    return [edge for edge in graph['links'] if edge.get('_markdown_mention') == 1]


def _native_corpus(tmp_path):
    corpus = tmp_path / 'corpus'
    corpus.mkdir()
    (corpus / 'widget.py').write_text(_WIDGET_PY)
    (corpus / 'doc.md').write_text('# Doc\n\n## Usage\n\nBuild a `Widget`.\n')
    return corpus


def test_code_span_mentions_are_classified():
    assert _code_span_mention("Widget") == (None, ["Widget"])
    assert _code_span_mention("render()") == (None, ["render"])
    assert _code_span_mention("pkg.sub.Widget") == (None, ["pkg", "sub", "Widget"])
    assert _code_span_mention("Widget.render()") == (None, ["Widget", "render"])
    # A file-like span with an unknown extension classifies as a mention and
    # is rejected at resolution, where `pyproject` matches no callable.
    assert _code_span_mention("pyproject.toml") == (None, ["pyproject", "toml"])
    pinned = ("src/widget.py", ["Widget", "render"])
    assert _code_span_mention("src/widget.py::Widget::render") == pinned
    assert _code_span_mention("src/widget.py::Widget::render()") == pinned
    # Files, commands, expressions and prose are not symbol mentions.
    for span in ("setup.py", "README.md", "notes.txt", "git revert", "x = 1", "a-b",
                 "--flag", "", "src/widget.py"):
        assert _code_span_mention(span) is None, span


def test_cited_file_matching():
    sources = {"src/mod.py", ".github/scripts/check.py", "lib/x.py", "vendor/lib/x.py",
               "docs/src/mod.py"}
    # Doc-relative first, then exact, then a unique segment-aligned suffix.
    assert _match_cited_file("src/mod.py", "docs/guide.md", sources) == "docs/src/mod.py"
    assert _match_cited_file("src/mod.py", "README.md", sources) == "src/mod.py"
    assert _match_cited_file("mod.py", "README.md", sources) is None
    assert _match_cited_file("scripts/check.py", "README.md", sources) == (
        ".github/scripts/check.py")
    # `./` and `../` are stripped as segments: a hidden directory keeps its dot.
    assert _match_cited_file(".github/scripts/check.py", "docs/guide.md", sources) == (
        ".github/scripts/check.py")
    assert _match_cited_file("./src/mod.py", "README.md", sources) == "src/mod.py"
    # The suffix form also names a root-level file.
    assert _match_cited_file("lib/x.py", "docs/guide.md", {"lib/x.py"}) == "lib/x.py"
    # An explicit relative cite resolves against the document only: escaping
    # the corpus never falls through to another copy of the file.
    assert _match_cited_file("../lib/x.py", "docs/guide.md", sources) == "lib/x.py"
    assert _match_cited_file("../lib/x.py", "README.md", sources) is None
    assert _match_cited_file("../../vendor/lib/x.py", "docs/guide.md", sources) is None


def test_extract_markdown_reports_mentions_as_markdown_raw_calls(tmp_path):
    doc = tmp_path / "docs" / "guide.md"
    doc.parent.mkdir()
    doc.write_text(_GUIDE_MD)

    r = extract_markdown(doc)

    assert r["edges"] == [e for e in r["edges"] if e["relation"] == "contains"]
    calls = r["raw_calls"]
    assert calls and all(rc["language"] == "markdown" for rc in calls)
    assert all(rc["is_member_call"] is False for rc in calls)
    heading_ids = {n["label"]: n["id"] for n in r["nodes"]}
    owners = {(rc["context"], rc["caller_nid"]) for rc in calls}
    # Body prose belongs to the enclosing heading; the first paragraph to the H1.
    assert ("Widget", heading_ids["Guide"]) in owners
    assert ("Widget", heading_ids["Rendering"]) in owners
    dotted = next(rc for rc in calls if rc["context"] == "Widget.render()")
    assert (dotted["caller_nid"], dotted["callee"]) == (heading_ids["Rendering"], "render")
    pinned = next(rc for rc in calls if rc["context"] == "src/widget.py::Widget::render")
    assert (pinned["path"], pinned["qualifiers"], pinned["callee"]) == (
        "src/widget.py", ["Widget"], "render")
    assert pinned["caller_nid"] == heading_ids["Pinned"]
    assert pinned["source_location"] == "L15"
    # Fenced code is not prose: `print(Widget)` inside the block adds nothing.
    assert [rc for rc in calls if rc["source_location"] == "L10"] == []
    # One raw call per (owner, mention), however often the heading repeats it.
    assert len([rc for rc in calls if rc["callee"] == "Widget"
                and rc["caller_nid"] == heading_ids["Rendering"]]) == 1


def test_extract_markdown_heading_code_span_belongs_to_that_heading(tmp_path):
    doc = tmp_path / "api.md"
    doc.write_text("# API\n\n## `Widget`\n\nText.\n")

    r = extract_markdown(doc)

    (rc,) = r["raw_calls"]
    heading = next(n for n in r["nodes"] if n["label"] == "`Widget`")
    assert (rc["caller_nid"], rc["callee"], rc["path"] if "path" in rc else None) == (
        heading["id"], "Widget", None)


def test_mentions_resolve_to_references_edges_end_to_end(tmp_path):
    r = _extract(tmp_path, {
        "src/widget.py": _WIDGET_PY,
        "src/gadget.py": _GADGET_PY,
        "docs/guide.md": _GUIDE_MD,
    })

    refs = _references(r)
    guide = _node(r, "Guide")
    rendering = _node(r, "Rendering")
    pinned = _node(r, "Pinned")
    widget = _node(r, "Widget", "widget.py")
    widget_render = next(
        n for n in r["nodes"] if n["label"] == ".render()" and "widget" in n["id"])
    gadget_helper = _node(r, "helper()", "gadget.py")

    # Bare unique name: INFERRED reference from the citing heading.
    assert refs[(guide["id"], widget["id"])]["confidence"] == "INFERRED"
    assert refs[(guide["id"], widget["id"])]["confidence_score"] == 0.95
    assert (rendering["id"], widget["id"]) in refs
    # `render` is defined twice, but the qualifier in `Widget.render()` names
    # the owner, so the mention resolves; the bare `helper()` stays ambiguous.
    assert {t for (s, t) in refs if s == rendering["id"]} == {
        widget["id"], widget_render["id"]}
    assert refs[(rendering["id"], widget_render["id"])]["confidence"] == "INFERRED"
    # Path-qualified mentions are EXTRACTED and scoped to the cited file.
    assert refs[(pinned["id"], widget_render["id"])]["confidence"] == "EXTRACTED"
    assert refs[(pinned["id"], widget_render["id"])]["confidence_score"] == 1.0
    assert refs[(pinned["id"], gadget_helper["id"])]["confidence"] == "EXTRACTED"
    # `../src/widget.py::Widget` resolves relative to the document.
    assert (pinned["id"], widget["id"]) in refs
    # A cited file that is not in the corpus yields nothing.
    assert {t for (s, t) in refs if s == pinned["id"]} == {
        widget_render["id"], gadget_helper["id"], widget["id"]}
    # Built-ins never resolve, and no mention becomes a call.
    node_ids = {n["id"] for n in r["nodes"]}
    assert all(s in node_ids and t in node_ids for (s, t) in refs)
    doc_ids = {n["id"] for n in r["nodes"] if n.get("file_type") == "document"}
    assert not [e for e in r["edges"]
                if e["relation"] in ("calls", "indirect_call") and e["source"] in doc_ids]


def test_ambiguous_bare_name_yields_no_edge(tmp_path):
    r = _extract(tmp_path, {
        "a/thing.py": "class Thing:\n    pass\n",
        "b/thing.py": "class Thing:\n    pass\n",
        "notes.md": "# Notes\n\nUse `Thing`.\n",
    })

    notes = _node(r, "Notes")
    assert {t for (s, t) in _references(r) if s == notes["id"]} == set()


def test_dotted_mentions_need_qualifier_evidence(tmp_path):
    r = _extract(tmp_path, {
        "src/widget.py": _WIDGET_PY + "\n\ndef sleep():\n    pass\n",
        "src/gadget.py": _GADGET_PY + "\n\ndef toml():\n    pass\n",
        "docs/notes.md": (
            "# Notes\n\n"
            "`time.sleep` and `pyproject.toml` are not this repo's sleep or toml.\n"
            "`widget.Widget`, `src.gadget.Gadget` and `Widget.render` are.\n"
            "`Gadget.helper` is not: `helper` is not owned by `Gadget`.\n"
        ),
    })

    notes = _node(r, "Notes")
    cited = {t for (s, t) in _references(r) if s == notes["id"]}
    widget = _node(r, "Widget", "widget.py")
    gadget = _node(r, "Gadget", "gadget.py")
    widget_render = next(
        n for n in r["nodes"] if n["label"] == ".render()" and "widget" in n["id"])
    assert cited == {widget["id"], gadget["id"], widget_render["id"]}


def test_mentions_survive_a_rebuild_over_an_existing_graph(tmp_path):
    """The watch reconcile owns authored ``[link](file)`` edges, not mentions.

    A rebuild over an existing graph re-parses the Markdown corpus and prunes
    any ``references`` edge it did not author; a code-span mention targets a
    code symbol, never a file, so it must survive a no-change rebuild and the
    incremental rebuilds of either side.
    """
    corpus = _native_corpus(tmp_path)
    doc = corpus / "doc.md"

    def mention_edges(changed_paths=None):
        return {(edge['source'], edge['target'])
                for edge in _native_mentions(_native_rebuild(corpus, changed_paths))}

    expected = mention_edges()
    assert len(expected) == 1
    assert mention_edges() == expected, "no-change rebuild"

    doc.write_text(doc.read_text() + "\nStill a `Widget`.\n")
    assert mention_edges([doc]) == expected, "document re-extracted"

    (corpus / "widget.py").write_text(_WIDGET_PY + "\n\ndef extra():\n    pass\n")
    assert mention_edges([corpus / 'widget.py']) == expected, "code re-extracted"


def test_mentions_survive_the_extraction_cache(tmp_path):
    files = {"src/widget.py": _WIDGET_PY, "docs/guide.md": _GUIDE_MD}
    first = _extract(tmp_path, files)
    second = _extract(tmp_path, files)

    def _pairs(r):
        return {(s, t) for (s, t) in _references(r)}

    assert _pairs(first) == _pairs(second)
    guide = _node(second, "Guide")
    widget = _node(second, "Widget", "widget.py")
    assert (guide["id"], widget["id"]) in _pairs(second)


def test_bare_lowercase_word_does_not_link_to_an_unrelated_method(tmp_path):
    """`redis` the server is prose, not a reference to whichever method is named redis (#4609)."""
    r = _extract(tmp_path, {
        "cache.py": "class Cache:\n    def redis(self):\n        return 'connection'\n",
        "README.md": "# Queue\n\nJobs run on `redis` (the server, not our code).\n",
    })

    queue = _node(r, "Queue")
    assert {t for (s, t) in _references(r) if s == queue["id"]} == set()


def test_bare_method_mention_survives_when_the_document_cites_its_class(tmp_path):
    """`successWithWarnings` next to `HandlerResult` is a real reference (#4609)."""
    r = _extract(tmp_path, {
        "handler.py": "class HandlerResult:\n    def successWithWarnings(self):\n        return 1\n",
        "notes.md": "# Notes\n\n`HandlerResult` returns `successWithWarnings`.\n",
    })

    notes = _node(r, "Notes")
    method = _node(r, ".successWithWarnings()")
    assert (notes["id"], method["id"]) in _references(r)


def test_bare_method_with_parentheses_still_resolves(tmp_path):
    """A trailing () is explicit evidence, so the owner does not have to be cited (#4609)."""
    r = _extract(tmp_path, {
        "cache.py": "class Cache:\n    def redis(self):\n        return 'connection'\n",
        "notes.md": "# Notes\n\nCall `redis()` on the cache.\n",
    })

    notes = _node(r, "Notes")
    method = _node(r, ".redis()")
    assert (notes["id"], method["id"]) in _references(r)


def test_bare_function_mention_still_resolves_without_its_owner(tmp_path):
    """The extra owner test is scoped to methods; a free function label stays evidence (#4609)."""
    r = _extract(tmp_path, {
        "deploy.py": "def deploy():\n    pass\n",
        "notes.md": "# Notes\n\nRun `deploy` after the tests.\n",
    })

    notes = _node(r, "Notes")
    deploy = _node(r, "deploy()", "deploy.py")
    assert (notes["id"], deploy["id"]) in _references(r)


def test_owner_cited_in_another_document_does_not_count(tmp_path):
    """The owner evidence is per document: one doc's mention cannot license another's (#4609)."""
    r = _extract(tmp_path, {
        "handler.py": "class HandlerResult:\n    def successWithWarnings(self):\n        return 1\n",
        "a.md": "# A\n\n`HandlerResult` is the result type.\n",
        "b.md": "# B\n\nReads `successWithWarnings`.\n",
    })

    b = _node(r, "B")
    assert {t for (s, t) in _references(r) if s == b["id"]} == set()


def test_owner_evidence_survives_an_edge_that_already_exists():
    """A mention whose edge is already in the graph still counts as citing its target.

    The resolver skips an edge that is already there, but the document has still
    cited that symbol, so the evidence must stay on record for a later method
    mention in the same document (#4609, review finding on the PR).
    """
    from graphify.markdown_resolution import resolve_markdown_mentions

    per_file = [{"raw_calls": [
        {"caller_nid": "doc_notes", "callee": "HandlerResult", "language": "markdown",
         "context": "HandlerResult", "source_file": "notes.md", "source_location": "L3"},
        {"caller_nid": "doc_notes", "callee": "successWithWarnings", "language": "markdown",
         "context": "successWithWarnings", "source_file": "notes.md", "source_location": "L3"},
    ]}]
    nodes = [
        {"id": "doc_notes", "label": "Notes", "file_type": "document",
         "source_file": "notes.md"},
        {"id": "handler_handlerresult", "label": "HandlerResult", "file_type": "code",
         "_callable": True, "_callable_class": True, "source_file": "handler.py"},
        {"id": "handler_handlerresult_successwithwarnings", "label": ".successWithWarnings()",
         "file_type": "code", "_callable": True, "source_file": "handler.py"},
    ]
    edges = [
        {"source": "handler_handlerresult",
         "target": "handler_handlerresult_successwithwarnings", "relation": "method"},
        # The class reference edge is already in the graph.
        {"source": "doc_notes", "target": "handler_handlerresult", "relation": "references"},
    ]

    resolve_markdown_mentions(per_file, nodes, edges)

    assert ("doc_notes", "handler_handlerresult_successwithwarnings") in {
        (e["source"], e["target"]) for e in edges if e.get("relation") == "references"}


def test_explicit_call_evidence_survives_an_earlier_bare_method_span(tmp_path):
    result = _extract(tmp_path, {
        'cache.py': 'class Cache:\n    def redis(self):\n        return 1\n',
        'notes.md': '# Notes\n\nThe `redis` service is used by `redis()`.\n',
    })
    notes = _node(result, 'Notes')
    method = _node(result, '.redis()')
    assert (notes['id'], method['id']) in _references(result)


def test_published_extraction_strips_private_callable_markers(tmp_path):
    result = _extract(tmp_path, {
        'widget.py': _WIDGET_PY,
        'doc.md': '# Doc\n\nUse `Widget` and `Widget.render()`.\n',
    })
    assert _references(result)
    assert all('_callable' not in node and '_callable_class' not in node
               for node in result['nodes'])


def test_scoped_mentions_account_for_inputs_without_writing(tmp_path):
    from graphify.source_io import SourceIO

    source = tmp_path / 'widget.py'
    source.write_text(_WIDGET_PY)
    doc = tmp_path / 'doc.md'
    doc.write_text('# Doc\n\nUse `Widget.render()`.\n')
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with SourceIO(tmp_path) as inputs:
        result = extract([source, doc], source_root=tmp_path, source_io=inputs,
                         quiet=True, ambient_output=False)
        reads = {record['path'] for record in inputs.evidence
                 if record['operation'] == 'read'}
        assert {'widget.py', 'doc.md'} <= reads
        assert len(result['outcomes']) == 2
        assert {outcome['status'] for outcome in result['outcomes']} == {'success'}
    assert _references(result)
    assert all('_callable' not in node and '_callable_class' not in node
               for node in result['nodes'])
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_native_code_changes_recheck_unchanged_document_ambiguity(tmp_path):
    corpus = _native_corpus(tmp_path)
    assert len(_native_mentions(_native_rebuild(corpus))) == 1

    other = corpus / 'other.py'
    other.write_text('class Widget:\n    pass\n')
    assert _native_mentions(_native_rebuild(corpus, [other])) == []

    other.unlink()
    edges = _native_mentions(_native_rebuild(corpus, [other]))
    assert len(edges) == 1
    assert edges[0]['confidence'] == 'INFERRED'


def test_native_doc_update_does_not_borrow_stale_definition(tmp_path):
    corpus = _native_corpus(tmp_path)
    assert len(_native_mentions(_native_rebuild(corpus))) == 1

    # The document event must not make the previous graph's class definition
    # current evidence when those code bytes changed without a matching event.
    (corpus / 'widget.py').write_text('class Gadget:\n    pass\n')
    doc = corpus / 'doc.md'
    doc.write_text(doc.read_text() + '\nAdditional prose.\n')
    assert _native_mentions(_native_rebuild(corpus, [doc])) == []


@pytest.mark.parametrize('change', ['rename', 'delete'])
def test_native_path_qualified_target_change_removes_old_reference(tmp_path, change):
    corpus = _native_corpus(tmp_path)
    doc = corpus / 'doc.md'
    doc.write_text('# Doc\n\nUse `widget.py::Widget`.\n')
    assert len(_native_mentions(_native_rebuild(corpus))) == 1

    source = corpus / 'widget.py'
    if change == 'rename':
        renamed = corpus / 'renamed.py'
        source.rename(renamed)
        changed = [source, renamed]
    else:
        source.unlink()
        changed = [source]
    assert _native_mentions(_native_rebuild(corpus, changed)) == []

    if change == 'rename':
        doc.write_text('# Doc\n\nUse `renamed.py::Widget`.\n')
        assert len(_native_mentions(_native_rebuild(corpus, [doc]))) == 1


def _add_semantic_fixture(corpus: Path):
    """Publish fixture semantics through the normal generation authority."""
    from graphify import transaction as tx

    output = corpus / 'graphify-out'
    graph = json.loads((output / 'graph.json').read_bytes())
    widget = next(node for node in graph['nodes'] if node['label'] == 'Widget')
    semantic_node = {
        'id': 'semantic_note', 'label': 'Retained semantic fact',
        'file_type': 'concept', 'source_file': 'semantic.md',
    }
    semantic_edge = {
        'source': semantic_node['id'], 'target': widget['id'],
        'relation': 'references', 'confidence': 'INFERRED',
        'confidence_score': 0.61, 'weight': 1.0, 'source_file': 'semantic.md',
    }
    graph['nodes'].append(semantic_node)
    graph['links'].append(semantic_edge)
    required = tuple(json.loads((output / '.graphify_generation.json').read_bytes())[
        'required_artifacts'])
    transaction = tx.begin_transaction('runtime', corpus, output=output)
    tx.stage_transaction_handoff(transaction)
    transaction = tx.resume_transaction(transaction.id, corpus, output=output)
    graph['graph'][tx.GRAPH_WATERMARK_KEY]['generation'] = transaction.generation
    payload = json.dumps(graph).encode()
    manifest_payload = (output / 'manifest.json').read_bytes()
    with tx.owned_step(transaction):
        tx.commit_bytes(transaction, 'graph.json', payload)
        tx.commit_generation(transaction, graph_payload=payload,
                             manifest_payload=manifest_payload,
                             required_artifacts=required)
    tx.finish_transaction(transaction)
    return semantic_node, semantic_edge


def test_native_mention_refresh_preserves_semantic_nodes_and_edges(tmp_path):
    corpus = _native_corpus(tmp_path)
    (corpus / 'semantic.md').write_text('# Semantic notes\n')
    _native_rebuild(corpus)
    semantic_node, semantic_edge = _add_semantic_fixture(corpus)

    doc = corpus / 'doc.md'
    doc.write_text(doc.read_text() + '\nStill a `Widget`.\n')
    graph = _native_rebuild(corpus, [doc])
    assert semantic_node in graph['nodes']
    assert semantic_edge in graph['links']
    assert len(_native_mentions(graph)) == 1


def test_native_doc_event_rechecks_same_size_same_mtime_code_bytes(tmp_path):
    from graphify.cache import file_hash

    corpus = _native_corpus(tmp_path)
    source = corpus / 'widget.py'
    assert len(_native_mentions(_native_rebuild(corpus))) == 1
    # Prime the normal stat-index fast path before changing only content.
    assert file_hash(source, root=corpus)
    before = source.stat()
    old_bytes = source.read_bytes()
    new_bytes = old_bytes.replace(b'Widget', b'Gadget')
    assert len(new_bytes) == len(old_bytes) and new_bytes != old_bytes
    source.write_bytes(new_bytes)
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert source.stat().st_size == before.st_size
    assert source.stat().st_mtime_ns == before.st_mtime_ns

    doc = corpus / 'doc.md'
    doc.write_text(doc.read_text() + '\nDoc-only event after an unreported code edit.\n')
    graph = _native_rebuild(corpus, [doc])
    assert _native_mentions(graph) == []


def test_native_mentions_follow_salted_ids_through_partial_rebuilds(tmp_path):
    from graphify.ids import make_id

    corpus = tmp_path / 'corpus'
    corpus.mkdir()
    files = ('a.b.py', 'a_b.py')
    # Establish the collision trigger, rather than merely testing two files
    # whose unsalted names happen to be different.
    assert make_id(Path(files[0]).stem, 'Worker') == make_id(Path(files[1]).stem, 'Worker')
    body = ('class Worker:\n'
            '    def run(self):\n'
            '        return 1\n\n'
            'def helper():\n'
            '    return Worker()\n')
    for filename in files:
        (corpus / filename).write_text(body)
    doc = corpus / 'doc.md'
    doc.write_text('# Guide\n\n'
                   '`a.b.py::Worker::run` and `a_b.py::Worker::run`.\n\n'
                   'Bare `Worker` and `helper()` are ambiguous.\n')

    def check(graph):
        ids = [node['id'] for node in graph['nodes']]
        assert len(ids) == len(set(ids))
        nodes = {node['id']: node for node in graph['nodes']}
        methods = {
            name: next(node for node in graph['nodes']
                       if node.get('source_file') == name and node['label'] == '.run()')
            for name in files
        }
        assert methods[files[0]]['id'] != methods[files[1]]['id']
        mentions = _native_mentions(graph)
        assert len(mentions) == 2
        assert {edge['context'] for edge in mentions} == {
            'a.b.py::Worker::run', 'a_b.py::Worker::run',
        }
        for edge in mentions:
            filename = edge['context'].split('::', 1)[0]
            assert edge['target'] == methods[filename]['id']
            assert edge['source'] in nodes and edge['target'] in nodes
            assert nodes[edge['source']]['source_file'] == 'doc.md'
            assert edge['source_file'] == 'doc.md'
            assert edge['confidence'] == 'EXTRACTED'
            assert edge['_origin'] == 'ast'
        assert all(edge['source'] in nodes and edge['target'] in nodes
                   for edge in graph['links'])

    check(_native_rebuild(corpus))
    doc.write_text(doc.read_text() + '\nOnly the document changed.\n')
    check(_native_rebuild(corpus, [doc]))
    source = corpus / files[0]
    source.write_text(body.replace('return 1', 'return 2'))
    check(_native_rebuild(corpus, [source]))


def test_frontmatter_mentions_are_suppressed_without_losing_document_links(tmp_path):
    result = _extract(tmp_path, {
        'widget.py': _WIDGET_PY,
        'doc.md': ('---\n'
                   'api: "`Widget` and `Widget.render()`"\n'
                   'related: "[Other](./other.md)"\n'
                   '---\n'
                   '# Guide\n\nBody prose without a symbol citation.\n'),
        'other.md': '# Other\n',
    })
    raw = extract_markdown(tmp_path / 'doc.md')
    assert raw['raw_calls'] == []
    doc = _node(result, 'doc.md')
    other = _node(result, 'other.md')
    assert (doc['id'], other['id']) in _references(result)
    assert not [edge for edge in result['edges'] if edge.get('_markdown_mention')]


def test_fenced_heading_does_not_own_following_mentions(tmp_path):
    result = _extract(tmp_path, {
        'widget.py': _WIDGET_PY,
        'doc.md': '# Guide\n~~~md\n# Example\n~~~\nUse `Widget`.\n',
    })
    mention, = [edge for edge in result['edges'] if edge.get('_markdown_mention')]
    assert mention['source'] == _node(result, 'Guide')['id']
    assert mention['target'] == _node(result, 'Widget')['id']
    assert mention['source_location'] == 'L5'


@pytest.mark.parametrize('fenced_body', [
    '~~~python\n`Widget`\n[Other](./other.md)\n~~~',
    '````markdown\n`Widget`\n```\n`Widget`\n[Other](./other.md)\n````',
], ids=['tilde', 'long-backtick'])
def test_mentions_respect_fence_delimiter_and_length(tmp_path, fenced_body):
    body = '# Guide\n\n' + fenced_body + '\n\nAfter the fence: `Widget`.\n'
    result = _extract(tmp_path, {
        'widget.py': _WIDGET_PY,
        'doc.md': body,
        'other.md': '# Other\n',
    })
    # Existing v8 link scanning sees these links inside both fixtures. New
    # mention fence rules must not change that separate document-link behavior.
    doc = _node(result, 'doc.md')
    other = _node(result, 'other.md')
    assert (doc['id'], other['id']) in _references(result)
    raw = extract_markdown(tmp_path / 'doc.md')['raw_calls']
    after_line = f'L{len(body.splitlines())}'
    assert [(call['context'], call['source_location']) for call in raw] == [
        ('Widget', after_line),
    ]
    guide = _node(result, 'Guide', 'doc.md')
    widget = _node(result, 'Widget', 'widget.py')
    mentions = [edge for edge in result['edges'] if edge.get('_markdown_mention')]
    assert len(mentions) == 1
    assert (mentions[0]['source'], mentions[0]['target']) == (guide['id'], widget['id'])
    assert mentions[0]['source_location'] == after_line


@pytest.mark.parametrize('suffix', ['.MD', '.mDx'])
@pytest.mark.parametrize('scoped', [False, True], ids=['public', 'scoped'])
def test_mixed_case_markdown_suffix_resolves_mentions(tmp_path, suffix, scoped):
    filename = 'guide' + suffix
    files = {'widget.py': _WIDGET_PY, filename: '# Guide\n\nUse `Widget.render()`.\n'}
    if scoped:
        from graphify.source_io import SourceIO

        for name, body in files.items():
            (tmp_path / name).write_text(body)
        before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
        with SourceIO(tmp_path) as inputs:
            result = extract([tmp_path / name for name in files], source_root=tmp_path,
                             source_io=inputs, quiet=True, ambient_output=False)
            reads = {record['path'] for record in inputs.evidence
                     if record['operation'] == 'read'}
            assert set(files) <= reads
            assert len(result['outcomes']) == 2
            assert {outcome['status'] for outcome in result['outcomes']} == {'success'}
        assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    else:
        result = _extract(tmp_path, files)
    guide = _node(result, 'Guide', filename)
    method = _node(result, '.render()', 'widget.py')
    mentions = [edge for edge in result['edges'] if edge.get('_markdown_mention')]
    assert len(mentions) == 1
    assert (mentions[0]['source'], mentions[0]['target']) == (guide['id'], method['id'])
    assert mentions[0]['context'] == 'Widget.render()'
    assert all('_callable' not in node and '_callable_class' not in node
               for node in result['nodes'])
    documents = {node['id'] for node in result['nodes']
                 if node.get('file_type') == 'document'}
    assert not [edge for edge in result['edges']
                if edge['source'] in documents
                and edge['relation'] in {'calls', 'indirect_call'}]


@pytest.mark.parametrize(('body', 'expected'), [
    ('<!-- `Hidden` -->\nUse `Visible`.\n', [('Visible', 'L2')]),
    ('Before `Before` <!-- `Hidden` --> after `After`.\n',
     [('Before', 'L1'), ('After', 'L1')]),
    ('<!--\n# Hidden heading\n~~~\n`Hidden`\n-->\nUse `Visible`.\n',
     [('Visible', 'L6')]),
    ('Use `<!-- Widget -->` and `Visible`.\n', [('Visible', 'L1')]),
    ('Use `<!--` then `Visible`.\n', [('Visible', 'L1')]),
    ('Use `Wid<!-- hidden -->get` and `Visible`.\n', [('Visible', 'L1')]),
    ('<!-- `Hidden`\nnever closed\n`AlsoHidden`\n', []),
], ids=['same-line-comment', 'inline-comment', 'multiline-comment',
        'literal-comment-span', 'literal-opener-span', 'no-mask-created-symbol',
        'unclosed-comment'])
def test_mentions_respect_html_comments(tmp_path, body, expected):
    doc = tmp_path / 'doc.md'
    doc.write_text(body)
    result = extract_markdown(doc)
    assert [(call['context'], call['source_location'])
            for call in result['raw_calls']] == expected


@pytest.mark.parametrize(('body', 'expected'), [
    ('    `Hidden`\n\t`AlsoHidden`\n\nUse `Visible`.\n', [('Visible', 'L4')]),
    ('Paragraph\n    `Continued`\n', [('Continued', 'L2')]),
    ('Paragraph\n\n    `Hidden`\n', []),
    ('- Use `Visible`.\n    and `Continued`.\n',
     [('Visible', 'L1'), ('Continued', 'L2')]),
    ('- First paragraph.\n\n    Use `Visible`.\n', [('Visible', 'L3')]),
    ('    <!--\n\nUse `Visible`.\n', [('Visible', 'L3')]),
    ('```text\n<!--\n```\nUse `Visible`.\n', [('Visible', 'L4')]),
], ids=['space-and-tab-blocks', 'paragraph-continuation', 'paragraph-separation',
        'list-continuation', 'list-paragraph', 'indented-literal-comment',
        'fenced-literal-comment'])
def test_mentions_respect_indented_code_and_prose(tmp_path, body, expected):
    doc = tmp_path / 'doc.md'
    doc.write_text(body)
    result = extract_markdown(doc)
    assert [(call['context'], call['source_location'])
            for call in result['raw_calls']] == expected


@pytest.mark.parametrize(('body', 'expected'), [
    ('Use `\nWidget\n` here.\n', [('Widget', 'L1')]),
    ('Use ``\nWidget\n`` and `Later`.\n', [('Widget', 'L1'), ('Later', 'L3')]),
    ('Use `Widget\n` here.\n', [('Widget', 'L1')]),
    ('Use `Wid\nget` here.\n', []),
    ('Use ``\nWidget`\n`` here.\n', []),
    ('Use `\n\nWidget` here.\n', []),
    ('Use `\n# Widget`\n', []),
    ('Use `\n~~~\nWidget`\n~~~\n', []),
    ('Use `\n- Widget`\n', []),
    ('Use `\n> Widget`\n', []),
    ('Use `\n---\nWidget`\n', []),
    ('Use `\n<!-- Widget` -->\n', []),
], ids=['soft-lines', 'double-delimiter', 'trailing-soft-line', 'internal-space',
        'exact-delimiter', 'blank-boundary', 'heading-boundary', 'fence-boundary',
        'list-boundary', 'quote-boundary', 'thematic-boundary', 'html-boundary'])
def test_mentions_support_bounded_multiline_code_spans(tmp_path, body, expected):
    doc = tmp_path / 'doc.md'
    doc.write_text(body)
    result = extract_markdown(doc)
    assert [(call['context'], call['source_location'])
            for call in result['raw_calls']] == expected


def test_comment_headings_do_not_take_mention_ownership_or_change_legacy_links(tmp_path):
    result = _extract(tmp_path, {
        'widget.py': _WIDGET_PY,
        'other.md': '# Other\n',
        'doc.md': ('# Guide\n\n<!--\n# Hidden\n`Widget.render()`\n'
                   '[Other](./other.md)\n-->\nUse `\nWidget\n`.\n'),
    })
    guide = _node(result, 'Guide', 'doc.md')
    widget = _node(result, 'Widget', 'widget.py')
    mentions = [edge for edge in result['edges'] if edge.get('_markdown_mention')]
    assert [(edge['source'], edge['target'], edge['context'], edge['source_location'])
            for edge in mentions] == [(guide['id'], widget['id'], 'Widget', 'L8')]
    # Legacy structural and link outputs deliberately still include comment text.
    assert _node(result, 'Hidden', 'doc.md')
    assert (_node(result, 'doc.md')['id'], _node(result, 'other.md')['id']) in _references(result)


def test_multiline_span_keeps_its_opening_heading_and_source_location(tmp_path):
    doc = tmp_path / 'doc.md'
    doc.write_text('# Guide\n\nUse ``\nWidget\n``.\n\n## Next\n\nUse `Other`.\n')
    result = extract_markdown(doc)
    headings = {node['label']: node['id'] for node in result['nodes']}
    assert [(call['context'], call['caller_nid'], call['source_location'])
            for call in result['raw_calls']] == [
        ('Widget', headings['Guide'], 'L3'), ('Other', headings['Next'], 'L9'),
    ]


@pytest.mark.parametrize(('body', 'visible_line'), [
    ('<script>const api = `Hidden`;</script>\nUse `Visible`.\n', 2),
    ('<pre>\n`Hidden`\n</pre>\nUse `Visible`.\n', 4),
    ('<STYLE>\n\n`Hidden`\n</STYLE>\nUse `Visible`.\n', 5),
    ('<textarea>\n`Hidden`\n</pre>\nUse `Visible`.\n', 4),
    ('<div>\n`Hidden`\n</div>\n`AlsoHidden`\n\nUse `Visible`.\n', 6),
    ('<custom-element title="example">\n`Hidden`\n</custom-element>\n\nUse `Visible`.\n', 5),
    ('<?processing\n`Hidden`\n?>\nUse `Visible`.\n', 4),
    ('<!DOCTYPE\n`Hidden`\n>\nUse `Visible`.\n', 4),
    ('<![CDATA[\n`Hidden`\n]]>\nUse `Visible`.\n', 4),
    ('> <pre>\n> `Hidden`\nUse `Visible`.\n', 3),
    ('- <pre>\n  `Hidden`\n\nUse `Visible`.\n', 4),
], ids=['script', 'pre', 'style-blank-line', 'raw-tag-close', 'block-tag-blank-end',
        'custom-tag', 'processing', 'declaration', 'cdata', 'quote-end', 'list-end'])
def test_mentions_exclude_raw_html_blocks(tmp_path, body, visible_line):
    doc = tmp_path / 'doc.md'
    doc.write_text('# Guide\n\n' + body)
    result = extract_markdown(doc)
    guide = next(node['id'] for node in result['nodes'] if node['label'] == 'Guide')
    assert [(call['context'], call['caller_nid'], call['source_location'])
            for call in result['raw_calls']] == [('Visible', guide, f'L{visible_line + 2}')]


@pytest.mark.parametrize('body', [
    'Use <span>`Visible`</span>.\n',
    'Use `<pre>` and `Visible`.\n',
    'Paragraph\n<custom-element>\nUse `Visible`.\n',
    '<span>`Visible`</span>\n',
])
def test_inline_html_and_code_literals_keep_visible_mentions(tmp_path, body):
    doc = tmp_path / 'doc.md'
    doc.write_text(body)
    assert [call['context'] for call in extract_markdown(doc)['raw_calls']] == ['Visible']


@pytest.mark.parametrize('block', [
    '> ```python\n> `Hidden`\n> ```',
    '- ```python\n  `Hidden`\n  ```',
    '1. ~~~python\n   `Hidden`\n   ~~~',
    '> > ````python\n> > `Hidden`\n> > ```\n> > `AlsoHidden`\n> > ````',
    '> - ```python\n>   `Hidden`\n>   ```',
    '- > ```python\n  > `Hidden`\n  > ```',
    '- Item\n  - ```python\n    `Hidden`\n    ```',
    '> ```python\n> `Hidden`',
    '- ```python\n  `Hidden`',
], ids=['quote', 'list', 'ordered-list', 'nested-quotes', 'quoted-list',
        'listed-quote', 'nested-list', 'unclosed-quote', 'unclosed-list'])
def test_mentions_respect_container_fences_and_their_lifetime(tmp_path, block):
    body = '# Guide\n\n' + block + '\n\nUse `Visible`.\n'
    doc = tmp_path / 'doc.md'
    doc.write_text(body)
    result = extract_markdown(doc)
    guide = next(node['id'] for node in result['nodes'] if node['label'] == 'Guide')
    assert [(call['context'], call['caller_nid'], call['source_location'])
            for call in result['raw_calls']] == [
        ('Visible', guide, f'L{len(body.splitlines())}'),
    ]


def test_container_prose_is_scanned_and_literal_fence_content_stays_literal(tmp_path):
    doc = tmp_path / 'doc.md'
    doc.write_text('> Use `Quoted`.\n- Use `Listed`.\n\n'
                   '```text\n> ```\n`Hidden`\n```\nUse `Visible`.\n')
    assert [call['context'] for call in extract_markdown(doc)['raw_calls']] == [
        'Quoted', 'Listed', 'Visible',
    ]


@pytest.mark.parametrize(('span', 'expected'), [
    ('ready?()', (None, ['ready?'])),
    ('save!()', (None, ['save!'])),
    ('Widget.ready?()', (None, ['Widget', 'ready?'])),
    ('stream$()', (None, ['stream$'])),
    ('$stream()', (None, ['$stream'])),
    ('pkg.stream$pipe()', (None, ['pkg', 'stream$pipe'])),
    ('api.rb::Widget::ready?()', ('api.rb', ['Widget', 'ready?'])),
    ('api.js::stream$()', ('api.js', ['stream$'])),
])
def test_language_specific_symbol_punctuation_is_classified(span, expected):
    assert _code_span_mention(span) == expected
    for prose in ('ready??()', 'save!!()', 'ready? now', 'a!=b', 'file.rb', 'file.js'):
        assert _code_span_mention(prose) is None


@pytest.mark.parametrize(('filename', 'source', 'symbol'), [
    ('api.rb', 'def ready?\n  true\nend\n', 'ready?'),
    ('api.rb', 'def save!\nend\n', 'save!'),
    ('api.js', 'function stream$() {}\n', 'stream$'),
])
def test_language_specific_symbol_punctuation_resolves_real_callables(tmp_path, filename,
                                                                   source, symbol):
    result = _extract(tmp_path, {
        filename: source,
        'doc.md': f'# Bare\n\nUse `{symbol}()`.\n\n# Pinned\n\nUse `{filename}::{symbol}()`.\n',
    })
    mentions = [edge for edge in result['edges'] if edge.get('_markdown_mention')]
    nodes = {node['id']: node for node in result['nodes']}
    assert {edge['context'] for edge in mentions} == {f'{symbol}()', f'{filename}::{symbol}()'}
    assert {edge['confidence'] for edge in mentions} == {'INFERRED', 'EXTRACTED'}
    assert {nodes[edge['target']]['label'] for edge in mentions} == {f'{symbol}()'}
    assert all('_callable' not in node and '_callable_class' not in node
               for node in result['nodes'])
