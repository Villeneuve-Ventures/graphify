"""Markdown extractor. Moved verbatim from graphify/extract.py."""
from __future__ import annotations
from graphify.source_io import source_read_text

import re
import os
import string

from bisect import bisect_left, bisect_right
from pathlib import Path
from graphify.extractors.base import _file_stem, _make_id


_MD_INLINE_LINK_RE = re.compile(r'(?<!\!)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[^)]*)?\)')

_MD_REF_DEF_RE = re.compile(r'^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?')

_MD_WIKILINK_RE = re.compile(r'(?<!\!)\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]')

_MD_LINKABLE_EXTS = {".md", ".mdx", ".qmd", ".markdown", ".rst", ".txt"}

_MD_MENTION_FENCE_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
_MD_MENTION_LIST_RE = re.compile(r'^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])([ \t]+)')
_MD_MENTION_QUOTE_RE = re.compile(r'^ {0,3}>[ \t]?')
_MD_MENTION_HEADING_RE = re.compile(r'^ {0,3}#{1,6}(?:[ \t]|$)')
_MD_MENTION_BREAK_RE = re.compile(
    r'^ {0,3}(?:#{1,6}(?:[ \t]|$)|>|<!--|'
    r'(?:[-*_][ \t]*){3,}$|(?:=+|-+)[ \t]*$)'
)
_MD_MENTION_REFERENCE_RE = re.compile(r' {0,3}\[((?:\\.|[^\[\]\\]){1,999})\]:')
_MD_MENTION_LABEL_RE = re.compile(r'\[((?:\\.|[^\[\]\\]){0,999})\]')
_MD_MENTION_WHITESPACE_RE = re.compile(r'[ \t]*(?:\n[ \t]*)?')
_MD_MENTION_TOKEN_RE = re.compile(r'\\|<!--|<|!?\[|\]|`+')
_MD_MENTION_TICK_RE = re.compile(r'`+')
_MD_MENTION_AUTOLINK_RE = re.compile(
    r'<(?:[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^\x00-\x20\x7f<>]*|'
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@"
    r'[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?'
    r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*)>'
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
    r'''[A-Za-z_:][A-Za-z0-9_.:\-]*(?:[ \t\n]*=[ \t\n]*(?:[^ \t\n"'=<>`]+|'[^']*'|"[^"]*"))?'''
)
_MD_MENTION_HTML_TAG = (
    r'(?:</([A-Za-z][A-Za-z0-9\-]*)[ \t\n]*>|<([A-Za-z][A-Za-z0-9\-]*)'
    r'(?:[ \t\n]+' + _MD_MENTION_HTML_ATTRIBUTE + r')*[ \t\n]*/?>)'
)
_MD_MENTION_HTML_TAG_RE = re.compile('^' + _MD_MENTION_HTML_TAG + r'[ \t]*$')
_MD_MENTION_INLINE_HTML_TAG_RE = re.compile(_MD_MENTION_HTML_TAG)
_MD_MENTION_HTML_BLANK_END_RE = re.compile(r'^[ \t]*$')
_MD_MENTION_SYMBOL = r'[A-Za-z_$][\w$]*[!?]?'
_MD_PATH_MENTION_RE = re.compile(
    r'^((?:[A-Za-z]:)?[A-Za-z0-9_./\\\-]+\.[A-Za-z0-9]+)::('
    + _MD_MENTION_SYMBOL + r'(?:::' + _MD_MENTION_SYMBOL + r')*)(?:\(\))?$'
)
_MD_BARE_MENTION_RE = re.compile(
    r'^(?:' + _MD_MENTION_SYMBOL + r'\.)*(' + _MD_MENTION_SYMBOL + r')(?:\(\))?$'
)


def _code_span_mention(span: str) -> "tuple[str | None, list[str]] | None":
    """Classify a symbol citation without guessing a defining file or node."""
    from graphify.detect import CODE_EXTENSIONS, DOC_EXTENSIONS

    text = span
    if text.startswith(' ') and text.endswith(' ') and text.strip(' '):
        text = text[1:-1]
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
    if not lines or lines[0].rstrip(' \t\r\n') != "---":
        return 0
    for index, line in enumerate(lines[1:201], 1):
        if line.rstrip(' \t\r\n') in {"---", "..."}:
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
        for run in _MD_MENTION_TICK_RE.finditer(line, start):
            if len(run.group()) == width:
                return end_index, run.start()
    return None


def _mention_html_tag_end(lines: list[str], index: int, start: int,
                          multiline: bool, first_line: str,
                          containers: tuple[tuple[str, int], ...]) -> tuple[int, int] | None:
    """Find a complete raw tag without treating quoted attribute text as prose."""
    prefix = first_line[:start]
    if (len(prefix) - len(prefix.rstrip('\\'))) % 2:
        return None
    if not re.match(r'</?[A-Za-z][A-Za-z0-9\-]*(?=[ \t/>]|$)', first_line[start:]):
        return None
    parts: list[str] = []
    quote = None
    for end_index in range(index, len(lines)):
        line = first_line[start:] if end_index == index else ''
        if end_index != index:
            line, continued, _ = _mention_container_content(
                lines[end_index], containers, extend=False,
            )
            if (not multiline or not line.strip() or continued != containers
                    or _MD_MENTION_FENCE_RE.match(line) or _MD_MENTION_LIST_RE.match(line)
                    or _MD_MENTION_BREAK_RE.match(line)
                    or _mention_html_block_end(line, paragraph=True) is not None):
                return None
        for position, character in enumerate(line):
            if quote is not None:
                if character == quote:
                    quote = None
            elif character in {'"', "'"}:
                quote = character
            elif character == '>':
                candidate = '\n'.join([*parts, line[:position + 1]])
                if _MD_MENTION_INLINE_HTML_TAG_RE.fullmatch(candidate):
                    return end_index, position + 1 + (start if end_index == index else 0)
                return None
        parts.append(line)
    return None


def _mention_prose_text(lines: list[str], index: int, first_line: str,
                        containers: tuple[tuple[str, int], ...], *, multiline: bool = True) -> str:
    """Logical lines available to one inline construct, bounded by its prose block."""
    parts = [first_line]
    if multiline:
        for end_index in range(index + 1, len(lines)):
            line, continued, _ = _mention_container_content(
                lines[end_index], containers, extend=False,
            )
            if (not line.strip() or continued != containers
                    or _MD_MENTION_FENCE_RE.match(line) or _MD_MENTION_LIST_RE.match(line)
                    or _MD_MENTION_BREAK_RE.match(line)
                    or _mention_html_block_end(line, paragraph=True) is not None):
                break
            parts.append(line)
    return '\n'.join(parts)


class _MentionProse:
    """Reuse one normalized prose block and map offsets without copying its tail."""

    def __init__(self, lines: list[str], index: int, first_line: str,
                 containers: tuple[tuple[str, int], ...], *, multiline: bool):
        self.index = index
        self.containers = containers
        self.text = _mention_prose_text(lines, index, first_line, containers,
                                        multiline=multiline)
        self.line_starts = [0, *(match.end() for match in re.finditer('\n', self.text))]
        self.last_comment_end = self.text.rfind('-->')
        # Bare destinations stop at ASCII whitespace/control characters. Pair
        # their unescaped parentheses once, so repeated malformed openers do
        # not each scan the remaining paragraph to discover an unmatched pair.
        self.parenthesis_ends: dict[int, int] = {}
        openings: list[int] = []
        position = 0
        while position < len(self.text):
            character = self.text[position]
            if (character == '\\' and position + 1 < len(self.text)
                    and self.text[position + 1] in string.punctuation):
                position += 2
                continue
            if character == ' ' or ord(character) < 32 or ord(character) == 127:
                openings.clear()
            elif character == '(':
                openings.append(position)
            elif character == ')' and openings:
                self.parenthesis_ends[openings.pop()] = position
            position += 1

    def contains(self, index: int, containers: tuple[tuple[str, int], ...],
                 multiline: bool) -> bool:
        return (self.index <= index < self.index + len(self.line_starts)
                and self.containers == containers
                and (multiline or len(self.line_starts) == 1))

    def offset(self, index: int, position: int = 0) -> int:
        return self.line_starts[index - self.index] + position

    def position(self, offset: int) -> tuple[int, int]:
        relative_index = bisect_right(self.line_starts, offset) - 1
        return self.index + relative_index, offset - self.line_starts[relative_index]


class _MentionSourceLine:
    """Recover literal slices after container parsing expanded source tabs."""

    def __init__(self, source: str):
        self.source = source
        self.columns = [0]
        for character in source:
            column = self.columns[-1]
            self.columns.append(column + (4 - column % 4 if character == '\t' else 1))

    def literal_slice(self, logical_line: str, start: int, end: int) -> str:
        if start == end:
            return ''
        content_column = self.columns[-1] - len(logical_line)
        start += content_column
        end += content_column
        left = bisect_right(self.columns, start) - 1
        right = bisect_left(self.columns, end)
        prefix = suffix = ''
        # A container prefix may consume only part of a tab. Keep those
        # structural remainder spaces; only wholly interior tabs are literal.
        if self.columns[left] < start:
            prefix = ' ' * (self.columns[left + 1] - start)
            left += 1
        if self.columns[right] > end:
            right -= 1
            suffix = ' ' * (end - self.columns[right])
        if left > right:
            return ' ' * (end - start)
        return prefix + self.source[left:right] + suffix


def _mention_whitespace_end(text: str, start: int) -> int:
    match = _MD_MENTION_WHITESPACE_RE.match(text, start)
    return match.end() if match is not None else start


def _mention_destination_end(text: str, start: int,
                             parenthesis_ends: dict[int, int]) -> int | None:
    """Parse a link destination without interpreting its contents as Markdown."""
    position = start
    angle = text.startswith('<', position)
    if angle:
        position += 1
    while position < len(text):
        character = text[position]
        if (character == '\\' and position + 1 < len(text)
                and text[position + 1] in string.punctuation):
            position += 2
            continue
        if angle:
            if character == '>':
                return position + 1
            if character in {'<', '\n'}:
                return None
        else:
            if character == ' ' or ord(character) < 32 or ord(character) == 127:
                break
            if character == '(':
                closing = parenthesis_ends.get(position)
                if closing is None:
                    return None
                position = closing + 1
                continue
            elif character == ')':
                break
        position += 1
    return position if not angle and position > start else None


def _mention_title_end(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] not in {'"', "'", '('}:
        return None
    closing = ')' if text[start] == '(' else text[start]
    position = start + 1
    while position < len(text):
        character = text[position]
        if (character == '\\' and position + 1 < len(text)
                and text[position + 1] in string.punctuation):
            position += 2
            continue
        if character == closing:
            return position + 1
        if text[start] == '(' and character == '(':
            return None
        position += 1
    return None


def _mention_inline_destination_end(text: str, start: int,
                                    parenthesis_ends: dict[int, int]) -> int | None:
    position = _mention_whitespace_end(text, start)
    if position < len(text) and text[position] == ')':
        return position + 1
    end = _mention_destination_end(text, position, parenthesis_ends)
    if end is not None:
        after = _mention_whitespace_end(text, end)
        if after > end:
            title = _mention_title_end(text, after)
            if title is not None:
                after = _mention_whitespace_end(text, title)
        if after < len(text) and text[after] == ')':
            return after + 1
    return None


def _mention_reference_end(text: str, parenthesis_ends: dict[int, int],
                           start: int = 0) -> int | None:
    reference = _MD_MENTION_REFERENCE_RE.match(text, start)
    if reference is None or not reference.group(1).strip():
        return None
    start = _mention_whitespace_end(text, reference.end())
    end = _mention_destination_end(text, start, parenthesis_ends)
    if end is None:
        return None
    same_line = end
    while same_line < len(text) and text[same_line] in ' \t':
        same_line += 1
    if same_line == len(text):
        return same_line
    title_start = _mention_whitespace_end(text, end)
    if title_start > end:
        title = _mention_title_end(text, title_start)
        if title is not None:
            after = title
            while after < len(text) and text[after] in ' \t':
                after += 1
            if after == len(text) or text[after] == '\n':
                return after
    # A failed title on the next line belongs to the following prose. A
    # trailing non-title on the destination's own line invalidates the definition.
    return same_line if text[same_line] == '\n' else None


def _markdown_mention_spans(lines: list[str], heading_owners: dict[int, str],
                           file_nid: str):
    reference_labels: set[str] = set()
    prose_contexts: dict[tuple, _MentionProse] = {}
    source_lines: dict[int, _MentionSourceLine | None] = {}
    # Forward reference labels need actual block-valid definitions. Reuse the
    # same walk and normalized buffers rather than scraping definition-shaped
    # text from fenced code, HTML, comments or an existing paragraph.
    if any('][' in line for line in lines):
        for _ in _scan_markdown_mentions(lines, heading_owners, file_nid,
                                          reference_labels, prose_contexts, source_lines,
                                          collect_definitions=True):
            pass
    yield from _scan_markdown_mentions(lines, heading_owners, file_nid,
                                       reference_labels, prose_contexts, source_lines)


def _mention_label_key(label: str) -> str:
    return re.sub(r'[ \t\n]+', ' ', label.strip(' \t\n')).casefold()


def _scan_markdown_mentions(lines: list[str], heading_owners: dict[int, str],
                            file_nid: str, reference_labels: set[str],
                            prose_contexts: dict[tuple, _MentionProse],
                            source_lines: dict[int, _MentionSourceLine | None], *,
                            collect_definitions: bool = False):
    """Scan new citations independently of the legacy heading/link output.

    Block state takes precedence over inline syntax. Inline tokens are consumed
    from left to right; code-span and raw-tag contents remain literal.
    """
    index = _mention_body_start(lines)
    containers: tuple[tuple[str, int], ...] = ()
    fence: tuple[str, int, tuple[tuple[str, int], ...]] | None = None
    html_block: tuple[re.Pattern[str], tuple[tuple[str, int], ...]] | None = None
    in_comment = False
    paragraph = False
    brackets: list[tuple[bool, bool, int, int]] = []  # (image, active, line, column)
    owner: str | None = file_nid
    prose: _MentionProse | None = None

    def prose_context(*, multiline: bool = True) -> _MentionProse:
        nonlocal prose
        if prose is None or not prose.contains(index, containers, multiline):
            key = (index, containers, multiline)
            prose = prose_contexts.get(key)
            if prose is None:
                prose = _MentionProse(lines, index, line, containers, multiline=multiline)
                prose_contexts[key] = prose
        return prose

    def literal_slice(at_line: int, logical_line: str, start: int, end: int) -> str:
        if at_line not in source_lines:
            source = lines[at_line]
            source_lines[at_line] = _MentionSourceLine(source) if '\t' in source else None
        mapped = source_lines[at_line]
        if mapped is None:
            return logical_line[start:end]
        return mapped.literal_slice(logical_line, start, end)

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
            brackets.clear()
        containers = current_containers
        fence_match = _MD_MENTION_FENCE_RE.match(line)

        block_line = False
        if not in_comment:
            if not line.strip():
                paragraph = False
                brackets.clear()
                index += 1
                continue
            indent = len(line) - len(line.lstrip(' '))
            # An indented line cannot interrupt a paragraph. List content has
            # its own indentation baseline, including after a blank line.
            if indent >= 4 and not paragraph:
                brackets.clear()
                index += 1
                continue
            if (fence_match and (fence_match.group(1)[0] != '`'
                                 or '`' not in fence_match.group(2))):
                fence = (fence_match.group(1)[0], len(fence_match.group(1)), containers)
                paragraph = False
                brackets.clear()
                index += 1
                continue
            if html_end := _mention_html_block_end(line, paragraph):
                if not html_end.search(line):
                    html_block = (html_end, containers)
                paragraph = False
                brackets.clear()
                index += 1
                continue
            if collect_definitions and re.match(r'^ {0,3}<!--', line):
                comment_end = re.compile('-->')
                if not comment_end.search(line):
                    html_block = (comment_end, containers)
                paragraph = False
                index += 1
                continue
            if not paragraph and re.match(r'^ {0,3}\[', line):
                context = prose_context()
                reference_end = _mention_reference_end(
                    context.text, context.parenthesis_ends, context.offset(index),
                )
                if reference_end is not None:
                    if collect_definitions:
                        reference = _MD_MENTION_REFERENCE_RE.match(
                            context.text, context.offset(index),
                        )
                        if reference is not None:
                            reference_labels.add(_mention_label_key(reference.group(1)))
                    index, _ = context.position(reference_end)
                    index += 1
                    brackets.clear()
                    continue
            if index in heading_owners:
                owner = heading_owners[index]
            elif _MD_MENTION_HEADING_RE.match(line):
                # The legacy AST parser can omit a heading recognized here.
                # Withhold citations until an existing heading binds the owner.
                owner = None
            block_line = bool(_MD_MENTION_BREAK_RE.match(line))
            if block_line:
                brackets.clear()
            paragraph = not block_line

        # Definitions belong to the block phase. Inline code/comment syntax
        # in a reference label cannot change which later blocks define it.
        if collect_definitions:
            index += 1
            continue

        position = 0
        while position < len(line):
            if in_comment:
                end = line.find('-->', position)
                if end < 0:
                    break
                in_comment = False
                position = end + 3
                continue
            token = _MD_MENTION_TOKEN_RE.search(line, position)
            if token is None:
                if line[position:].strip() and not block_line:
                    paragraph = True
                break
            start = token.start()
            if token.group() == '\\':
                position = start + 1
                if position < len(line) and line[position] in string.punctuation:
                    position += 1
                continue
            if token.group() in {'[', '!['}:
                position = start + len(token.group())
                if reference_labels and not collect_definitions:
                    prose_context(multiline=not block_line)
                brackets.append((token.group() == '![', True, index, position))
                continue
            if token.group() == ']':
                opener = brackets.pop() if brackets else None
                position = start + 1
                if (opener is not None and opener[1]
                        and (line[position:position + 1] == '('
                             or (reference_labels and not collect_definitions))):
                    context = prose_context(multiline=not block_line)
                    link_end = None
                    if line[position:position + 1] == '(':
                        link_end = _mention_inline_destination_end(
                            context.text, context.offset(index, position + 1),
                            context.parenthesis_ends,
                        )
                    elif reference_labels and not collect_definitions:
                        label = _MD_MENTION_LABEL_RE.match(
                            context.text, context.offset(index, position),
                        )
                        if label is not None and len(label.group(1)) <= 999:
                            raw_label = label.group(1)
                            if not raw_label and opener[2] >= context.index:
                                first_start = context.offset(opener[2], opener[3])
                                first_end = context.offset(index, start)
                                if first_end - first_start <= 999:
                                    candidate = context.text[first_start:first_end]
                                    if _MD_MENTION_LABEL_RE.fullmatch('[' + candidate + ']'):
                                        raw_label = candidate
                            if raw_label and _mention_label_key(raw_label) in reference_labels:
                                link_end = label.end()
                    if link_end is not None:
                        if not opener[0]:
                            brackets = [(image, active and image, at_line, at_pos)
                                        for image, active, at_line, at_pos in brackets]
                        end_index, end_position = context.position(link_end)
                        if end_index != index:
                            line = _mention_container_content(lines[end_index], containers,
                                                              extend=False)[0]
                        index = end_index
                        position = end_position
                continue
            if token.group() == '<!--':
                if re.match(r'^ {0,3}<!--', line) is None:
                    context = prose_context(multiline=not block_line)
                    if context.last_comment_end < context.offset(index, start + 4):
                        position = start + 4
                        continue
                in_comment = True
                position = start + 4
                continue
            if token.group() == '<':
                autolink = _MD_MENTION_AUTOLINK_RE.match(line, start)
                if autolink is not None:
                    position = autolink.end()
                    brackets = [(image, active and image, at_line, at_pos)
                                for image, active, at_line, at_pos in brackets]
                    continue
                end = _mention_html_tag_end(lines, index, start, not block_line, line, containers)
                if end is None:
                    position = start + 1
                    continue
                end_index, end_position = end
                if end_index != index:
                    line = _mention_container_content(lines[end_index], containers,
                                                      extend=False)[0]
                index = end_index
                position = end_position
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
            if end_index == index:
                parts = [literal_slice(index, line, start + width, end_position)]
            else:
                parts = [literal_slice(index, line, start + width, len(line))]
                for at_line, content in enumerate(continued_lines, index + 1):
                    end_column = end_position if at_line == end_index else len(content)
                    parts.append(literal_slice(at_line, content, 0, end_column))
            # CommonMark normalizes soft line endings in code spans to spaces.
            if owner is not None:
                yield owner, ' '.join(parts), index + 1
            if end_index != index:
                line = _mention_container_content(lines[end_index], containers, extend=False)[0]
            index = end_index
            position = end_position + width
        if block_line:
            paragraph = False
            brackets.clear()
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
