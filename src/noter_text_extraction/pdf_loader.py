"""Download regnskap PDFs from gs://brreg-regnskap and rasterize to JPGs."""
import os
import subprocess
from pathlib import Path
from typing import Optional

from google.cloud import storage

from .config import PDF_BUCKET, PDF_PREFIX, WORK_DIR


_client: Optional[storage.Client] = None


def gcs_client() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def download_pdf(orgnr: str, year: int, dest: Optional[str] = None) -> str:
    """Download regnskap PDF for one (orgnr, year). Tries
    aarsregnskap_YEAR.pdf, then aarsregnskap_YEAR_v*.pdf variants."""
    work = Path(WORK_DIR) / f"{orgnr}_{year}"
    work.mkdir(parents=True, exist_ok=True)
    if dest is None:
        dest = str(work / "source.pdf")
    bkt = gcs_client().bucket(PDF_BUCKET)

    # Direct match first
    blob = bkt.blob(f"{PDF_PREFIX}/{orgnr}/aarsregnskap_{year}.pdf")
    if blob.exists():
        blob.download_to_filename(dest)
        return dest

    # Versioned variants
    candidates = sorted(
        bkt.list_blobs(prefix=f"{PDF_PREFIX}/{orgnr}/aarsregnskap_{year}_"),
        key=lambda b: b.name,
    )
    if candidates:
        candidates[-1].download_to_filename(dest)  # take latest version
        return dest

    raise FileNotFoundError(
        f"No PDF found for orgnr={orgnr} year={year} in gs://{PDF_BUCKET}/{PDF_PREFIX}/{orgnr}/"
    )


def rasterize_pdf(pdf_path: str, out_dir: str, dpi: int = 200) -> list[str]:
    """Render every page of `pdf_path` to JPGs in `out_dir`. Returns sorted
    list of page paths."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["pdftoppm", "-jpeg", "-r", str(dpi), pdf_path, f"{out_dir}/page"],
        check=True,
        capture_output=True,
    )
    return sorted(str(p) for p in Path(out_dir).glob("page-*.jpg"))


def page_count(pdf_path: str) -> int:
    res = subprocess.run(
        ["pdfinfo", pdf_path], capture_output=True, text=True, check=True
    )
    for line in res.stdout.split("\n"):
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    return 0


def prepare_pages(orgnr: str, year: int, dpi: int = 200) -> dict:
    """Full prep: download PDF + rasterize every page. Returns dict with
    pdf_path, out_dir, n_pages, page_jpgs."""
    work = Path(WORK_DIR) / f"{orgnr}_{year}"
    work.mkdir(parents=True, exist_ok=True)
    pdf_path = str(work / "source.pdf")
    if not os.path.exists(pdf_path):
        download_pdf(orgnr, year, pdf_path)
    n_pages = page_count(pdf_path)
    page_jpgs = rasterize_pdf(pdf_path, str(work), dpi=dpi)
    return {
        "orgnr": orgnr,
        "year": year,
        "pdf_path": pdf_path,
        "out_dir": str(work),
        "n_pages": n_pages,
        "page_jpgs": page_jpgs,
    }
