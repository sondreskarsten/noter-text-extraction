"""Full PDF orchestrator with setup-aware parser dispatch."""

from __future__ import annotations

import io
from datetime import datetime, timezone

import fitz
from PIL import Image

from .classifier import build_manifest
from .extractors.generell_info import extract_generell_info
from .extractors.primary_statement import extract_primary_statements
from .extractors.primary_statement_konsern import extract_primary_statements_konsern
from .setup_detector import detect_setup


BRREG_WIDTH = 1728
DPI_DEFAULT = 300


def render_page(doc: fitz.Document, page_idx: int, dpi: int = DPI_DEFAULT) -> Image.Image:
    """Return page image. For BRREG-rendered pages (1728-wide), extract the
    embedded PNG at native resolution to avoid 4x memory upscaling. For
    company pages with no embedded image at native resolution, fall back to
    pixmap rendering at the requested DPI.

    Memory: native extraction = ~12 MB per page; dpi=300 pixmap = ~200 MB.
    """
    page = doc[page_idx]
    if abs(page.rect.width - 1728) < 5:
        imgs = page.get_images(full=True)
        if imgs:
            xref = imgs[0][0]
            base = doc.extract_image(xref)
            return Image.open(io.BytesIO(base["image"]))
    pix = page.get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def is_modern_format(manifest: dict) -> bool:
    return manifest["split"]["n_brreg"] >= 1


def extract_full_pdf(pdf_bytes: bytes,
                     orgnr: str | None = None,
                     year: int | None = None,
                     api_entry: dict | None = None,
                     dpi: int = DPI_DEFAULT) -> dict:
    manifest = build_manifest(pdf_bytes, orgnr=orgnr, year=year)

    if not is_modern_format(manifest):
        return {
            "orgnr": orgnr,
            "year": year,
            "status": "skipped_legacy_format",
            "manifest": {
                "n_brreg": manifest["split"]["n_brreg"],
                "n_company": manifest["split"]["n_company"],
                "total_pages": manifest["document"]["total_pages"],
            },
        }

    n_brreg = manifest["split"]["n_brreg"]
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    p1 = render_page(doc, 0, dpi=dpi)

    gen = extract_generell_info(p1)
    gen.pop("_ocr_text", None)
    del p1

    setup = detect_setup(api_entry, manifest, gen)

    # Render BRREG pages 2..n one at a time and OCR immediately to bound memory
    import os as _os
    import tempfile as _tempfile
    from .ocr import tesseract_ocr_page as _tesseract_ocr_page
    page_texts: list[str] = []
    for i in range(1, n_brreg):
        img = render_page(doc, i, dpi=dpi)
        with _tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            path = f.name
        try:
            img.convert("RGB").save(path, "JPEG", quality=92)
            del img
            text = _tesseract_ocr_page(path, lang="nor", psm=6)
            page_texts.append(text)
        finally:
            _os.unlink(path)
    doc.close()

    if setup in ("store_konsern", "smaa_konsern"):
        from .extractors.primary_statement_konsern import _scan_for_field_konsern as _scan_konsern
        from .canonical_schema import RESULTAT_FIELDS as _RF, BALANSE_FIELDS as _BF
        primary_fields = {}
        for spec in _RF + _BF:
            hit = _scan_konsern(spec, page_texts)
            if hit is not None:
                primary_fields[spec.canonical] = hit
    else:
        from .extractors.primary_statement import _scan_for_field as _scan
        from .canonical_schema import RESULTAT_FIELDS as _RF, BALANSE_FIELDS as _BF
        primary_fields = {}
        for spec in _RF + _BF:
            hit = _scan(spec, page_texts)
            if hit is not None:
                primary_fields[spec.canonical] = hit

    record = {
        "orgnr": orgnr,
        "year": year,
        "status": "ok",
        "modern_format": True,
        "setup": setup,
        "manifest": {
            "n_brreg": manifest["split"]["n_brreg"],
            "n_company": manifest["split"]["n_company"],
            "total_pages": manifest["document"]["total_pages"],
            "platform": manifest.get("platform", {}).get("id"),
            "konsern_evidence": manifest.get("konsern", {}),
        },
        "generell_info": gen,
        "primary": primary_fields,
        "extraction_meta": {
            "pdf_sha256_prefix": manifest["document"].get("pdf_sha256_prefix") or manifest["document"].get("pdf_hash"),
            "classifier_version": manifest["classifier"]["version"],
            "dpi": dpi,
            "n_pages_ocrd": 1 + len(page_texts),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    return record
