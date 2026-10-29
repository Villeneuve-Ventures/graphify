"""Cross-file resolution of code-span mentions in Markdown documents.

``extract_markdown`` reports every inline code span that reads as a symbol
name (``Widget``, ``render()``, ``pkg.Widget``, ``src/mod.py::Widget::render``)
as a ``raw_calls`` entry tagged ``language: "markdown"`` instead of guessing a
target id: the symbol is defined in some other file, and only once every file
is extracted and the id-remap passes have run can the mention be matched to
the node that actually exists. Riding the ``raw_calls`` channel means those
passes rewrite the mention's ``caller_nid`` alongside every other raw call,
and the extraction cache round-trips it for free.

This resolver runs from the language-resolver registry (see
``resolver_registry``) with the merged corpus in hand and turns each mention
into a ``references`` edge from the heading (or page) that cites the symbol
to the code node that defines it:

* a path-qualified mention resolves against the cited file only. The file is
  matched by its path relative to the document, then as a segment-aligned
  suffix of a scanned file, and must be unique; the symbol chain is walked
  through ``contains`` / ``method`` edges so ``mod.py::Widget::render`` is
  the ``render`` owned by ``Widget``. The document names the file and the
  symbol verbatim, so the edge is EXTRACTED;
* a bare mention resolves only when exactly one callable or type in the
  corpus carries that label (case-sensitive; language built-ins excluded).
  Only nodes the extractors marked ``_callable`` qualify, for the reason the
  indirect-call pass gives (#1566): a by-name match must land on a real
  function, method or class, never on a same-named data symbol such as a
  JSON key. A dotted mention (``pkg.Widget``, ``Widget.render``) keeps its
  qualifiers, and a candidate survives only when they match an ordered suffix
  of its owner chain, or of its declared namespace or corpus-relative
  module/package path followed by its complete owner chain. ``time.sleep``
  never lands on a repo's own ``sleep``, and
  ``Widget.render`` picks the ``render`` that ``Widget`` owns. No other
  tie-breaking: an ambiguous name is a guess, and a guess is not an edge.
  A bare, parenthesis-less method name is held to one more test: a word like
  ``redis`` is prose as often as it is a reference, so a method match survives
  only when the citing document also names the class that owns it (#4609).
  The trailing ``()``, a dotted form, or a class/function label carries enough
  evidence on its own.
  The match is INFERRED (0.95, a named cross-file reference).

An explicit relative cite (``./`` or ``../``) resolves against the document's
directory only; it never falls through to the suffix rule, so a path that
escapes the corpus cannot land on an unrelated copy of the file elsewhere.

The shared cross-file call pass in ``extract`` skips ``markdown`` raw calls,
so a mention never surfaces as a ``calls`` edge.
"""
from __future__ import annotations

import os
import re
from typing import Any

from graphify.extractors.base import _LANGUAGE_BUILTIN_GLOBALS

MARKDOWN_MENTION_SUFFIXES = frozenset({".md", ".mdx", ".qmd", ".skill"})

_CONTAINMENT_RELATIONS = frozenset({"contains", "method"})

#: Leading ``./`` and ``../`` segments of a cited path, stripped as whole
#: segments (``lstrip("./")`` would eat the dot of ``.github/...``).
_RELATIVE_PREFIX_RE = re.compile(r"^(\.\.?/)+")


def _symbol_label(node: dict[str, Any]) -> str:
    """A node's label as a document would write it: no ``()``, no leading dot."""
    return str(node.get("label", "")).strip("()").lstrip(".")


def _is_file_node(node: dict[str, Any]) -> bool:
    label = str(node.get("label", ""))
    source_file = str(node.get("source_file", ""))
    return bool(source_file) and label == os.path.basename(source_file)


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _match_cited_file(cited: str, doc_file: str, source_files: set[str], *,
                      source_aliases: dict[str, str] | None = None) -> str | None:
    """The scanned source file a ``path::Name`` mention names, or None.

    Tries the path relative to the citing document first (``../src/mod.py``
    from ``docs/guide.md``), then an exact match, then a unique
    segment-aligned suffix (``src/mod.py`` naming ``/repo/src/mod.py``). A
    cite that is explicitly relative (``./x`` or ``../x``) stops after the
    first step: it names one location, and a suffix match elsewhere would be a
    different file. Admitted aliases map that location to canonical source
    identity; they do not change the citing document's selected directory.
    """
    cited_posix = _posix(cited)
    aliases = source_aliases or {}
    doc_dir = os.path.dirname(doc_file)
    relative = _posix(os.path.normpath(os.path.join(doc_dir, cited_posix)))
    relative = aliases.get(relative, relative)
    if relative in source_files:
        return relative
    if cited_posix.startswith(("./", "../")):
        return None
    exact = aliases.get(cited_posix, cited_posix)
    if exact in source_files:
        return exact
    stripped = _RELATIVE_PREFIX_RE.sub("", cited_posix)
    matches = {aliases.get(sf, sf) for sf in source_files | aliases.keys()
               if (sf == stripped or sf.endswith("/" + stripped))
               and aliases.get(sf, sf) in source_files}
    return next(iter(matches)) if len(matches) == 1 else None


def _evidence(node_id: str, nodes_by_id: dict[str, dict[str, Any]],
              parents: dict[str, set[str]], *, source_path: str) -> set[tuple[str, ...]]:
    """Coherent ordered qualifier sequences for ``node_id``.

    Keep each actual owner chain, declared namespace and admitted module or
    package path separate. Namespace and module/package prefixes retain the
    declaration's complete non-file owner chain; a member cannot masquerade as
    an unowned callable. Their unrelated segments cannot be spliced together,
    reordered or repeated to license a citation. A filename stem is omitted
    only when it is the exact recorded name of the outermost type.
    """
    evidence: set[tuple[str, ...]] = set()
    owner_chains: set[tuple[str, ...]] = set()
    filename_type_chains: set[tuple[str, ...]] = set()
    parts = source_path.split("/") if source_path else []
    module = (*parts[:-1], os.path.splitext(parts[-1])[0]) if parts else ()
    frontier: list[tuple[str, tuple[str, ...], frozenset[tuple[str, ...]], frozenset[str]]] = [
        (node_id, (), frozenset(), frozenset()),
    ]
    while frontier:
        nid, chain, namespaces, seen = frontier.pop()
        if nid in seen:
            continue
        seen = seen | {nid}
        declaration = nodes_by_id.get(nid, {})
        metadata = declaration.get("metadata")
        if (declaration.get("file_type") == "code" and declaration.get("_callable")
                and isinstance(metadata, dict)):
            namespace = metadata.get("namespace")
            if isinstance(namespace, str):
                segments = tuple(segment for segment in namespace.split(".") if segment)
                if segments:
                    namespaces = namespaces | {segments}
        owners: list[tuple[str, str]] = []
        for owner in parents.get(nid, ()):
            node = nodes_by_id.get(owner)
            if node is not None and not _is_file_node(node):
                label = _symbol_label(node)
                if label:
                    owners.append((owner, label))
        if not owners:
            owner_chains.add(chain)
            if (module and declaration.get("_callable_class")
                    and _symbol_label(declaration) == module[-1]):
                filename_type_chains.add(chain)
            if chain:
                evidence.add(chain)
            evidence.update(namespace + chain for namespace in namespaces)
        for owner, label in owners:
            frontier.append((owner, (label,) + chain, namespaces, seen))
    if module:
        evidence.update(module + chain for chain in owner_chains)
        # A same-named type can represent the filename in a citation. A free
        # function, or a differently named owner, cannot stand in for its module.
        if module[:-1]:
            evidence.update(module[:-1] + chain for chain in filename_type_chains)
    return evidence


def _markdown_raw_calls(per_file: list[dict]) -> list[dict]:
    calls: list[dict] = []
    for result in per_file:
        if not isinstance(result, dict):
            continue
        for rc in result.get("raw_calls", []) or []:
            if isinstance(rc, dict) and rc.get("language") == "markdown":
                calls.append(rc)
    return calls


def resolve_markdown_mentions(
    per_file: list[dict],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Turn ``language: "markdown"`` raw calls into ``references`` edges in place."""
    mentions = _markdown_raw_calls(per_file)
    if not mentions:
        return

    # Extraction admits both the selected input spelling and its canonical
    # identity. Use only those aliases: a result can also contain imported
    # foreign nodes, whose source files must not inherit the selected identity.
    source_aliases = {
        _posix(os.path.normpath(str(alias))):
        _posix(os.path.normpath(str(result["_markdown_source_file"])))
        for result in per_file
        if result.get("_markdown_source_file")
        for alias in (result.get("_markdown_source_alias"), result.get("_markdown_source_origin"))
        if alias
    }

    # Citation origin is the selected document location, even when its bytes
    # come from a symlink target. Canonical identities still match code sources.
    source_origins = {
        _posix(os.path.normpath(str(alias))):
        _posix(os.path.normpath(str(result.get("_markdown_source_origin") or alias)))
        for result in per_file
        for alias in (result.get("_markdown_source_alias"), result.get("_markdown_source_origin"))
        if alias
    }

    def _document_origin(value: object) -> str:
        origin = _posix(os.path.normpath(str(value or "")))
        return source_origins.get(origin, origin)

    def _source_identity(value: object) -> str:
        if not value:
            return ""
        source = _posix(os.path.normpath(str(value)))
        return source_aliases.get(source, source)

    # These paths come from the extraction's explicit logical root. Never infer
    # that root from a common prefix: a selected subset can live in one package.
    source_paths = {
        _source_identity(result["_markdown_source_file"]):
        _posix(os.path.normpath(str(result["_markdown_source_relative"])))
        for result in per_file
        if result.get("_markdown_source_file") and result.get("_markdown_source_relative")
    }

    def _qualifier_path(node: dict) -> str:
        source = _source_identity(node.get("source_file", ""))
        relative = source_paths.get(source, source)
        if (relative.startswith("/") or re.match(r"^[A-Za-z]:", relative)
                or relative == ".." or relative.startswith("../")):
            return ""
        return relative

    code_nodes = [
        n for n in all_nodes
        if n.get("id") and n.get("file_type") == "code" and not _is_file_node(n)
    ]
    # Other extractors can import the same declaration; count targets, not records.
    by_label: dict[str, set[str]] = {}
    by_file: dict[str, dict[str, set[str]]] = {}
    for n in code_nodes:
        label = _symbol_label(n)
        if not label:
            continue
        if n.get("_callable"):
            by_label.setdefault(label, set()).add(n["id"])
        source_file = _source_identity(n.get("source_file", ""))
        if source_file:
            by_file.setdefault(source_file, {}).setdefault(label, set()).add(n["id"])
    # Empty or failed selected files still participate in cited-file ambiguity.
    # An incomplete corpus cannot justify a unique inferred symbol match.
    # Explicit zero-node results cannot prove absence, even when extraction
    # accepts them. Metadata-only and raw-call fixtures have no nodes field.
    complete = not any(result.get("error") or result.get("_unsupported")
                       or result.get("nodes") == [] for result in per_file)
    for result in per_file:
        if source_file := result.get("_markdown_source_file"):
            by_file.setdefault(_source_identity(source_file), {})
    source_files = set(by_file)

    parents: dict[str, set[str]] = {}
    for e in all_edges:
        if e.get("relation") in _CONTAINMENT_RELATIONS:
            parents.setdefault(str(e.get("target")), set()).add(str(e.get("source")))
    nodes_by_id = {n["id"]: n for n in all_nodes if n.get("id")}
    node_ids = set(nodes_by_id)
    existing = {
        (e.get("source"), e.get("target"))
        for e in all_edges if e.get("relation") == "references"
    }
    mention_edges = {
        (e.get("source"), e.get("target")): e for e in all_edges
        if e.get("relation") == "references" and e.get("_markdown_mention") == 1
        and e.get("_origin") in (None, "ast")
    }

    def _resolve_chain(file_labels: dict[str, set[str]], names: list[str]) -> str | None:
        candidates = list(file_labels.get(names[-1], []))
        for depth, owner_name in enumerate(reversed(names[:-1]), start=1):
            owners = set(file_labels.get(owner_name, []))
            narrowed = []
            for c in candidates:
                chain = {c}
                for _ in range(depth):
                    chain = {p for nid in chain for p in parents.get(nid, ())}
                if chain & owners:
                    narrowed.append(c)
            candidates = narrowed
        return candidates[0] if len(candidates) == 1 else None

    def _target(rc: dict) -> tuple[str | None, str, float]:
        """The node a mention resolves to, plus the confidence of that match."""
        names = list(rc.get("qualifiers") or []) + [str(rc.get("callee", "")).strip()]
        cited = rc.get("path")
        if cited:
            source_file = _match_cited_file(
                str(cited), _document_origin(rc.get("source_file", "")), source_files,
                source_aliases=source_aliases,
            )
            if source_file is None:
                return None, "EXTRACTED", 1.0
            return _resolve_chain(by_file[source_file], names), "EXTRACTED", 1.0
        callee = names[-1]
        if not complete:
            return None, "INFERRED", 0.95
        if len(names) == 1 and callee in _LANGUAGE_BUILTIN_GLOBALS:
            return None, "INFERRED", 0.95
        qualifiers = tuple(names[:-1])
        candidates = [
            c for c in by_label.get(callee, [])
            if not qualifiers or any(sequence[-len(qualifiers):] == qualifiers
                                     for sequence in _evidence(
                                         c, nodes_by_id, parents,
                                         source_path=_qualifier_path(nodes_by_id[c])))
        ]
        return (candidates[0] if len(candidates) == 1 else None), "INFERRED", 0.95

    def _method_owner(node_id: str) -> str | None:
        """The class that owns *node_id*, when it is a method and not a function."""
        for parent in parents.get(node_id, ()):
            node = nodes_by_id.get(parent)
            if node is not None and node.get("_callable_class"):
                return parent
        return None

    #: Targets already cited by each document, used as evidence in pass 2b.
    cited_targets: dict[str, set[str]] = {}

    def _add(rc: dict, target: str | None, confidence: str, score: float) -> None:
        caller = str(rc.get("caller_nid", ""))
        if target is None or target == caller:
            return
        doc = _document_origin(rc.get("source_file", ""))
        provenance = {
            "context": rc.get("context", ""), "confidence": confidence,
            "confidence_score": score, "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
        }
        if (caller, target) in existing:
            previous = mention_edges.get((caller, target))
            if (previous is not None and confidence == "EXTRACTED"
                    and previous.get("confidence") == "INFERRED"):
                previous.update(provenance)
            # The edge is already in the graph, but this document still CITES that
            # target, so it stays on record as owner evidence for a later method
            # mention in the same document (#4609 review finding).
            cited_targets.setdefault(doc, set()).add(target)
            return
        existing.add((caller, target))
        edge = {
            "source": caller,
            "target": target,
            "relation": "references",
            "_markdown_mention": 1,
            **provenance,
            "weight": 1.0,
        }
        all_edges.append(edge)
        mention_edges[(caller, target)] = edge
        cited_targets.setdefault(doc, set()).add(target)

    # Pass 1: explicit evidence resolves on its own — a cited path, a dotted
    # qualifier chain, or a trailing (). Record those targets per document.
    # Pass 2 handles the bare, parenthesis-less, single-name spans, where the
    # word alone (`redis`, `data`, `message`) is indistinguishable from prose.
    bare: list[dict] = []
    for rc in mentions:
        caller = str(rc.get("caller_nid", ""))
        callee = str(rc.get("callee", "")).strip()
        if not caller or not callee or caller not in node_ids:
            continue
        names = list(rc.get("qualifiers") or []) + [callee]
        span = str(rc.get("context") or "")
        if rc.get("path") or len(names) > 1 or "(" in span:
            _add(rc, *_target(rc))
        else:
            bare.append(rc)

    # Pass 2a: bare mentions that do not name a method. A class or free function
    # label is specific enough to keep matching, and the class a later method
    # mention needs must be on record before pass 2b runs.
    method_mentions: list[tuple[dict, str, str, float]] = []
    for rc in bare:
        target, confidence, score = _target(rc)
        if target is not None and _method_owner(target) is not None:
            method_mentions.append((rc, target, confidence, score))
        else:
            _add(rc, target, confidence, score)

    # Pass 2b: a bare method name (#4609) is only evidence when the same
    # document already cites the class that owns it. `redis` the server does not
    # link to `Cache.redis` unless the document names `Cache`;
    # `successWithWarnings` survives because `HandlerResult` is cited next to it.
    for rc, target, confidence, score in method_mentions:
        doc = _document_origin(rc.get("source_file", ""))
        if _method_owner(target) in cited_targets.get(doc, ()):
            _add(rc, target, confidence, score)
