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

_MD_MENTION_FENCE_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
_MD_MENTION_LIST_RE = re.compile(r'^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])([ \t]+)')
_MD_MENTION_QUOTE_RE = re.compile(r'^ {0,3}>[ \t]?')
_MD_MENTION_BREAK_RE = re.compile(
    r'^ {0,3}(?:#{1,6}(?:[ \t]|$)|>|<!--|'
    r'(?:[-*_][ \t]*){3,}$|(?:=+|-+)[ \t]*$|\[[^\]]+\]:)'
)
_MD_MENTION_RAW_HTML_START_RE = re.compile(r'^<(?:pre|script|style|textarea)(?:[ \t>]|$)', re.I)
_MD_MENTION_RAW_HTML_END_RE = re.compile(r'</(?:pre|script|style|textarea)>', re.I)
_MD_MENTION_BLOCK_HTML_RE = re.compile(
    r'^</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|'
    r'colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|'
    r'frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|'
    r'menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|'
    r'tbody|td|tfoot|th|thead|title|tr|track|ul)(?:[ \t>]|/?>|$)', re.I,
)
_MD_MENTION_HTML_ATTRIBUTE = (
    r'''[A-Za-z_:][A-Za-z0-9_.:\-]*(?:[ \t]*=[ \t]*(?:[^ \t"'=<>`]+|'[^']*'|"[^"]*"))?'''
)
_MD_MENTION_HTML_TAG_RE = re.compile(
    r'^(?:</([A-Za-z][A-Za-z0-9\-]*)[ \t]*>|<([A-Za-z][A-Za-z0-9\-]*)'
    r'(?:[ \t]+' + _MD_MENTION_HTML_ATTRIBUTE + r')*[ \t]*/?>)[ \t]*$'
)
_MD_MENTION_HTML_BLANK_END_RE = re.compile(r'^[ \t]*$')
_MD_MENTION_SYMBOL = r'[A-Za-z_$][\w$]*[!?]?'
_MD_PATH_MENTION_RE = re.compile(
    r'^([A-Za-z0-9_./\-]+\.[A-Za-z0-9]+)::('
    + _MD_MENTION_SYMBOL + r'(?:::' + _MD_MENTION_SYMBOL + r')*)(?:\(\))?$'
)
_MD_BARE_MENTION_RE = re.compile(
    r'^(?:' + _MD_MENTION_SYMBOL + r'\.)*(' + _MD_MENTION_SYMBOL + r')(?:\(\))?$'
)


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


def _mention_container_content(line: str, previous: tuple[tuple[str, int], ...],
                               *, extend: bool = True
                               ) -> tuple[str, tuple[tuple[str, int], ...], bool]:
    """Remove continuing quote/list prefixes, then admit new nested containers."""
    content = line.expandtabs(4)
    containers: list[tuple[str, int]] = []
    for kind, width in previous:
        if kind == 'quote':
            marker = _MD_MENTION_QUOTE_RE.match(content)
            if marker is None:
                break
            content = content[marker.end():]
        elif not content.strip():
            content = ''  # Blank lines may continue a list without indentation.
        elif content.startswith(' ' * width):
            content = content[width:]
        else:
            break
        containers.append((kind, width))
    added = False
    while extend:
        quote = _MD_MENTION_QUOTE_RE.match(content)
        marker = quote or _MD_MENTION_LIST_RE.match(content)
        if marker is None:
            break
        width = marker.end()
        # Five or more spaces after a list marker use one separator space;
        # the remainder belongs to the content (possibly indented code).
        if quote is None and len(marker.group(1)) > 4:
            width = marker.start(1) + 1
        containers.append(('quote', 0) if quote else ('list', width))
        content = content[width:]
        added = True
    return content, tuple(containers), added


def _mention_html_block_end(line: str, paragraph: bool) -> re.Pattern[str] | None:
    """Identify raw HTML leaf blocks; inline HTML remains ordinary prose."""
    if len(line) - len(line.lstrip(' ')) > 3:
        return None
    text = line.lstrip(' ')
    if _MD_MENTION_RAW_HTML_START_RE.match(text):
        return _MD_MENTION_RAW_HTML_END_RE
    if text.startswith('<?'):
        return re.compile(r'\?>')
    if text.startswith('<![CDATA['):
        return re.compile(r'\]\]>')
    if re.match(r'<![A-Za-z]', text):
        return re.compile('>')
    if _MD_MENTION_BLOCK_HTML_RE.match(text):
        return _MD_MENTION_HTML_BLANK_END_RE
    if not paragraph and (tag := _MD_MENTION_HTML_TAG_RE.fullmatch(text)):
        if tag.group(1) or tag.group(2).lower() not in {'pre', 'script', 'style', 'textarea'}:
            return _MD_MENTION_HTML_BLANK_END_RE
    return None


def _mention_span_end(lines: list[str], index: int, start: int, width: int,
                      multiline: bool, first_line: str,
                      containers: tuple[tuple[str, int], ...]) -> tuple[int, int] | None:
    """Find an equal-length closing run without crossing a prose block."""
    for end_index in range(index, len(lines)):
        line = first_line
        if end_index != index:
            line, continued, _ = _mention_container_content(
                lines[end_index], containers, extend=False,
            )
            if (not multiline or not line.strip() or _MD_MENTION_FENCE_RE.match(line)
                    or continued != containers or _MD_MENTION_LIST_RE.match(line)
                    or _MD_MENTION_BREAK_RE.match(line)
                    or _mention_html_block_end(line, paragraph=True) is not None):
                return None
            start = 0
        for run in re.finditer(r'`+', line[start:]):
            if len(run.group()) == width:
                return end_index, start + run.start()
    return None


def _markdown_mention_spans(lines: list[str], heading_owners: dict[int, str],
                           file_nid: str):
    """Scan new citations independently of the legacy heading/link output.

    Block state takes precedence over inline syntax. Within prose, a valid
    code span takes precedence over comment delimiters in its literal content.
    """
    index = _mention_body_start(lines)
    containers: tuple[tuple[str, int], ...] = ()
    fence: tuple[str, int, tuple[tuple[str, int], ...]] | None = None
    html_block: tuple[re.Pattern[str], tuple[tuple[str, int], ...]] | None = None
    in_comment = False
    paragraph = False
    owner = file_nid
    while index < len(lines):
        raw_line = lines[index]
        if fence is not None:
            line, continued, _ = _mention_container_content(raw_line, fence[2], extend=False)
            if continued == fence[2]:
                fence_match = _MD_MENTION_FENCE_RE.match(line)
                if (fence_match and fence_match.group(1)[0] == fence[0]
                        and len(fence_match.group(1)) >= fence[1]
                        and not fence_match.group(2).strip()):
                    fence = None
                index += 1
                continue
            # An unclosed fence ends with its containing quote or list item.
            fence = None
            paragraph = False
        if html_block is not None:
            line, continued, _ = _mention_container_content(raw_line, html_block[1], extend=False)
            if continued == html_block[1]:
                if html_block[0].search(line):
                    html_block = None
                index += 1
                continue
            html_block = None
            paragraph = False

        line, current_containers, added = _mention_container_content(raw_line, containers)
        if current_containers != containers or added:
            paragraph = False
        containers = current_containers
        fence_match = _MD_MENTION_FENCE_RE.match(line)

        block_line = False
        if not in_comment:
            if not line.strip():
                paragraph = False
                index += 1
                continue
            indent = len(line) - len(line.lstrip(' '))
            # An indented line cannot interrupt a paragraph. List content has
            # its own indentation baseline, including after a blank line.
            if indent >= 4 and not paragraph:
                index += 1
                continue
            if (fence_match and (fence_match.group(1)[0] != '`'
                                 or '`' not in fence_match.group(2))):
                fence = (fence_match.group(1)[0], len(fence_match.group(1)), containers)
                paragraph = False
                index += 1
                continue
            if html_end := _mention_html_block_end(line, paragraph):
                if not html_end.search(line):
                    html_block = (html_end, containers)
                paragraph = False
                index += 1
                continue
            owner = heading_owners.get(index, owner)
            block_line = bool(_MD_MENTION_BREAK_RE.match(line))
            paragraph = not block_line

        position = 0
        while position < len(line):
            if in_comment:
                end = line.find('-->', position)
                if end < 0:
                    break
                in_comment = False
                position = end + 3
                continue
            token = re.search(r'<!--|`+', line[position:])
            if token is None:
                if line[position:].strip() and not block_line:
                    paragraph = True
                break
            start = position + token.start()
            if token.group() == '<!--':
                in_comment = True
                position = start + 4
                continue
            width = len(token.group())
            end = _mention_span_end(lines, index, start + width, width,
                                    not block_line, line, containers)
            if end is None:
                position = start + width
                continue
            end_index, end_position = end
            continued_lines = [_mention_container_content(text, containers, extend=False)[0]
                               for text in lines[index + 1:end_index + 1]]
            parts = [line[start + width:end_position]] if end_index == index else [
                line[start + width:], *continued_lines[:-1],
                continued_lines[-1][:end_position],
            ]
            # CommonMark normalizes soft line endings in code spans to spaces.
            yield owner, ' '.join(parts), index + 1
            if end_index != index:
                line = _mention_container_content(lines[end_index], containers, extend=False)[0]
            index = end_index
            position = end_position + width
        if block_line:
            paragraph = False
        index += 1

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

    for owner, span, line in _markdown_mention_spans(lines, heading_owners, file_nid):
        add_mention(owner, span, line)

    return {"nodes": nodes, "edges": edges, "raw_calls": raw_calls,
            "input_tokens": 0, "output_tokens": 0}
