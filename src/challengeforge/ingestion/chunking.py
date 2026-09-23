"""Retrieval-oriented chunking over canonical documents.

CHUNKER_VERSION = cf-chunk-hybrid-1

Strategy: **hybrid structure-aware packing with a character budget**.

- Prefer Markdown / prose / code natural boundaries
- Soft-pack small units under ``max_chars``
- Hard-split oversized logical units (never unbounded)
- No overlap in v1 (cleaner provenance; revisit if retrieval needs windows)
- Budget is **Unicode code points** (Python ``str`` length), not LLM tokens

Also exposes ``chunk_fixed_chars`` for experimental comparison only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from challengeforge.ingestion.canonical import CanonicalDocument, ContentKind

CHUNKER_VERSION = "cf-chunk-hybrid-1"
DEFAULT_MAX_CHARS = 1200


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    CODE_FENCE = "code_fence"
    CODE = "code"
    LIST = "list"
    HARD_SPLIT = "hard_split"
    MIXED = "mixed"
    FIXED = "fixed"


@dataclass(frozen=True)
class LogicalUnit:
    text: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    block_type: BlockType
    heading_path: tuple[str, ...]


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    content: str
    content_sha256: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    block_type: str
    heading_path: tuple[str, ...]
    oversized_split: bool


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_LIST_RE = re.compile(r"^(\s*([-*+]|\d+\.)\s+)")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_starts(text: str) -> list[int]:
    """Index 0 unused; index L = char offset where 1-based line L begins."""
    starts = [0, 0]
    for i, ch in enumerate(text):
        if ch == "\n" and i + 1 < len(text):
            starts.append(i + 1)
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    if offset < 0:
        return 1
    lo, hi, ans = 1, len(starts) - 1, 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if starts[mid] <= offset:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _span(
    text: str, starts: list[int], line0: int, line1_exclusive: int
) -> tuple[int, int, str]:
    """Char span for 0-based lines ``[line0, line1_exclusive)`` (end exclusive)."""
    if not text or line0 >= line1_exclusive:
        return 0, 0, ""
    cs = starts[line0 + 1]
    next_1based = line1_exclusive + 1
    if next_1based < len(starts):
        ce = starts[next_1based]
        if ce > cs and text[ce - 1] == "\n":
            ce -= 1
    else:
        ce = len(text)
    body = text[cs:ce]
    return cs, ce, body


def _split_oversized(unit: LogicalUnit, max_chars: int) -> list[LogicalUnit]:
    if len(unit.text) <= max_chars:
        return [unit]
    out: list[LogicalUnit] = []
    text = unit.text
    base = unit.char_start
    pos = 0
    while pos < len(text):
        remaining = len(text) - pos
        if remaining <= max_chars:
            cut = remaining
        else:
            window = text[pos : pos + max_chars]
            cut = window.rfind("\n")
            if cut < max_chars // 4:
                cut = window.rfind(" ")
            if cut < max_chars // 4:
                cut = max_chars
            else:
                cut = cut + 1
        piece = text[pos : pos + cut]
        if not piece:
            break
        abs_start = base + pos
        abs_end = abs_start + len(piece)
        out.append(
            LogicalUnit(
                text=piece,
                char_start=abs_start,
                char_end=abs_end,
                line_start=unit.line_start,
                line_end=unit.line_end,
                block_type=BlockType.HARD_SPLIT,
                heading_path=unit.heading_path,
            )
        )
        pos += cut
    return out


def _unitize_structured(text: str, *, code_mode: bool) -> list[LogicalUnit]:
    if not text:
        return []
    starts = _line_starts(text)
    lines = text.split("\n")
    units: list[LogicalUnit] = []
    heading_stack: list[tuple[int, str]] = []
    i = 0
    n = len(lines)

    def path() -> tuple[str, ...]:
        return tuple(t for _, t in heading_stack)

    while i < n:
        line = lines[i]

        if not code_mode and line.startswith("```"):
            j = i + 1
            while j < n and not lines[j].startswith("```"):
                j += 1
            if j < n:
                j += 1
            cs, ce, body = _span(text, starts, i, j)
            if body.strip():
                units.append(
                    LogicalUnit(
                        text=body,
                        char_start=cs,
                        char_end=ce,
                        line_start=i + 1,
                        line_end=j,
                        block_type=BlockType.CODE_FENCE,
                        heading_path=path(),
                    )
                )
            i = j
            continue

        if not code_mode:
            hm = _HEADING_RE.match(line)
            if hm:
                level = len(hm.group(1))
                title = hm.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                cs, ce, body = _span(text, starts, i, i + 1)
                units.append(
                    LogicalUnit(
                        text=body,
                        char_start=cs,
                        char_end=ce,
                        line_start=i + 1,
                        line_end=i + 1,
                        block_type=BlockType.HEADING,
                        heading_path=path(),
                    )
                )
                i += 1
                continue

        if line.strip() == "":
            i += 1
            continue

        j = i
        is_list = (not code_mode) and bool(_LIST_RE.match(line))
        while j < n and lines[j].strip() != "":
            if not code_mode:
                if lines[j].startswith("```") or _HEADING_RE.match(lines[j]):
                    break
                if is_list:
                    if not (
                        _LIST_RE.match(lines[j])
                        or lines[j].startswith((" ", "\t"))
                    ):
                        break
                elif _LIST_RE.match(lines[j]):
                    break
            j += 1
        cs, ce, body = _span(text, starts, i, j)
        if body.strip():
            if code_mode:
                btype = BlockType.CODE
            elif is_list:
                btype = BlockType.LIST
            else:
                btype = BlockType.PARAGRAPH
            units.append(
                LogicalUnit(
                    text=body,
                    char_start=cs,
                    char_end=ce,
                    line_start=i + 1,
                    line_end=j,
                    block_type=btype,
                    heading_path=path(),
                )
            )
        i = j
    return units


def unitize(doc: CanonicalDocument) -> list[LogicalUnit]:
    if doc.kind == ContentKind.EMPTY or not doc.text.strip():
        return []
    if doc.kind == ContentKind.CODE:
        return _unitize_structured(doc.text, code_mode=True)
    return _unitize_structured(doc.text, code_mode=False)


def _fix_lines(text: str, units: list[LogicalUnit]) -> list[LogicalUnit]:
    starts = _line_starts(text)
    out: list[LogicalUnit] = []
    for u in units:
        if not text:
            ls = le = 1
        else:
            ls = _line_of(starts, u.char_start)
            le = _line_of(starts, max(u.char_start, u.char_end - 1))
        out.append(
            LogicalUnit(
                text=u.text,
                char_start=u.char_start,
                char_end=u.char_end,
                line_start=ls,
                line_end=le,
                block_type=u.block_type,
                heading_path=u.heading_path,
            )
        )
    return out


def _pack(units: list[LogicalUnit], max_chars: int, text: str) -> list[list[LogicalUnit]]:
    """Pack contiguous units so each group maps to ``text[start:end]`` exactly."""
    groups: list[list[LogicalUnit]] = []
    current: list[LogicalUnit] = []

    def group_len(group: list[LogicalUnit]) -> int:
        if not group:
            return 0
        return group[-1].char_end - group[0].char_start

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for unit in units:
        if not current:
            current = [unit]
            continue
        # Contiguous in document order (allow only forward spans)
        tentative_end = unit.char_end
        tentative_start = current[0].char_start
        span_len = tentative_end - tentative_start
        if span_len <= max_chars and unit.char_start >= current[-1].char_start:
            current.append(unit)
        else:
            flush()
            current = [unit]
    flush()
    # Safety: if a single unit somehow exceeds (should be hard-split already), keep it
    return groups


def chunk_hybrid(
    doc: CanonicalDocument,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[ChunkDraft]:
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    raw_units = unitize(doc)
    expanded: list[LogicalUnit] = []
    for u in raw_units:
        expanded.extend(_split_oversized(u, max_chars))
    expanded = _fix_lines(doc.text, expanded)
    repaired_units: list[LogicalUnit] = []
    for u in expanded:
        expected = doc.text[u.char_start : u.char_end]
        if expected != u.text:
            u = LogicalUnit(
                text=expected,
                char_start=u.char_start,
                char_end=u.char_end,
                line_start=u.line_start,
                line_end=u.line_end,
                block_type=u.block_type,
                heading_path=u.heading_path,
            )
        repaired_units.append(u)
    expanded = _fix_lines(doc.text, repaired_units)
    groups = _pack(expanded, max_chars, doc.text)
    chunks: list[ChunkDraft] = []
    for group in groups:
        char_start = group[0].char_start
        char_end = group[-1].char_end
        content = doc.text[char_start:char_end]
        if not content.strip():
            continue
        if len(content) > max_chars:
            # Should only happen if hard-split failed; truncate with provenance
            content = content[:max_chars]
            char_end = char_start + len(content)
        types = {u.block_type for u in group}
        if len(types) == 1:
            block_type = next(iter(types)).value
        elif BlockType.HARD_SPLIT in types:
            block_type = BlockType.HARD_SPLIT.value
        else:
            block_type = BlockType.MIXED.value
        heading = group[0].heading_path
        # Prefer deepest path that still prefixes later units when nested in-span
        for u in group:
            if u.heading_path[: len(heading)] == heading and len(u.heading_path) > len(
                heading
            ):
                heading = u.heading_path
            elif not heading:
                heading = u.heading_path
        starts = _line_starts(doc.text)
        line_start = _line_of(starts, char_start) if doc.text else 1
        line_end = _line_of(starts, max(char_start, char_end - 1)) if doc.text else 1
        chunks.append(
            ChunkDraft(
                ordinal=len(chunks),
                content=content,
                content_sha256=_sha(content),
                char_start=char_start,
                char_end=char_end,
                line_start=line_start,
                line_end=line_end,
                block_type=block_type,
                heading_path=heading,
                oversized_split=BlockType.HARD_SPLIT in types,
            )
        )
    return chunks


def chunk_fixed_chars(
    doc: CanonicalDocument,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = 0,
) -> list[ChunkDraft]:
    """Naive fixed-size splitter for experimental comparison only."""
    text = doc.text
    if not text.strip():
        return []
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be >= 0 and < max_chars")
    starts = _line_starts(text)
    chunks: list[ChunkDraft] = []
    pos = 0
    step = max(1, max_chars - overlap)
    while pos < len(text):
        end = min(len(text), pos + max_chars)
        content = text[pos:end]
        if content.strip():
            chunks.append(
                ChunkDraft(
                    ordinal=len(chunks),
                    content=content,
                    content_sha256=_sha(content),
                    char_start=pos,
                    char_end=end,
                    line_start=_line_of(starts, pos),
                    line_end=_line_of(starts, max(pos, end - 1)),
                    block_type=BlockType.FIXED.value,
                    heading_path=(),
                    oversized_split=False,
                )
            )
        if end >= len(text):
            break
        pos += step
    return chunks
