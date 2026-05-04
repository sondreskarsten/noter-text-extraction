"""End-to-end pipeline: regnskap PDF -> noter_v5b-compatible JSON.

Output schema mirrors gs://sondre_brreg_data/raw/noter_extraction_2025/extractions/noter_v5b/
so the downstream noter-parser can consume it transparently."""
import datetime
import hashlib
import json
from pathlib import Path
from typing import Optional

from google.cloud import storage

from .amount_normalizer import detect_scale
from .config import DATA_BUCKET, TESSERACT_PREFIX
from .kv_extractor import extract_kv
from .note_segmenter import segment_notes
from .ocr import ocr_with_backend, OcrBackend
from .pdf_loader import gcs_client, prepare_pages


def extract_one(
    orgnr: str,
    year: int,
    dpi: int = 200,
    ocr_backend: Optional[OcrBackend] = None,
    upload: bool = True,
) -> dict:
    """Run the full pipeline for one (orgnr, year). Returns the JSON payload
    (and optionally uploads it to GCS)."""
    # 1. Download + rasterize
    prep = prepare_pages(orgnr, year, dpi=dpi)
    pdf_hash = _file_hash(prep["pdf_path"])

    # 2. OCR
    pages_text = ocr_with_backend(prep["page_jpgs"], backend=ocr_backend)

    # 3. Segment into notes
    notes = segment_notes(pages_text)

    # 4. Detect scale (in MNOK / tNOK / hele tusen?) per note
    for n in notes:
        scale = detect_scale(n["raw_text"][:300])  # check note preamble
        n["scale_inferred"] = scale
        n["raw_amounts"] = extract_kv(n["raw_text"])
        # Apply scale to all extracted values (caller owns this — we record it)
        if scale != 1.0:
            n["raw_amounts"] = {k: v * scale for k, v in n["raw_amounts"].items()}
        n["type"] = "table" if n["raw_amounts"] else "narrative"
        n["has_table"] = bool(n["raw_amounts"])

    # 5. Build noter_v5b-compatible payload
    payload = {
        "orgnr": orgnr,
        "year": year,
        "pdf_hash": pdf_hash,
        "prompt_name": "tesseract_v1",
        "model": "tesseract-5.3.4-nor",
        "input_mode": "rasterize_then_ocr",
        "media_resolution": f"DPI_{dpi}",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "n_pages_sent": prep["n_pages"],
        "page_numbers": list(range(1, prep["n_pages"] + 1)),
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": 0.0,
        "elapsed_s": None,
        "finish_reason": "STOP",
        "status": "ok",
        "noter": notes,
        "n_notes": len(notes),
    }

    # 6. Upload
    if upload:
        path = f"{TESSERACT_PREFIX}/{orgnr}_{year}.json"
        gcs_client().bucket(DATA_BUCKET).blob(path).upload_from_string(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )
        payload["_uploaded_to"] = f"gs://{DATA_BUCKET}/{path}"

    return payload


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]
