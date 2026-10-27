"""Markdown extractor. Moved verbatim from graphify/extract.py."""
from __future__ import annotations
from graphify.source_io import source_read_text

import re
import os

from pathlib import Path
from graphify.extractors.base import _file_stem, _make_id


_MD_INLINE_LINK_RE = re.compile(r'(?<!\!)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[^)]*)?\)')

_MD_REF_DEF_RE = re.compile(r'^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?')

_MD_WIKILINK_RE = re.compile(r'(?<!\!)\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]')

_MD_LINKABLE_EXTS = {".md", ".mdx", ".qmd", ".markdown", ".rst", ".txt"}

_MD_CODE_SPAN_RE = re.compile(r'(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)')
_MD_PATH_MENTION_RE = re.compile(
    r'^([A-Za-z0-9_./\-]+\.[A-Za-z0-9]+)::([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)(?:\(\))?$'
)
_MD_BARE_MENTION_RE = re.compile(r'^(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)(?:\(\))?$')


def _code_span_mention(span: str) -> "tuple[str | None, list[str]] | None":
    """Classify a symbol citation without guessing a defining file or node."""
    from graphify.detect import CODE_EXTENSIONS, DOC_EXTENSIONS

    text = span.strip()
    if not text or " " in text:
        return None
    match = _MD_PATH_MENTION_RE.fullmatch(text)
    if match:
        return match.group(1), match.group(2).split("::")
    match = _MD_BARE_MENTION_RE.fullmatch(text)
    if not match:
        return None
    if "." in text:
        suffix = "." + match.group(1)
        if suffix in CODE_EXTENSIONS or suffix in DOC_EXTENSIONS:
            return None
        return None, text.removesuffix("()").split(".")
    return None, [match.group(1)]


def _mention_body_start(lines: list[str]) -> int:
    """Exclude bounded leading frontmatter from new mention scanning only."""
    if not lines or lines[0].strip() != "---":
        return 0
    for index, line in enumerate(lines[1:201], 1):
        if line.strip() in {"---", "..."}:
            return index + 1
    return 0

def _resolve_markdown_link(raw: str, source_dir: Path) -> "Path | None":
    """Resolve a markdown link target to the absolute path of a sibling document.

    Returns the resolved (normalized, not necessarily existing) path when the
    target is a *local* relative/absolute file-path link to a document, or None
    when it should be skipped: external URLs (http/https/mailto/protocol-
    relative/data), pure in-page anchors (``#section``), and links to non-doc
    file types (code/assets are handled by their own extractors).

    The anchor fragment (``#section``) and query (``?x=1``) are stripped before
    resolution so ``./repo.md#setup`` resolves to the same node as ``./repo.md``.
    Extension-less targets (typical of wikilinks) are treated as sibling ``.md``.
    """
    target = raw.strip()
    if not target:
        return None
    # Drop anchor / query so #section links still resolve to the target doc.
    target = target.split("#", 1)[0].split("?", 1)[0].strip()
    if not target:
        return None
    low = target.lower()
    if "://" in target or low.startswith(("mailto:", "tel:", "//", "data:")):
        return None
    suffix = Path(target).suffix.lower()
    if suffix == "":
        target = target + ".md"
        suffix = ".md"
    if suffix not in _MD_LINKABLE_EXTS:
        return None
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = source_dir / candidate
    return Path(os.path.normpath(str(candidate)))

def extract_markdown(path: Path) -> dict:
    """Extract structural nodes and edges from a Markdown file.

    Produces nodes for:
    - The file itself
    - Each heading (# / ## / ### etc.)

    Produces edges for:
    - file --contains--> heading
    - parent heading --contains--> child heading (nesting by level)
    - heading --references--> other node (when backtick `Name` matches a known pattern)
    - file --references--> linked document, for inline ``[text](./other.md)``,
      reference-style ``[label]: ./other.md`` and ``[[wikilink]]`` links, so a
      hub doc (``index.md`` / ``table-of-contents.md``) becomes a real hub node
      instead of an under-connected orphan (#1376). The target node ID is built
      from the resolved target path with the same recipe as the target file's
      own node, so the edge merges into that node (no ghost node). External
      URLs, in-page anchors, images and non-document targets are skipped.

    Fenced code blocks (``` ... ```) are skipped during parsing so their
    contents don't get treated as headings, but no node is emitted for
    them — they were always orphans (only a single contains edge to the
    parent doc) and inflated the disconnected-component count (#1077).

    No tree-sitter dependency — pure line-by-line parsing.
    """
    try:
        source = source_read_text(path, encoding="utf-8", errors="replace")
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    raw_calls: list[dict] = []
    seen_ids: set[str] = set()
    seen_mentions: set[tuple[str, str | None, tuple[str, ...], bool]] = set()

    def add_mention(caller: str, span: str, line: int) -> None:
        mention = _code_span_mention(span)
        if mention is None:
            return
        cited_path, names = mention
        # An explicit () is stronger evidence than a preceding bare word.
        key = (caller, cited_path, tuple(names), "(" in span)
        if key in seen_mentions:
            return
        seen_mentions.add(key)
        raw_call = {
            "caller_nid": caller, "callee": names[-1], "is_member_call": False,
            "language": "markdown", "context": span.strip(), "source_file": str_path,
            "source_location": f"L{line}",
        }
        if cited_path is not None:
            raw_call["path"] = cited_path
        if len(names) > 1:
            raw_call["qualifiers"] = names[:-1]
        raw_calls.append(raw_call)

    def add_node(nid: str, label: str, line: int, file_type: str = "document") -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": file_type,
                          "source_file": str_path, "source_location": f"L{line}"})

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0) -> None:
        edges.append({"source": src, "target": tgt, "relation": relation,
                      "confidence": confidence, "source_file": str_path,
                      "source_location": f"L{line}", "weight": weight})

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    source_dir = path.parent
    # Dedup link edges by resolved target node so a hub doc that links to the
    # same sibling many times yields one edge, not N (keeps weights meaningful).
    linked_targets: set[str] = set()

    def add_link(raw: str, line: int) -> None:
        resolved = _resolve_markdown_link(raw, source_dir)
        if resolved is None:
            return
        # Build the target ID with the SAME recipe as the target file's own
        # node (_make_id(str(path)) at extract time, canonicalized to
        # _file_node_id(rel) by the extract() post-pass). Using the absolute
        # resolved path means both endpoints get remapped identically, so the
        # edge merges into the existing doc node instead of spawning a ghost.
        tgt_nid = _make_id(str(resolved))
        if tgt_nid == file_nid or tgt_nid in linked_targets:
            return
        linked_targets.add(tgt_nid)
        add_edge(file_nid, tgt_nid, "references", line)

    # Track heading stack for nesting: [(level, nid), ...]
    heading_stack: list[tuple[int, str]] = []
    heading_owners: dict[int, str] = {}
    in_code_block = False

    lines = source.splitlines()
    for line_num_0, line_text in enumerate(lines):
        line_num = line_num_0 + 1

        # Skip over fenced code blocks so their contents are not parsed as
        # headings, but do not emit nodes/edges for them (#1077).
        stripped = line_text.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        # Markdown links -> document references (#1376). Scanned on every
        # non-fenced line (including heading lines, which the heading branch
        # below `continue`s past) so links anywhere in the doc are captured.
        for m in _MD_INLINE_LINK_RE.finditer(line_text):
            add_link(m.group(1), line_num)
        for m in _MD_WIKILINK_RE.finditer(line_text):
            add_link(m.group(1), line_num)
        ref_def = _MD_REF_DEF_RE.match(line_text)
        if ref_def:
            add_link(ref_def.group(1), line_num)

        # Detect headings: # Heading, ## Heading, etc.
        heading_match = re.match(r'^(#{1,6})\s+(.+)', line_text)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            h_nid = _make_id(stem, title)
            # Avoid duplicate heading IDs by appending line number
            if h_nid in seen_ids:
                h_nid = _make_id(stem, title, str(line_num))
            add_node(h_nid, title, line_num)

            # Pop headings at same or deeper level
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()

            # Connect to parent heading or file
            parent = heading_stack[-1][1] if heading_stack else file_nid
            add_edge(parent, h_nid, "contains", line_num)

            heading_stack.append((level, h_nid))
            heading_owners[line_num_0] = h_nid

    # New mentions use full fence delimiters independently of v8's heading and
    # document-link scan. A shorter or different fence cannot close a block.
    mention_body_start = _mention_body_start(lines)
    mention_fence: tuple[str, int] | None = None
    owner = file_nid
    for index, line_text in enumerate(lines):
        if index < mention_body_start:
            continue
        fence = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line_text)
        if mention_fence is not None:
            if (fence and fence.group(1)[0] == mention_fence[0]
                    and len(fence.group(1)) >= mention_fence[1]
                    and not fence.group(2).strip()):
                mention_fence = None
            continue
        if fence and (fence.group(1)[0] != '`' or '`' not in fence.group(2)):
            mention_fence = (fence.group(1)[0], len(fence.group(1)))
            continue
        owner = heading_owners.get(index, owner)
        for match in _MD_CODE_SPAN_RE.finditer(line_text):
            add_mention(owner, match.group(2), index + 1)

    return {"nodes": nodes, "edges": edges, "raw_calls": raw_calls,
            "input_tokens": 0, "output_tokens": 0}
