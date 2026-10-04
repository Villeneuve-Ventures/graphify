"""Bounded project XML admission, without DTD or entity expansion.

Expat recognizes declarations in its supported XML encodings. Rejecting the
doctype callback stops before the internal subset or external DTD is processed;
a standalone ENTITY declaration is invalid XML and fails parsing. Comments,
CDATA and escaped declaration text remain ordinary content. Input byte bounds
do not provide a general CPU or tree-memory bound; runtime Expat still matters.
"""
from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

from graphify.source_io import current_source_io, SourceTooLarge


class XMLDeclarationError(ET.ParseError):
    """A real DTD declaration is outside the project XML contract."""


class _RejectingTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise XMLDeclarationError("refusing XML with DOCTYPE/ENTITY declaration")


def parse_xml(payload: bytes | str) -> ET.Element:
    """Parse admitted content with declaration rejection before expansion."""
    return ET.fromstring(payload, parser=ET.XMLParser(target=_RejectingTreeBuilder()))


def read_xml_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Read a complete bounded input; never accept a truncated prefix.

    SourceIO retains its own tighter limits, evidence and failure latch. The
    ordinary reader requests at most cap+1 bytes, including if a file grows.
    """
    scope = current_source_io()
    if scope is not None:
        return scope.read_bytes(path, max_bytes=max_bytes)
    with path.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise SourceTooLarge("input byte limit exceeded")
    return payload
