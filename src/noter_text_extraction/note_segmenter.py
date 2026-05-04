"""Segment a stream of OCR'd page texts into individual notes.

Norwegian årsregnskap notes follow a stable header pattern:
  'Note <N> <Title>'   (most common)
  'Note <N>:<Title>'
  'Note <N>'           (then title on next line)
  '<N>. <Title>'       (numbered list style — less common)

We also attempt to detect the boundary between primary statements
(resultatregnskap/balanse) and notes — only content AFTER 'Noter' or the
first 'Note 1' is segmented as notes."""
import re
from typing import Iterable


_NOTE_HEADER = re.compile(
    r"""
    ^[\s]*
    Note\s+
    (?P<nr>\d+[a-z]?)
    [\s:.\-]+
    (?P<title>[^\n]+?)
    [\s]*$
    """,
    re.VERBOSE | re.IGNORECASE | re.MULTILINE,
)

_NOTE_HEADER_BARE = re.compile(
    r"^\s*Note\s+(?P<nr>\d+[a-z]?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def segment_notes(pages_text: list[str]) -> list[dict]:
    """Concatenate OCR'd pages and split into notes.

    Returns a list of dicts with shape:
      {
        'nr': '1',
        'tittel': 'Lønnskostnader',
        'raw_text': '...full text of the note...',
        'page_refs': [page_idx_start, page_idx_end],  # 1-indexed
      }
    """
    # Concatenate with a page separator we can later use to attribute lines back to pages
    page_break = "\n<<<PAGE_BREAK>>>\n"
    combined = page_break.join(pages_text)

    # Find all note headers with their character positions
    headers = []
    for m in _NOTE_HEADER.finditer(combined):
        headers.append({
            "nr": m.group("nr"),
            "tittel": m.group("title").strip(),
            "start": m.start(),
            "end": m.end(),
        })
    # Also catch bare "Note N" headers where title is on next line
    for m in _NOTE_HEADER_BARE.finditer(combined):
        # If we already captured this position via the long form, skip
        if any(h["start"] == m.start() for h in headers):
            continue
        # Try to read the next non-empty line as the title
        rest = combined[m.end():]
        first_line = rest.split("\n", 2)[1].strip() if "\n" in rest else ""
        if first_line and len(first_line) < 100 and not _looks_numeric(first_line):
            headers.append({
                "nr": m.group("nr"),
                "tittel": first_line,
                "start": m.start(),
                "end": m.end() + len(first_line) + 1,
            })

    # Sort by position and dedupe by note number
    headers.sort(key=lambda h: h["start"])
    seen_nrs = set()
    headers_unique = []
    for h in headers:
        if h["nr"] in seen_nrs:
            continue
        seen_nrs.add(h["nr"])
        headers_unique.append(h)

    # Slice text between consecutive note headers
    notes = []
    for i, h in enumerate(headers_unique):
        body_start = h["end"]
        body_end = headers_unique[i + 1]["start"] if i + 1 < len(headers_unique) else len(combined)
        body = combined[body_start:body_end].strip()
        # Recover page_refs from <<<PAGE_BREAK>>> markers
        page_refs = _resolve_page_refs(combined, h["start"], body_end, page_break)
        notes.append({
            "nr": h["nr"],
            "tittel": h["tittel"],
            "raw_text": body.replace(page_break, "\n"),
            "page_refs": page_refs,
        })
    return notes


def _looks_numeric(s: str) -> bool:
    digits = sum(c.isdigit() for c in s)
    return digits > len(s) * 0.4


def _resolve_page_refs(text: str, start: int, end: int, page_break: str) -> list[int]:
    """Translate (char_start, char_end) into 1-indexed page numbers."""
    pages_before_start = text[:start].count(page_break)
    pages_before_end = text[:end].count(page_break)
    return list(range(pages_before_start + 1, pages_before_end + 2))
