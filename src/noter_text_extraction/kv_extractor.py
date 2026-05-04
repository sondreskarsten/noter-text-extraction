"""Extract structured key-value pairs from a note's raw_text.

Supports two table layouts:

  Layout A — column-as-period:
    Header: '<sub-table-name>?\\t2024\\t2023'
    Data:   '<row label>\\t<v1>\\t<v2>'
    Output: '<row label> <year>': value

  Layout B — column-as-field (roll-forward style):
    Header: 'Aksjekapital\\tAnnen innskutt\\tAnnen opptjent\\tSum'
    Data:   'EK pr. 31.12.2024\\t<v1>\\t<v2>\\t<v3>\\t<v4>'
    Output: '<row label> <field>': value

  Fallback: scan each line for the rightmost number, emit '<label>': value.
"""
import re
from typing import Optional

from .amount_normalizer import normalize_amount


_YEAR_HEADER = re.compile(r"\b(20[0-2]\d)\b")
_YEAR_ONLY = re.compile(r"^(20[0-2]\d)$")


def extract_kv(raw_text: str) -> dict:
    if not raw_text:
        return {}
    lines = [ln.rstrip() for ln in raw_text.split("\n")]
    headers = _detect_headers(lines)
    if not headers:
        return _fallback_unkeyed(lines)
    out: dict = {}
    for idx, hdr in enumerate(headers):
        next_hi = headers[idx + 1]["line_idx"] if idx + 1 < len(headers) else len(lines)
        for ln in lines[hdr["line_idx"] + 1: next_hi]:
            row = _parse_table_row(ln, hdr["column_labels"], hdr["kind"])
            if row:
                out.update(row)
    return out


def _detect_headers(lines: list[str]) -> list[dict]:
    """Two header kinds:
      - 'year': trailing tab-cells are years
      - 'field': all tab-cells are short text labels"""
    headers = []
    for i, ln in enumerate(lines):
        years = _trailing_year_cells(ln)
        if years:
            headers.append({"line_idx": i, "kind": "year", "column_labels": years})
            continue
        if "\t" not in ln:
            extracted = _YEAR_HEADER.findall(ln)
            if extracted and _looks_like_year_header(ln, extracted):
                headers.append({"line_idx": i, "kind": "year", "column_labels": extracted})
                continue
        cells = [c.strip() for c in ln.split("\t") if c.strip()]
        if len(cells) >= 2 and all(_looks_like_field_label(c) for c in cells):
            headers.append({"line_idx": i, "kind": "field", "column_labels": cells})
    return headers


def _trailing_year_cells(line: str) -> list[str]:
    if "\t" not in line:
        return []
    cells = [c.strip() for c in line.split("\t")]
    trailing = []
    for cell in reversed(cells):
        if not cell:
            continue
        if _YEAR_ONLY.match(cell):
            trailing.insert(0, cell)
        else:
            break
    return trailing


def _looks_like_year_header(line: str, years: list[str]) -> bool:
    stripped = line
    for y in years:
        stripped = stripped.replace(y, "")
    stripped = stripped.replace("\t", " ").strip()
    has_long_number = bool(re.search(r"\d{3,}", stripped))
    return not has_long_number and len(stripped) < 60


def _looks_like_field_label(cell: str) -> bool:
    if len(cell) < 2 or len(cell) > 60:
        return False
    if normalize_amount(cell) is not None:
        return False
    digits = sum(c.isdigit() for c in cell)
    return digits < len(cell) * 0.4


_BLACKLIST_LABEL_KEYWORDS = (
    "docusign", "envelope id", "noter til regnskapet",
)


def _is_blacklisted_label(label: str) -> bool:
    low = label.lower()
    return any(kw in low for kw in _BLACKLIST_LABEL_KEYWORDS)


def _parse_table_row(line: str, column_labels: list[str], kind: str) -> dict:
    if not line.strip():
        return {}
    cells = [c.strip() for c in line.split("\t") if c.strip()]
    if len(cells) < 2:
        return _parse_table_row_space(line, column_labels)
    parsed = [normalize_amount(c) for c in cells]
    n_trailing = 0
    for v in reversed(parsed):
        if v is None:
            break
        n_trailing += 1
    if n_trailing == 0:
        return {}
    label_cells = cells[: len(cells) - n_trailing]
    label = " ".join(label_cells).strip()
    if not label or len(label) < 2 or label.isdigit():
        return {}
    if _is_blacklisted_label(label):
        return {}
    values = parsed[len(cells) - n_trailing:]
    out = {}
    for i, v in enumerate(values[:len(column_labels)]):
        if v is not None:
            out[f"{label} {column_labels[i]}"] = v
    if not out and values and values[0] is not None and column_labels:
        out[f"{label} {column_labels[0]}"] = values[0]
    return out


def _parse_table_row_space(line: str, column_labels: list[str]) -> dict:
    line = line.strip()
    candidates = _find_amounts_spaced(line)
    if not candidates:
        return {}
    first_start = candidates[0][0]
    label = line[:first_start].strip()
    if not label or len(label) < 2 or label.isdigit():
        return {}
    values = [c[2] for c in candidates if c[2] is not None]
    out = {}
    for i, v in enumerate(values[:len(column_labels)]):
        out[f"{label} {column_labels[i]}"] = v
    return out


def _fallback_unkeyed(lines: list[str]) -> dict:
    out: dict = {}
    for ln in lines:
        line_norm = ln.replace("\t", " ").strip()
        if not line_norm:
            continue
        candidates = _find_amounts_spaced(line_norm)
        if not candidates:
            continue
        first_start, _, first_val = candidates[0]
        if first_val is None:
            continue
        label = line_norm[:first_start].strip()
        if not label or len(label) < 2:
            continue
        out[label] = first_val
    return out


_NUMBER_TOKEN = re.compile(
    r"-?\(?\d{1,4}(?:[\s.\u00a0]\d{3}){0,3}(?:,\d+)?\)?",
)


def _find_amounts_spaced(line: str) -> list[tuple[int, int, Optional[float]]]:
    out = []
    for m in _NUMBER_TOKEN.finditer(line):
        token = m.group(0)
        if re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}", token):
            continue
        val = normalize_amount(token)
        if val is not None:
            out.append((m.start(), m.end(), val))
    return out
