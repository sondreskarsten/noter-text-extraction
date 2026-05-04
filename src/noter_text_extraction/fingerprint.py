"""Template fingerprinting for Norwegian årsregnskap PDFs.

Three-signal approach (proven in prior experiments):
  1. Perceptual hashing of footer zone (bottom 12%) — clusters by visual template
  2. OCR text fingerprinting of header/footer zones — deterministic leverandør ID
  3. Silhouette features (32x32 grayscale) — page-type classification

BRREG rasterizes all PDFs (destroys /Producer /Creator metadata), so
metadata-based detection is impossible. All signals are image-based.

Usage:
    from noter_text_extraction.fingerprint import fingerprint_pdf, classify_leverandor

    fp = fingerprint_pdf("path/to/regnskap.pdf")
    leverandor = classify_leverandor(fp)
    # -> {"leverandor": "visma_finale", "confidence": "high", "signals": {...}}
"""
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass
class PageFingerprint:
    page_idx: int
    source: str
    page_type: Optional[str]
    phash_footer: Optional[str]
    ahash_footer: Optional[str]
    dhash_footer: Optional[str]
    phash_header: Optional[str]
    footer_text: Optional[str]
    header_text: Optional[str]
    silhouette: Optional[list]
    width: int = 0
    height: int = 0


@dataclass
class PDFFingerprint:
    path: str
    n_pages: int
    brreg_boundary: Optional[int]
    pages: list[PageFingerprint] = field(default_factory=list)
    leverandor: Optional[str] = None
    leverandor_confidence: Optional[str] = None
    template_cluster: Optional[int] = None
    signals: dict = field(default_factory=dict)


def _render_page(doc, page_idx: int, dpi: int = 72):
    page = doc[page_idx]
    mat = __import__("fitz").Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=__import__("fitz").csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)


def _crop_zone(img: np.ndarray, zone: str) -> np.ndarray:
    h = img.shape[0]
    if zone == "header":
        return img[:int(h * 0.15), :]
    elif zone == "footer":
        return img[int(h * 0.88):, :]
    return img


def _compute_hashes(img_array: np.ndarray) -> dict:
    try:
        import imagehash
        from PIL import Image
        pil_img = Image.fromarray(img_array)
        return {
            "phash": str(imagehash.phash(pil_img, hash_size=8)),
            "ahash": str(imagehash.average_hash(pil_img, hash_size=8)),
            "dhash": str(imagehash.dhash(pil_img, hash_size=8)),
        }
    except ImportError:
        h = hashlib.md5(img_array.tobytes()).hexdigest()[:16]
        return {"phash": h, "ahash": h, "dhash": h}


def _ocr_zone(img_array: np.ndarray) -> str:
    try:
        import pytesseract
        from PIL import Image
        pil_img = Image.fromarray(img_array)
        text = pytesseract.image_to_string(pil_img, lang="nor", config="--psm 6")
        return text.strip()
    except Exception:
        return ""


def _compute_silhouette(img_array: np.ndarray, size: int = 32) -> list:
    from PIL import Image
    pil_img = Image.fromarray(img_array)
    small = pil_img.resize((size, size), Image.LANCZOS)
    return np.array(small).flatten().tolist()


def _detect_brreg_boundary(doc) -> Optional[int]:
    import fitz
    for i in range(doc.page_count):
        pix = doc[i].get_pixmap(colorspace=fitz.csGRAY)
        w = pix.width
        if w == 1728:
            continue
        return i
    return None


LEVERANDOR_PATTERNS = [
    {
        "name": "visma_finale",
        "footer_re": re.compile(r"(?:SIDE|Side)\s+\d+", re.IGNORECASE),
        "header_re": None,
        "confidence": "high",
    },
    {
        "name": "fiken_tripletex",
        "footer_re": None,
        "header_re": re.compile(r"Noter\s+\d{4}\s*\n", re.IGNORECASE),
        "confidence": "high",
    },
    {
        "name": "poweroffice",
        "footer_re": re.compile(r"(?:PowerOffice|Powered by PowerOffice)", re.IGNORECASE),
        "header_re": None,
        "confidence": "high",
    },
    {
        "name": "conta",
        "footer_re": re.compile(r"conta\.no|Conta AS", re.IGNORECASE),
        "header_re": None,
        "confidence": "medium",
    },
    {
        "name": "24sevenoffice",
        "footer_re": re.compile(r"24SevenOffice|24seven", re.IGNORECASE),
        "header_re": None,
        "confidence": "medium",
    },
]

TEMPLATE_UNKNOWN = "unknown"


def classify_leverandor_from_text(header_text: str, footer_text: str) -> dict:
    combined = f"{header_text}\n{footer_text}"
    for pattern in LEVERANDOR_PATTERNS:
        if pattern["footer_re"] and pattern["footer_re"].search(footer_text):
            return {"leverandor": pattern["name"], "confidence": pattern["confidence"],
                    "match_type": "footer_regex"}
        if pattern["header_re"] and pattern["header_re"].search(header_text):
            return {"leverandor": pattern["name"], "confidence": pattern["confidence"],
                    "match_type": "header_regex"}
    return {"leverandor": TEMPLATE_UNKNOWN, "confidence": "none", "match_type": "none"}


def fingerprint_page(doc, page_idx: int, ocr: bool = True) -> PageFingerprint:
    import fitz
    pix = doc[page_idx].get_pixmap(colorspace=fitz.csGRAY)
    w, h = pix.width, pix.height

    source = "brreg" if w == 1728 else "company"

    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w)
    footer = _crop_zone(img, "footer")
    header = _crop_zone(img, "header")

    footer_hashes = _compute_hashes(footer)
    header_hashes = _compute_hashes(header)

    footer_text = _ocr_zone(footer) if ocr else ""
    header_text = _ocr_zone(header) if ocr else ""

    silhouette = _compute_silhouette(img, size=32)

    page_type = None
    if source == "brreg":
        brreg_pages = doc.page_count
        if page_idx == 0:
            page_type = "generell_info"
        elif page_idx == 1:
            page_type = "resultatregnskap"
        elif page_idx == 2:
            page_type = "balanse"

    return PageFingerprint(
        page_idx=page_idx,
        source=source,
        page_type=page_type,
        phash_footer=footer_hashes["phash"],
        ahash_footer=footer_hashes["ahash"],
        dhash_footer=footer_hashes["dhash"],
        phash_header=header_hashes["phash"],
        footer_text=footer_text,
        header_text=header_text,
        silhouette=silhouette,
        width=w,
        height=h,
    )


def fingerprint_pdf(path: str, ocr: bool = True,
                    max_company_pages: int = 5) -> PDFFingerprint:
    import fitz
    doc = fitz.open(path)
    n_pages = doc.page_count

    brreg_boundary = _detect_brreg_boundary(doc)
    company_start = brreg_boundary if brreg_boundary is not None else 0

    pages = []
    target_pages = list(range(min(3, n_pages)))
    if company_start < n_pages:
        company_pages = list(range(company_start, min(company_start + max_company_pages, n_pages)))
        target_pages = sorted(set(target_pages + company_pages))

    for idx in target_pages:
        fp = fingerprint_page(doc, idx, ocr=ocr)
        pages.append(fp)

    company_fps = [p for p in pages if p.source == "company"]
    leverandor_result = {"leverandor": TEMPLATE_UNKNOWN, "confidence": "none"}
    if company_fps:
        target = company_fps[1] if len(company_fps) > 1 else company_fps[0]
        leverandor_result = classify_leverandor_from_text(
            target.header_text or "", target.footer_text or ""
        )

    doc.close()

    return PDFFingerprint(
        path=path,
        n_pages=n_pages,
        brreg_boundary=brreg_boundary,
        pages=pages,
        leverandor=leverandor_result["leverandor"],
        leverandor_confidence=leverandor_result["confidence"],
        signals=leverandor_result,
    )


def cluster_fingerprints(fingerprints: list[PDFFingerprint],
                         hash_field: str = "phash_footer",
                         max_distance: int = 5) -> dict[int, list[str]]:
    from collections import defaultdict

    company_hashes = []
    for fp in fingerprints:
        company_fps = [p for p in fp.pages if p.source == "company"]
        if company_fps:
            target = company_fps[1] if len(company_fps) > 1 else company_fps[0]
            h = getattr(target, hash_field, None)
            if h:
                company_hashes.append((fp.path, h))

    try:
        import imagehash
        hash_objs = [(path, imagehash.hex_to_hash(h)) for path, h in company_hashes]
    except ImportError:
        clusters = defaultdict(list)
        for i, (path, h) in enumerate(company_hashes):
            clusters[i].append(path)
        return dict(clusters)

    assigned = {}
    cluster_id = 0
    centroids = []

    for path, h in hash_objs:
        found = False
        for cid, centroid in centroids:
            if h - centroid <= max_distance:
                assigned[path] = cid
                found = True
                break
        if not found:
            assigned[path] = cluster_id
            centroids.append((cluster_id, h))
            cluster_id += 1

    clusters = defaultdict(list)
    for path, cid in assigned.items():
        clusters[cid].append(path)

    return dict(clusters)


def fingerprint_batch(pdf_paths: list[str], ocr: bool = True,
                      max_company_pages: int = 3) -> list[PDFFingerprint]:
    results = []
    for i, path in enumerate(pdf_paths):
        try:
            fp = fingerprint_pdf(path, ocr=ocr, max_company_pages=max_company_pages)
            results.append(fp)
        except Exception as e:
            results.append(PDFFingerprint(
                path=path, n_pages=0, brreg_boundary=None,
                leverandor="error", leverandor_confidence="none",
                signals={"error": str(e)},
            ))
        if (i + 1) % 50 == 0:
            print(f"  fingerprinted {i+1}/{len(pdf_paths)}")
    return results
