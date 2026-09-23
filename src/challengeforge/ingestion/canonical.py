"""Canonical document representation (structure-preserving normalize).

PARSER_VERSION = cf-parse-2

Prior synthetic normalize collapsed all whitespace (cf-parse-1 style), which
destroyed Markdown/code structure and made provenance offsets meaningless.
This module produces a UTF-8 text whose line/character offsets are the
authoritative source for chunk provenance.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum


PARSER_VERSION = "cf-parse-2"


class ContentKind(StrEnum):
    MARKDOWN = "markdown"
    CODE = "code"
    PROSE = "prose"
    EMPTY = "empty"


@dataclass(frozen=True)
class CanonicalDocument:
    """Structure-preserving normalized artifact text.

    Offsets used by the chunker are measured against ``text`` (Unicode code
    points / Python ``str`` indices), **not** raw artifact UTF-8 bytes.
    """

    text: str
    kind: ContentKind
    parser_version: str
    content_sha256: str
    line_count: int

    @property
    def char_count(self) -> int:
        return len(self.text)


def _detect_kind(text: str) -> ContentKind:
    if not text.strip():
        return ContentKind.EMPTY
    lines = text.split("\n")
    heading_hits = sum(1 for ln in lines if ln.lstrip().startswith("#"))
    fence_hits = sum(1 for ln in lines if ln.strip().startswith("```"))
    codey = sum(
        1
        for ln in lines
        if ln.startswith(("def ", "class ", "import ", "from ", "function ", "package "))
        or ln.rstrip().endswith("{")
        or ln.strip().startswith(("public ", "private ", "static "))
    )
    if fence_hits >= 1 or heading_hits >= 2:
        return ContentKind.MARKDOWN
    if codey >= 2 and heading_hits == 0:
        return ContentKind.CODE
    return ContentKind.PROSE


def normalize_to_canonical(raw_text: str) -> CanonicalDocument:
    """Normalize without destroying structure.

    - Unify newlines to ``\\n``
    - Strip trailing spaces per line
    - Strip leading/trailing document blank lines
    - Preserve internal blank lines (paragraph / block separators)
    - Do **not** collapse runs of spaces inside lines (code indentation)
    """
    unified = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip(" \t") for ln in unified.split("\n")]
    # Trim leading/trailing empty lines only
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    text = "\n".join(lines)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    kind = _detect_kind(text)
    return CanonicalDocument(
        text=text,
        kind=kind,
        parser_version=PARSER_VERSION,
        content_sha256=digest,
        line_count=0 if text == "" else text.count("\n") + 1,
    )
