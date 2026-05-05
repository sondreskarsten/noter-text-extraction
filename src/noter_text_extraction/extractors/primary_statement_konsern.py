"""Konsern primary statement extractor.

Konsern (consolidated) regnskap have 4 numerical columns per row:
    <Label>  [Note]  morselskap_curr  morselskap_prev  konsern_curr  konsern_prev

The TSV column-aware OCR returns these as 4 tab-separated tokens. We emit
both `value` (consolidated current — the main figure) and `morselskap_*`
prefixed fields for the parent-only values.

Detection: when a candidate line yields ≥4 numerical columns, treat as konsern.
Falls back to selskap (2-col) if a line has only 2.
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


def _extract_columns(line: str, label: str) -> list[int]:
    if label:
        idx = line.lower().find(label.lower())
        tail = line[idx + len(label):]
    else:
        tail = line
    cols = [c.strip() for c in tail.split("\t")]
    nums: list[int] = []
    for c in cols:
        if not c:
            continue
        cleaned = c.replace(" ", "").replace("\u00a0", "").replace(",", "")
        if len(cleaned) <= 4 and cleaned.isdigit():
            continue
        v = normalize_amount(c)
        if v is not None:
            nums.append(v)
    return nums


def _scan_for_field_konsern(spec: FieldSpec, page_texts: list[str]) -> dict | None:
    for page_idx, text in enumerate(page_texts):
        lines = text.splitlines()
        for line_idx, raw in enumerate(lines):
            for label in spec.norwegian_labels:
                if not _label_anchor_match(raw, label):
                    continue
                nums = _extract_columns(raw, label)
                if len(nums) >= 4:
                    return {
                        "value": nums[-2],            # konsern_curr (last is konsern_prev)
                        "value_prev": nums[-1],
                        "morselskap_value": nums[-4],
                        "morselskap_value_prev": nums[-3],
                        "page_idx": page_idx,
                        "line_idx": line_idx,
                        "raw_line": raw,
                        "label_matched": label,
                        "n_numbers": len(nums),
                        "layout": "4col",
                    }
                if len(nums) >= 2:
                    return {
                        "value": nums[-2],
                        "value_prev": nums[-1],
                        "page_idx": page_idx,
                        "line_idx": line_idx,
                        "raw_line": raw,
                        "label_matched": label,
                        "n_numbers": len(nums),
                        "layout": "2col_fallback",
                    }
    return None


def extract_primary_statements_konsern(brreg_page_images: list[Image.Image]) -> dict:
    """Extract canonical primary-statement fields with konsern (4-col) support."""
    page_texts = [_ocr_image(img) for img in brreg_page_images]

    out = {}
    for spec in RESULTAT_FIELDS + BALANSE_FIELDS:
        hit = _scan_for_field_konsern(spec, page_texts)
        if hit is not None:
            out[spec.canonical] = hit

    return {"fields": out, "_page_texts": page_texts}
