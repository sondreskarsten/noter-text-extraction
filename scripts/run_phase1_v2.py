"""Phase 1 runner v2: idempotent, GCS-incremental.

Skips (orgnr, year) pairs whose tesseract_v1_full JSON already exists.
Writes each record immediately on completion so partial progress survives.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import fitz
from google.cloud import storage
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from noter_text_extraction.full_pdf_orchestrator import extract_full_pdf
from noter_text_extraction.nokkeltall_validator import validate_against_nokkeltall

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

PDF_BUCKET = "brreg-regnskap"
OUT_BUCKET = "sondre_brreg_data"
OUT_PREFIX_RECORDS = "raw/noter_extraction_2025/extractions/tesseract_v1_full"
OUT_PREFIX_META = "raw/noter_extraction_2025/_meta"
OUT_PREFIX_RASTERS = "raw/noter_extraction_2025/_meta/raster_pages"


def _record_exists(client, orgnr: str, year: int) -> bool:
    return client.bucket(OUT_BUCKET).blob(
        f"{OUT_PREFIX_RECORDS}/{orgnr}_{year}.json"
    ).exists()


def _fetch_pdf_and_api(client, orgnr: str, year: int):
    src = client.bucket(PDF_BUCKET)
    pdf_blob = None
    api_blob = None
    for suffix in ("", "_v2", "_v3"):
        pb = src.blob(f"regnskap/{orgnr}/aarsregnskap_{year}{suffix}.pdf")
        ab = src.blob(f"regnskap/{orgnr}/regnskap_{year}{suffix}.json")
        if pb.exists() and ab.exists():
            pdf_blob = pb
            api_blob = ab
            break
    if pdf_blob is None or api_blob is None:
        return None, {"error": "files_missing"}
    pdf_bytes = pdf_blob.download_as_bytes()
    api_data = json.loads(api_blob.download_as_text())
    if isinstance(api_data, list):
        api_entry = next(
            (e for e in api_data if e["regnskapsperiode"]["tilDato"].startswith(str(year))),
            None
        )
        if api_entry is None and api_data:
            api_entry = api_data[0]
        if api_entry is None:
            return None, {"error": "api_no_entry"}
    else:
        api_entry = api_data
    return pdf_bytes, api_entry


def _render_page(pdf_bytes: bytes, page_idx: int, dpi: int = 200) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_idx]
    if abs(page.rect.width - 1728) < 5:
        imgs = page.get_images(full=True)
        if imgs:
            xref = imgs[0][0]
            base = doc.extract_image(xref)
            doc.close()
            return base["image"]
    pix = page.get_pixmap(dpi=dpi)
    png = pix.tobytes("png")
    doc.close()
    return png


def process_one(orgnr: str, year: int, dpi: int = 300,
                render_disagreement_pages: bool = True,
                skip_existing: bool = True) -> dict:
    summary = {
        "orgnr": orgnr, "year": year,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    client = storage.Client()
    try:
        if skip_existing and _record_exists(client, orgnr, year):
            summary["status"] = "skipped_exists"
            return summary

        pdf_bytes, api_entry = _fetch_pdf_and_api(client, orgnr, year)
        if pdf_bytes is None:
            summary["status"] = api_entry.get("error", "fetch_failed")
            return summary

        record = extract_full_pdf(pdf_bytes, orgnr=orgnr, year=year, dpi=dpi, api_entry=api_entry)

        if record.get("status") == "skipped_legacy_format":
            summary["status"] = "skipped_legacy_format"
            summary["n_brreg"] = record["manifest"]["n_brreg"]
            client.bucket(OUT_BUCKET).blob(
                f"{OUT_PREFIX_RECORDS}/{orgnr}_{year}.json"
            ).upload_from_string(
                json.dumps(record, ensure_ascii=False, default=str),
                content_type="application/json",
            )
            return summary

        validation = validate_against_nokkeltall(record, api_entry)
        record["validation"] = validation

        # Deep-dive for each numerical disagreement
        if validation["disagreements"]:
            from noter_text_extraction.deep_dive import deep_dive
            from noter_text_extraction.canonical_schema import get_field
            deep_dive_results = {}
            for fname in validation["disagreements"]:
                spec = get_field(fname)
                if spec is None or spec.layout not in ("resultat", "balanse"):
                    continue
                hit = record["primary"].get(fname)
                if isinstance(hit, dict) and "page_idx" in hit:
                    page_pdf_idx = hit["page_idx"] + 1
                    label = hit.get("label_matched") or spec.norwegian_labels[0]
                    tess_v = hit.get("value")
                else:
                    # Field missing from extraction — try every BRREG page
                    page_pdf_idx = 1
                    label = spec.norwegian_labels[0]
                    tess_v = None
                api_v_raw = next((c["api"] for c in validation["checks"]
                                  if c["field"] == fname), None)
                if api_v_raw is None:
                    continue
                try:
                    dd = deep_dive(
                        pdf_bytes=pdf_bytes,
                        page_idx=page_pdf_idx,
                        label=label,
                        tesseract_v1_value=int(tess_v) if tess_v is not None else None,
                        api_value=int(api_v_raw),
                    )
                    deep_dive_results[fname] = dd
                    if dd.get("reconciled_value") is not None:
                        if not isinstance(record["primary"].get(fname), dict):
                            record["primary"][fname] = {}
                        record["primary"][fname]["reconciled_value"] = dd["reconciled_value"]
                        record["primary"][fname]["deep_dive_diagnosis"] = dd["diagnosis"]
                except Exception as e:
                    deep_dive_results[fname] = {"error": str(e)}
            if deep_dive_results:
                record["deep_dive"] = deep_dive_results
            # Re-validate against API using reconciled values
            from noter_text_extraction.canonical_schema import flatten_api
            api_flat = flatten_api(api_entry)
            from noter_text_extraction.nokkeltall_validator import _tolerance_ok
            n_recovered = 0
            for fname, dd in deep_dive_results.items():
                rec_val = dd.get("reconciled_value") if isinstance(dd, dict) else None
                if rec_val is None:
                    continue
                spec = get_field(fname)
                if spec is None:
                    continue
                ok, _ = _tolerance_ok(rec_val, api_flat.get(fname), spec.tolerance)
                if ok:
                    n_recovered += 1
            record["validation"]["n_recovered_by_deep_dive"] = n_recovered

        rasters: dict[int, str] = {}
        if render_disagreement_pages and validation["disagreements"]:
            pages_to_render: set[int] = set()
            for fname in validation["disagreements"]:
                hit = record["primary"].get(fname)
                if isinstance(hit, dict) and "page_idx" in hit:
                    pages_to_render.add(hit["page_idx"] + 1)
            if validation["n_missing_extract"]:
                for i in range(1, record["manifest"]["n_brreg"]):
                    pages_to_render.add(i)

            for pidx in sorted(pages_to_render):
                try:
                    png = _render_page(pdf_bytes, pidx, dpi=dpi)
                    name = f"{OUT_PREFIX_RASTERS}/{orgnr}_{year}_p{pidx + 1}.png"
                    client.bucket(OUT_BUCKET).blob(name).upload_from_string(
                        png, content_type="image/png"
                    )
                    rasters[pidx + 1] = f"gs://{OUT_BUCKET}/{name}"
                except Exception as e:
                    log.warning(f"raster {orgnr} p{pidx+1} failed: {e}")
            record["disagreement_rasters"] = rasters

        client.bucket(OUT_BUCKET).blob(
            f"{OUT_PREFIX_RECORDS}/{orgnr}_{year}.json"
        ).upload_from_string(
            json.dumps(record, ensure_ascii=False, default=str),
            content_type="application/json",
        )

        summary["status"] = "ok"
        summary["record_uri"] = f"gs://{OUT_BUCKET}/{OUT_PREFIX_RECORDS}/{orgnr}_{year}.json"
        summary["n_pass"] = validation["n_pass"]
        summary["n_fail"] = validation["n_fail"]
        summary["n_missing_extract"] = validation["n_missing_extract"]
        summary["disagreements"] = validation["disagreements"]
        summary["n_rasters"] = len(rasters)
        summary["smaa_foretak"] = (record.get("generell_info") or {}).get("smaa_foretak")
        summary["regnskapstype"] = (record.get("api_entry_used") or {}).get("regnskapstype") if record.get("api_entry_used") else None

    except Exception as e:
        summary["status"] = "exception"
        summary["error"] = str(e)
        summary["traceback"] = traceback.format_exc()[-1500:]

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def run(sample_path: str, workers: int, tag: str, dpi: int = 300,
        skip_existing: bool = True) -> dict:
    sample = json.load(open(sample_path))
    log.info(f"Run tag={tag} | n_input={len(sample)} | workers={workers} | dpi={dpi}")

    summaries: list[dict] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_one, o, y, dpi, True, skip_existing): (o, y)
                for o, y in sample}
        for i, fut in enumerate(as_completed(futs)):
            try:
                s = fut.result()
            except Exception as e:
                o, y = futs[fut]
                s = {"orgnr": o, "year": y, "status": "exception", "error": str(e)}
            summaries.append(s)
            elapsed = time.time() - t0
            log.info(
                f"  [{i+1}/{len(sample)}] {s['orgnr']} {s['year']} "
                f"st={s['status']:<22} "
                f"p={s.get('n_pass',0)}/f={s.get('n_fail',0)}/m={s.get('n_missing_extract',0)} "
                f"({elapsed:.0f}s)"
            )

    keys = sorted({k for s in summaries for k in s.keys()})
    csv_buf = io.StringIO()
    writer = csv.DictWriter(csv_buf, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    for s in summaries:
        s_copy = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in s.items()}
        writer.writerow(s_copy)

    client = storage.Client()
    client.bucket(OUT_BUCKET).blob(
        f"{OUT_PREFIX_META}/run_summary_{tag}.csv"
    ).upload_from_string(csv_buf.getvalue(), content_type="text/csv")

    n_ok = sum(1 for s in summaries if s["status"] == "ok")
    n_skipped_legacy = sum(1 for s in summaries if s["status"] == "skipped_legacy_format")
    n_skipped_exists = sum(1 for s in summaries if s["status"] == "skipped_exists")
    n_failed = sum(1 for s in summaries if s["status"] not in ("ok", "skipped_legacy_format", "skipped_exists"))
    total_pass = sum(s.get("n_pass", 0) for s in summaries)
    total_fail = sum(s.get("n_fail", 0) for s in summaries)
    total_miss = sum(s.get("n_missing_extract", 0) for s in summaries)

    aggregate = {
        "tag": tag, "n_input": len(sample),
        "n_ok": n_ok, "n_skipped_legacy": n_skipped_legacy,
        "n_skipped_exists": n_skipped_exists, "n_failed": n_failed,
        "total_field_pass": total_pass, "total_field_fail": total_fail,
        "total_field_missing_extract": total_miss,
        "field_pass_rate": (total_pass / max(1, total_pass + total_fail + total_miss)),
    }
    log.info(f"\nDONE: {json.dumps(aggregate, indent=2)}")
    client.bucket(OUT_BUCKET).blob(
        f"{OUT_PREFIX_META}/aggregate_{tag}.json"
    ).upload_from_string(json.dumps(aggregate, indent=2), content_type="application/json")

    return aggregate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default="/home/claude/work/sample/sample_100.json")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--no-skip", action="store_true")
    args = ap.parse_args()
    if args.tag is None:
        args.tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    run(args.sample, args.workers, args.tag, dpi=args.dpi,
        skip_existing=not args.no_skip)


if __name__ == "__main__":
    main()
