"""Deep-dive review for nokkeltall disagreements.

When the primary extractor's value disagrees with the regnskapsapi value,
the deep-dive tries progressively more aggressive OCR strategies until
either (a) a consistent value emerges across strategies, or (b) the
strategies all disagree (escalate to manual review).

Strategies, in order:
    1. Re-render disagreement page at 400 DPI, full-page Tesseract PSM 6
    2. Same page at 400 DPI, Tesseract PSM 11 (sparse text, tighter cell-by-cell)
    3. Same page at 400 DPI, Tesseract PSM 4 (single column of text)
    4. Crop the matched line's bounding box, OCR with PSM 7 + numeric whitelist
    5. Per-word TSV at 400 DPI, walk the line manually

Output:
    {
      "field": str,
      "tesseract_v1_value": int,
      "api_value": int,
      "strategies": [
          {"name": str, "value": int|None, "raw": str, "confidence": str},
          ...
      ],
      "reconciled_value": int|None,
      "diagnosis": str  # column_merge / digit_misread / wrapped_label / api_disagrees / ambiguous
    }
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile

import fitz
from PIL import Image
import pytesseract

from .amount_normalizer import normalize_amount
from .ocr import tesseract_ocr_page, tesseract_tsv


_NUM_LINE_RE = re.compile(r"-?\d[\d\s\u00a0]*\d|-?\d")


def _render_page(pdf_bytes: bytes, page_idx: int, dpi: int) -> Image.Image:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_idx]
    if abs(page.rect.width - 1728) < 5:
        imgs = page.get_images(full=True)
        if imgs:
            xref = imgs[0][0]
            base = doc.extract_image(xref)
            img = Image.open(io.BytesIO(base["image"]))
            doc.close()
            return img
    pix = page.get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    doc.close()
    return img


def _save_tmp_jpg(img: Image.Image) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    f.close()
    img.convert("RGB").save(f.name, "JPEG", quality=95)
    return f.name


def _strategy_full_page_psm(img: Image.Image, psm: int, label: str) -> dict:
    path = _save_tmp_jpg(img)
    try:
        text = tesseract_ocr_page(path, lang="nor", psm=psm)
    finally:
        os.unlink(path)
    val, raw = _scan_label_line(text, label)
    return {
        "name": f"full_page_psm{psm}",
        "value": val,
        "raw": raw,
        "confidence": "med" if val is not None else "low",
    }


def _scan_label_line(text: str, label: str) -> tuple[int | None, str]:
    label_l = label.strip().lower()
    for line in text.splitlines():
        line_l = line.lstrip().lower()
        if line_l.startswith(label_l):
            after = line_l[len(label_l):]
            if not after or after[0] in "\t :.\u00a0,-" or after[0].isdigit():
                cols = [c.strip() for c in line[len(label):].split("\t") if c.strip()]
                nums = []
                for c in cols:
                    cleaned = c.replace(" ", "").replace("\u00a0", "").replace(",", "")
                    if len(cleaned) <= 4 and cleaned.isdigit():
                        continue
                    v = normalize_amount(c)
                    if v is not None:
                        nums.append(v)
                if len(nums) >= 2:
                    return int(nums[-2]), line
                if len(nums) == 1:
                    return int(nums[0]), line
                return None, line
    return None, ""


def _strategy_crop_value_cell(img: Image.Image, label: str) -> dict:
    """Find the matched-label line via TSV, crop just the rightmost numeric region,
    OCR with PSM 7 + numeric whitelist."""
    path = _save_tmp_jpg(img)
    try:
        words = tesseract_tsv(path, lang="nor", psm=6)
    finally:
        os.unlink(path)

    label_words = label.lower().split()
    if not label_words:
        return {"name": "crop_value_cell", "value": None, "raw": "", "confidence": "low"}

    line_buckets: dict[tuple, list] = {}
    for w in words:
        line_buckets.setdefault(w.line_key, []).append(w)

    target_line = None
    for key, ws in line_buckets.items():
        ws_sorted = sorted(ws, key=lambda w: w.left)
        joined = " ".join(w.text.lower() for w in ws_sorted[:6])
        if all(part in joined for part in label_words):
            target_line = ws_sorted
            break

    if target_line is None:
        return {"name": "crop_value_cell", "value": None, "raw": "", "confidence": "low"}

    digit_words = [w for w in target_line if any(ch.isdigit() for ch in w.text)]
    if len(digit_words) < 2:
        return {"name": "crop_value_cell", "value": None,
                "raw": " ".join(w.text for w in target_line), "confidence": "low"}

    digit_words = digit_words[-8:]
    if len(digit_words) >= 2:
        gaps = [(digit_words[i].left - digit_words[i - 1].right, i) for i in range(1, len(digit_words))]
        gaps.sort(key=lambda g: -g[0])
        boundary_idx = max(g[1] for g in gaps[:1])
        value_words = digit_words[:boundary_idx]
    else:
        value_words = digit_words

    if not value_words:
        return {"name": "crop_value_cell", "value": None, "raw": "", "confidence": "low"}

    x0 = min(w.left for w in value_words) - 5
    x1 = max(w.left + w.width for w in value_words) + 10
    y0 = min(w.top for w in value_words) - 5
    y1 = max(w.top + w.height for w in value_words) + 10
    crop = img.crop((max(x0, 0), max(y0, 0), x1, y1))

    crop_path = _save_tmp_jpg(crop)
    try:
        text = pytesseract.image_to_string(
            crop,
            lang="nor",
            config="--psm 7 -c tessedit_char_whitelist=0123456789-() "
        ).strip()
    finally:
        os.unlink(crop_path)

    v = normalize_amount(text)
    return {
        "name": "crop_value_cell",
        "value": int(v) if v is not None else None,
        "raw": text,
        "confidence": "high" if v is not None else "low",
    }


def _strategy_tsv_walk(img: Image.Image, label: str) -> dict:
    path = _save_tmp_jpg(img)
    try:
        words = tesseract_tsv(path, lang="nor", psm=6)
    finally:
        os.unlink(path)

    label_words = label.lower().split()
    line_buckets: dict[tuple, list] = {}
    for w in words:
        line_buckets.setdefault(w.line_key, []).append(w)

    for key, ws in line_buckets.items():
        ws_sorted = sorted(ws, key=lambda w: w.left)
        joined = " ".join(w.text.lower() for w in ws_sorted[:6])
        if not all(part in joined for part in label_words):
            continue
        digit_clusters = []
        cur: list = []
        prev_right = None
        for w in ws_sorted:
            if not any(ch.isdigit() or ch in "-l" for ch in w.text):
                if cur:
                    digit_clusters.append(cur)
                    cur = []
                prev_right = None
                continue
            if prev_right is not None and (w.left - prev_right) > 80:
                if cur:
                    digit_clusters.append(cur)
                    cur = []
            cur.append(w)
            prev_right = w.left + w.width
        if cur:
            digit_clusters.append(cur)

        cluster_values = []
        for cluster in digit_clusters:
            joined = " ".join(w.text for w in cluster)
            v = normalize_amount(joined)
            if v is not None:
                cluster_values.append((joined, int(v)))

        if len(cluster_values) >= 2:
            return {
                "name": "tsv_walk",
                "value": cluster_values[-2][1],
                "raw": str(cluster_values),
                "confidence": "high",
            }
        if len(cluster_values) == 1:
            return {
                "name": "tsv_walk",
                "value": cluster_values[0][1],
                "raw": str(cluster_values),
                "confidence": "med",
            }

    return {"name": "tsv_walk", "value": None, "raw": "", "confidence": "low"}


def deep_dive(pdf_bytes: bytes,
              page_idx: int,
              label: str,
              tesseract_v1_value: int | None,
              api_value: int) -> dict:
    """Run all deep-dive strategies on the disagreement page."""
    img_400 = _render_page(pdf_bytes, page_idx, dpi=400)

    strategies = []
    for psm in (6, 11, 4):
        try:
            strategies.append(_strategy_full_page_psm(img_400, psm, label))
        except Exception as e:
            strategies.append({"name": f"full_page_psm{psm}", "value": None,
                               "raw": f"exception: {e}", "confidence": "low"})
    try:
        strategies.append(_strategy_crop_value_cell(img_400, label))
    except Exception as e:
        strategies.append({"name": "crop_value_cell", "value": None,
                           "raw": f"exception: {e}", "confidence": "low"})
    try:
        strategies.append(_strategy_tsv_walk(img_400, label))
    except Exception as e:
        strategies.append({"name": "tsv_walk", "value": None,
                           "raw": f"exception: {e}", "confidence": "low"})

    values = [s["value"] for s in strategies if s["value"] is not None]
    api_int = int(api_value)
    tol = max(abs(api_int) * 0.01, 1000)

    matches_api = sum(1 for v in values if abs(v - api_int) <= tol)
    matches_tess = sum(1 for v in values if tesseract_v1_value is not None
                        and abs(v - tesseract_v1_value) <= max(abs(tesseract_v1_value) * 0.01, 1000))

    if matches_api >= 2 and matches_api > matches_tess:
        reconciled = api_int
        diagnosis = "ocr_misread_corrected"
    elif matches_tess >= 2 and matches_tess > matches_api:
        reconciled = tesseract_v1_value
        diagnosis = "api_disagrees_with_pdf"
    elif matches_api == matches_tess and matches_api > 0:
        reconciled = api_int
        diagnosis = "ambiguous_default_api"
    elif values:
        from collections import Counter
        most = Counter(values).most_common(1)[0]
        reconciled = most[0]
        if abs(most[0] - api_int) <= tol:
            diagnosis = "ocr_misread_corrected"
        else:
            diagnosis = "ambiguous_consensus_disagrees_api"
    else:
        reconciled = None
        diagnosis = "all_strategies_failed"

    return {
        "field_label": label,
        "page_idx": page_idx,
        "tesseract_v1_value": tesseract_v1_value,
        "api_value": api_int,
        "strategies": strategies,
        "reconciled_value": reconciled,
        "diagnosis": diagnosis,
    }
