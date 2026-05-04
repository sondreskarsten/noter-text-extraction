"""OCR layer with spatial layout preservation.

The naive `tesseract image -` text output collapses column-aligned values
into ambiguous space-separated tokens — e.g. '522 504 331 492 928 614'
becomes a single 18-digit run that no Norwegian-amount-aware regex can
unambiguously split. The fix is to use Tesseract's TSV output, which
includes per-word bounding boxes, and reconstruct line text by detecting
'column gaps' (gaps significantly wider than typical intra-number gaps).

Adjacent column values are separated by '\\t' so downstream extractors
can split on tab to identify column boundaries unambiguously."""
import csv
import io
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Word:
    block_id: int
    par_id: int
    line_id: int
    word_id: int
    left: int
    top: int
    width: int
    height: int
    conf: float
    text: str

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def line_key(self) -> tuple:
        return (self.block_id, self.par_id, self.line_id)


def tesseract_tsv(jpg_path: str, lang: str = "nor", psm: int = 6) -> list[Word]:
    """Run Tesseract with TSV output. Returns list of Word objects (level=5)."""
    res = subprocess.run(
        ["tesseract", jpg_path, "-", "-l", lang, "--psm", str(psm), "tsv"],
        capture_output=True, text=True, check=True,
    )
    reader = csv.DictReader(io.StringIO(res.stdout), delimiter="\t")
    words = []
    for row in reader:
        try:
            level = int(row.get("level", 0))
        except (ValueError, TypeError):
            continue
        if level != 5:
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            words.append(Word(
                block_id=int(row["block_num"]),
                par_id=int(row["par_num"]),
                line_id=int(row["line_num"]),
                word_id=int(row["word_num"]),
                left=int(row["left"]),
                top=int(row["top"]),
                width=int(row["width"]),
                height=int(row["height"]),
                conf=float(row.get("conf", 0)),
                text=text,
            ))
        except (ValueError, KeyError):
            continue
    return words


def reconstruct_text(words: list[Word]) -> str:
    """Reconstruct page text from TSV words. Lines are identified uniquely
    by (block_num, par_num, line_num) — line_num restarts inside each block."""
    if not words:
        return ""
    lines: dict[tuple, list[Word]] = {}
    for w in words:
        lines.setdefault(w.line_key, []).append(w)
    # Sort lines by their topmost word's y-coordinate to get reading order
    line_order = sorted(lines.keys(), key=lambda k: min(w.top for w in lines[k]))
    out = []
    for k in line_order:
        line_words = sorted(lines[k], key=lambda w: w.left)
        if line_words:
            out.append(_reconstruct_line(line_words))
    return "\n".join(out)


def _reconstruct_line(words: list[Word]) -> str:
    """Build a single line, inserting tabs where the gap between adjacent
    words is anomalously wide compared to the line's typical intra-word gap.
    Threshold: max(median_gap * 2, 30 pixels)."""
    if len(words) <= 1:
        return " ".join(w.text for w in words)
    gaps = [max(0, words[i].left - words[i - 1].right) for i in range(1, len(words))]
    sorted_gaps = sorted(gaps)
    median_gap = sorted_gaps[len(sorted_gaps) // 2]
    threshold = max(median_gap * 2, 30)
    parts = [words[0].text]
    for i in range(1, len(words)):
        sep = "\t" if gaps[i - 1] > threshold else " "
        parts.append(sep + words[i].text)
    return "".join(parts)


def tesseract_ocr_page(jpg_path: str, lang: str = "nor", psm: int = 6) -> str:
    """OCR a single JPG using TSV-based column-aware text reconstruction."""
    words = tesseract_tsv(jpg_path, lang=lang, psm=psm)
    return reconstruct_text(words)


def tesseract_ocr_pages(
    jpg_paths: list[str], lang: str = "nor", psm: int = 6, max_workers: int = 4
) -> list[str]:
    """OCR every page in parallel."""
    cores = os.cpu_count() or 4
    workers = min(max_workers, max(2, cores))

    def _ocr(p: str) -> str:
        return tesseract_ocr_page(p, lang=lang, psm=psm)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_ocr, jpg_paths))


OcrBackend = Callable[[list[str]], list[str]]


def ocr_with_backend(jpg_paths: list[str], backend: Optional[OcrBackend] = None) -> list[str]:
    if backend is None:
        backend = tesseract_ocr_pages
    return backend(jpg_paths)
