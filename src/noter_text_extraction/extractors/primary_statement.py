"""Primary statement extractor — resultatregnskap + balanse.

Uses TSV-based OCR with column-gap detection (tabs separate columns).
Each canonical numerical field is found by Norwegian label match across
all BRREG pages (excluding p1). The two values are taken from the trailing
tab-separated columns on the matched line.
"""

from __future__ import annotations

import os
import re
import tempfile

from PIL import Image

from ..amount_normalizer import normalize_amount
from ..canonical_schema import RESULTAT_FIELDS, BALANSE_FIELDS, FieldSpec
from ..ocr import tesseract_ocr_page


_NOTE_REF_RE = re.compile(r"^\s*\d{1,3}(?:[,\s]+\d{1,3}){0,4}\s*")


def _ocr_image(img: Image.Image) -> str:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    try:
        img.convert("RGB").save(path, "JPEG", quality=92)
        return tesseract_ocr_page(path, lang="nor", psm=6)
    finally:
        os.unlink(path)


def _label_anchor_match(line: str, label: str) -> bool:
    """Match label anchored at start of line. After label must come a tab,
    end-of-line, colon, digit, or hyphen — NOT a continuation word (so that
    'Sum egenkapital' does not match 'Sum egenkapital og gjeld')."""
    line_l = line.lstrip().lower()
    label_l = label.strip().lower()
    if not line_l.startswith(label_l):
        return False
    after = line_l[len(label_l):]
    if not after:
        return True
    if after[0] in "\t:.\u00a0,":
        return True
    if after[0].isdigit() or after[0] == "-":
        return True
    if after[0] == " ":
        rest = after[1:].lstrip()
        if not rest:
            return True
        if rest[0].isdigit() or rest[0] == "-" or rest[0] == "\t":
            return True
        return False
    return False


def _try_two_number_split(col: str) -> tuple[float | None, float | None]:
    """Given a column that may contain 1 or 2 numbers concatenated, return
    the most likely (curr, prev) pair. If only one number, returns (val, None).
    """
    tokens = col.split()
    n = len(tokens)
    whole = normalize_amount(col)
    if n < 2:
        return (whole, None)

    candidates: list[tuple[int, float, float]] = []
    for split_at in range(1, n):
        first = " ".join(tokens[:split_at])
        second = " ".join(tokens[split_at:])
        v1 = normalize_amount(first)
        v2 = normalize_amount(second)
        if v1 is not None and v2 is not None:
            candidates.append((split_at, v1, v2))

    if not candidates:
        return (whole, None)

    def balance_score(c):
        s, v1, v2 = c
        if v1 == 0 and v2 == 0:
            return 1.0
        a, b = abs(v1), abs(v2)
        if a == 0 or b == 0:
            return 0.001
        ratio = min(a, b) / max(a, b)
        d1 = max(1, len(str(int(a))))
        d2 = max(1, len(str(int(b))))
        return ratio * min(d1, d2)

    candidates.sort(key=balance_score, reverse=True)
    s, v1, v2 = candidates[0]
    return (v1, v2)


def _extract_columns(line: str, label: str) -> tuple[list[float], str, list[int]]:
    """Returns (nums, tail, col_positions). col_positions[i] gives the
    1-indexed tab-column origin of nums[i] (1 = first column = year_curr,
    2 = second column = year_prev, ...). 0 means split-derived (no tab origin)."""
    if label:
        idx = line.lower().find(label.lower())
        tail = line[idx + len(label):]
    else:
        tail = line
    has_tab = "\t" in tail
    cols = [c.strip() for c in tail.split("\t")]
    cols_nonempty_with_pos = [(i, c) for i, c in enumerate(cols) if c]
    nums: list[float] = []
    positions: list[int] = []

    skipped_cols: list[tuple[int, str]] = []

    nonempty_seq = 0
    for col_tab_idx, c in enumerate(cols):
        if not c:
            continue
        nonempty_seq += 1
        cleaned = c.replace(" ", "").replace("\u00a0", "").replace(",", "").replace("-", "")
        if len(cleaned) <= 3 and cleaned.replace("l", "1").replace("O", "0").replace("o", "0").isdigit():
            continue
        if not has_tab:
            v1, v2 = _try_two_number_split(c)
            if v1 is not None and v2 is not None:
                nums.extend([v1, v2])
                positions.extend([0, 0])
                continue
            if v1 is not None:
                nums.append(v1)
                positions.append(0)
                continue
        v = normalize_amount(c)
        if v is not None:
            nums.append(v)
            positions.append(nonempty_seq)
            continue
        v1, v2 = _try_two_number_split(c)
        if v1 is not None:
            nums.append(v1)
            positions.append(nonempty_seq)
        else:
            skipped_cols.append((nonempty_seq, c))
        if v2 is not None:
            nums.append(v2)
            positions.append(0)

    if len(nums) == 0 and len(cols_nonempty_with_pos) == 1:
        v1, v2 = _try_two_number_split(cols_nonempty_with_pos[0][1])
        if v1 is not None and v2 is not None:
            return [v1, v2], tail, [0, 0]
    if len(nums) == 1 and len(cols_nonempty_with_pos) == 1:
        tokens = cols_nonempty_with_pos[0][1].split()
        if len(tokens) >= 4:
            v1, v2 = _try_two_number_split(cols_nonempty_with_pos[0][1])
            if v1 is not None and v2 is not None and v1 != 0 and v2 != 0:
                d1 = len(str(int(abs(v1))))
                d2 = len(str(int(abs(v2))))
                if min(d1, d2) >= 3:
                    ratio = min(abs(v1), abs(v2)) / max(abs(v1), abs(v2))
                    if ratio > 0.05:
                        return [v1, v2], tail, [0, 0]

    return nums, tail, positions


def _scan_for_field(spec: FieldSpec, page_texts: list[str]) -> dict | None:
    for page_idx, text in enumerate(page_texts):
        lines = text.splitlines()
        for line_idx, raw in enumerate(lines):
            for label in spec.norwegian_labels:
                if not _label_anchor_match(raw, label):
                    continue
                nums, tail, positions = _extract_columns(raw, label)
                if len(nums) >= 2:
                    return {
                        "value": nums[-2],
                        "value_prev": nums[-1],
                        "page_idx": page_idx,
                        "line_idx": line_idx,
                        "raw_line": raw,
                        "label_matched": label,
                        "n_numbers": len(nums),
                    }
                if len(nums) == 1 and positions and positions[0] >= 2:
                    return {
                        "value": None,
                        "value_prev": nums[0],
                        "page_idx": page_idx,
                        "line_idx": line_idx,
                        "raw_line": raw,
                        "label_matched": label,
                        "n_numbers": 1,
                        "year_column_failed_ocr": True,
                    }
                if len(nums) == 0 and line_idx + 1 < len(lines):
                    next_nums, _, _ = _extract_columns(lines[line_idx + 1], "")
                    if len(next_nums) >= 2:
                        return {
                            "value": next_nums[-2],
                            "value_prev": next_nums[-1],
                            "page_idx": page_idx,
                            "line_idx": line_idx,
                            "raw_line": raw + " | " + lines[line_idx + 1],
                            "label_matched": label,
                            "n_numbers": len(next_nums),
                            "wrapped": True,
                        }
                if len(nums) == 1 and line_idx + 1 < len(lines):
                    next_nums, _, _ = _extract_columns(lines[line_idx + 1], "")
                    if len(next_nums) == 1:
                        return {
                            "value": nums[0],
                            "value_prev": next_nums[0],
                            "page_idx": page_idx,
                            "line_idx": line_idx,
                            "raw_line": raw + " | " + lines[line_idx + 1],
                            "label_matched": label,
                            "n_numbers": 2,
                            "wrapped_value": True,
                        }
    return None


def extract_primary_statements(brreg_page_images: list[Image.Image]) -> dict:
    """Extract canonical primary-statement fields from BRREG pages 2..n."""
    page_texts = [_ocr_image(img) for img in brreg_page_images]

    out = {}
    for spec in RESULTAT_FIELDS + BALANSE_FIELDS:
        hit = _scan_for_field(spec, page_texts)
        if hit is not None:
            out[spec.canonical] = hit

    return {"fields": out, "_page_texts": page_texts}
