"""Bounded, streaming inspection of the portable schema marker in JSON graphs."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
import codecs
from dataclasses import dataclass
import json
import re

_WINDOW = 64 * 1024
_MAX_DEPTH = 1024
_TARGET_KEYS = ("graph", "_graphify_protocol", "schema")
_STRING_BOUNDARY = re.compile(r'["\\\x00-\x1f]')
_SPACE = re.compile(r"[ \t\r\n]+")
_DIGITS = re.compile(r"[0-9]+")
_HEX_ESCAPE = re.compile(r"[0-9a-fA-F]{4}\Z")
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
            "n": "\n", "r": "\r", "t": "\t"}


class GraphMarkerLimitError(RuntimeError):
    """Marker inspection exceeded a resource bound; callers must refuse."""


def _decoded_windows(chunks: Iterable[bytes]) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    for chunk in chunks:
        for start in range(0, len(chunk), _WINDOW):
            text = decoder.decode(chunk[start:start + _WINDOW])
            if text:
                yield text
    tail = decoder.decode(b"", final=True)
    if tail:
        yield tail


class _Text:
    def __init__(self, chunks: Iterable[bytes]):
        self.windows = iter(_decoded_windows(chunks))
        self.text = ""
        self.position = 0
        self.finished = False

    def fill(self) -> bool:
        if self.position < len(self.text):
            return True
        if not self.finished:
            try:
                self.text = next(self.windows)
                self.position = 0
                return True
            except StopIteration:
                self.finished = True
                self.text = ""
                self.position = 0
        return False

    def peek(self) -> str:
        return self.text[self.position] if self.fill() else ""

    def take(self) -> str:
        character = self.peek()
        if not character:
            raise ValueError("truncated graph JSON")
        self.position += 1
        return character

    def space(self) -> None:
        while self.fill():
            match = _SPACE.match(self.text, self.position)
            if match is None:
                return
            self.position = match.end()

    def string(self, target: str | None = None) -> bool:
        """Validate a string, comparing only its decoded runs with a short key."""
        if self.take() != '"':
            raise ValueError("graph JSON object key must be a string")
        matches = target is not None
        matched = 0
        while self.fill():
            boundary = _STRING_BOUNDARY.search(self.text, self.position)
            end = boundary.start() if boundary is not None else len(self.text)
            length = end - self.position
            if matches and target is not None:
                if (length > len(target) - matched
                        or not target.startswith(self.text[self.position:end], matched)):
                    matches = False
                else:
                    matched += length
            self.position = end
            if boundary is None:
                continue
            character = self.take()
            if character == '"':
                return matches and target is not None and matched == len(target)
            if character != "\\":
                raise ValueError("unescaped control character in graph JSON string")
            escape = self.take()
            if escape == "u":
                digits = "".join(self.take() for _ in range(4))
                if _HEX_ESCAPE.fullmatch(digits) is None:
                    raise ValueError("invalid Unicode escape in graph JSON string")
                character = chr(int(digits, 16))
            elif escape in _ESCAPES:
                character = _ESCAPES[escape]
            else:
                raise ValueError("invalid escape in graph JSON string")
            if matches and target is not None:
                if matched == len(target) or target[matched] != character:
                    matches = False
                else:
                    matched += 1
        raise ValueError("unterminated graph JSON string")

    def literal(self, value: str) -> None:
        for character in value:
            if self.take() != character:
                raise ValueError("invalid literal in graph JSON")


@dataclass(slots=True)
class _Frame:
    kind: str
    target: int | None
    state: str
    selected: bool = False
    result: bool = False


class _Probe:
    def __init__(self, chunks: Iterable[bytes], token_limit: int):
        self.text = _Text(chunks)
        self.token_limit = token_limit
        self.stack: list[_Frame] = []
        self.result = False

    def number(self, retain: bool) -> bool:
        kept = bytearray() if retain else None

        def append(start: int, end: int) -> None:
            if kept is not None:
                if len(kept) + end - start > self.token_limit:
                    raise GraphMarkerLimitError("graph marker numeric token exceeds limit")
                kept.extend(self.text.text[start:end].encode("ascii"))

        def character() -> None:
            self.text.peek()
            start = self.text.position
            self.text.take()
            append(start, self.text.position)

        def digits() -> None:
            found = False
            while self.text.fill():
                match = _DIGITS.match(self.text.text, self.text.position)
                if match is None:
                    break
                found = True
                append(self.text.position, match.end())
                self.text.position = match.end()
            if not found:
                raise ValueError("missing digits in graph JSON number")

        if self.text.peek() == "-":
            character()
        if self.text.peek() == "I":
            for expected in "Infinity":
                if self.text.peek() != expected:
                    raise ValueError("invalid literal in graph JSON")
                character()
        else:
            first = self.text.peek()
            if first == "0":
                character()
            elif first in "123456789" and first:
                digits()
            else:
                raise ValueError("invalid graph JSON number")
            if self.text.peek() == ".":
                character()
                digits()
            if self.text.peek() in ("e", "E"):
                character()
                if self.text.peek() in ("+", "-"):
                    character()
                digits()
        if kept is None:
            return False
        try:
            return json.loads(kept) == 2
        except (ValueError, OverflowError) as exc:
            # Grammar has already validated this numeric token. For example,
            # CPython's integer-digit ceiling is a resource refusal, not bad JSON.
            raise GraphMarkerLimitError("graph marker numeric token exceeds runtime limits") from exc

    def begin(self, target: int | None) -> bool | None:
        self.text.space()
        character = self.text.peek()
        if character in ("{", "["):
            if len(self.stack) >= _MAX_DEPTH:
                raise GraphMarkerLimitError("graph JSON container depth exceeds limit")
            self.text.take()
            if character == "{":
                self.stack.append(_Frame("object", target if target != 3 else None, "first-key"))
            else:
                self.stack.append(_Frame("array", None, "first-value"))
            return None
        if character == '"':
            self.text.string()
        elif character in ("-", "I") or (character and character in "0123456789"):
            return self.number(target == 3)
        elif character in ("t", "f", "n", "N"):
            self.text.literal({"t": "true", "f": "false", "n": "null", "N": "NaN"}[character])
        else:
            raise ValueError("expected a graph JSON value")
        return False

    def complete(self, result: bool) -> None:
        if not self.stack:
            self.result = result
            return
        parent = self.stack[-1]
        if parent.selected:
            parent.result = result
        parent.state = "after"

    def run(self) -> bool:
        result = self.begin(0)
        if result is not None:
            self.complete(result)
        while self.stack:
            frame = self.stack[-1]
            self.text.space()
            if frame.state in ("first-key", "key"):
                if self.text.peek() == "}" and frame.state == "first-key":
                    self.text.take()
                    self.stack.pop()
                    self.complete(frame.result)
                    continue
                key = _TARGET_KEYS[frame.target] if frame.target is not None else None
                frame.selected = self.text.string(key)
                frame.state = "colon"
            elif frame.state == "colon":
                if self.text.take() != ":":
                    raise ValueError("expected a colon in graph JSON object")
                frame.state = "value"
            elif frame.state in ("first-value", "value"):
                if frame.state == "first-value" and self.text.peek() == "]":
                    self.text.take()
                    self.stack.pop()
                    self.complete(False)
                    continue
                target = frame.target + 1 if frame.selected and frame.target is not None else None
                result = self.begin(target)
                if result is not None:
                    self.complete(result)
            else:
                character = self.text.take()
                if character == ",":
                    frame.state = "key" if frame.kind == "object" else "value"
                elif character == ("}" if frame.kind == "object" else "]"):
                    self.stack.pop()
                    self.complete(frame.result)
                else:
                    raise ValueError("expected a comma or closing container in graph JSON")
        self.text.space()
        if self.text.peek():
            raise ValueError("trailing content after graph JSON")
        return self.result


def portable_schema_marker(chunks: Iterable[bytes], *, token_limit: int) -> bool:
    """Check the last ``graph._graphify_protocol.schema`` value for numeric 2.

    Validate the complete UTF-8 document, with stdlib-compatible duplicate-key
    and NaN/Infinity behavior. Non-object parent replacements clear the marker.
    Large unrelated strings, keys, arrays and objects are never materialized.
    Only relevant numeric tokens are retained, bounded by ``token_limit`` bytes;
    nesting is limited to 1024 containers without relying on Python recursion.

    Malformed or truncated JSON raises ValueError. Resource limits instead raise
    GraphMarkerLimitError (RuntimeError), which must not become a legacy fallback.
    """
    if type(token_limit) is not int or token_limit <= 0:
        raise ValueError("graph marker token_limit must be a positive integer")
    return _Probe(chunks, token_limit).run()
