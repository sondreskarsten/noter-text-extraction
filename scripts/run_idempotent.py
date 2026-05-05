"""Idempotent batch runner: process orgnrs, push records to GCS as they complete.

Idempotency: before processing, checks whether the output JSON already exists in GCS.
If so, skips. This means re-running picks up where it left off.

Per (orgnr, year):
    1. Check if gs://sondre_brreg_data/raw/noter_extraction_2025/extractions/tesseract_v1_full/{orgnr}_{year}.json exists
    2. If yes: skip
    3. Else: download PDF + nokkeltall, extract, validate, write to GCS

Incremental writing:
    - Per-orgnr record written immediately on completion
    - Disagreements appended to a per-run NDJSON log on GCS (one line per fail)
    - Run summary updated every 10 firms

Usage:
    python3 -u scripts/run_phase1_idempotent.py --tag <run_tag> --workers 4
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from google.cloud import storage

from noter_text_extraction.full_pdf_orchestrator import extract_full_pdf
from noter_text_extraction.nokkeltall_validator import validate_against_nokkeltall


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

PDF_BUCKET = "brreg-regnskap"
OUT_BUCKET = "sondre_brreg_data"
OUT_PREFIX_RECORDS = "raw/noter_extraction_2025/extractions/tesseract_v1_full"
OUT_PREFIX_META = "raw/noter_extraction_2025/_meta"


def _record_uri(orgnr: str, year: int) -> str:
    return f"{OUT_PREFIX_RECORDS}/{orgnr}_{year}.json"


def _record_exists(out_bkt, orgnr: str, year: int) -> bool:
    return out_bkt.blob(_record_uri(orgnr, year)).exists()


def process_one(orgnr: str, year: int, run_tag: str,
                pdf_bkt, out_bkt) -> dict:
    t0 = time.time()
    summary = {
        "orgnr": orgnr, "year": year, "run_tag": run_tag,
        "status": None, "elapsed": None,
        "n_pass": 0, "n_fail": 0, "n_missing_extract": 0,
        "n_missing_api": 0, "disagreements": [],
        "setup": None, "error": None,
    }
    try:
        if _record_exists(out_bkt, orgnr, year):
            summary["status"] = "already_exists"
            summary["elapsed"] = round(time.time() - t0, 2)
            return summary

        # Find PDF (try unsuffixed, then _v2, _v3)
        pdf_blob = None
        for suffix in ("", "_v2", "_v3"):
            b = pdf_bkt.blob(f"regnskap/{orgnr}/aarsregnskap_{year}{suffix}.pdf")
            if b.exists():
                pdf_blob = b
                break
        if pdf_blob is None:
            summary["status"] = "no_pdf"
            return summary
        pdf_bytes = pdf_blob.download_as_bytes()

        api_blob = None
        for suffix in ("", "_v2", "_v3"):
            b = pdf_bkt.blob(f"regnskap/{orgnr}/regnskap_{year}{suffix}.json")
            if b.exists():
                api_blob = b
                break
        if api_blob is None:
            summary["status"] = "no_nokkeltall"
            return summary
        api_data = json.loads(api_blob.download_as_text())

        # Pick matching year entry
        api_entry = None
        if isinstance(api_data, list):
            for e in api_data:
                til = e.get("regnskapsperiode", {}).get("tilDato", "")
                if til.startswith(str(year)):
                    api_entry = e
                    break
            if api_entry is None and api_data:
                api_entry = api_data[0]
        else:
            api_entry = api_data

        record = extract_full_pdf(pdf_bytes, orgnr=orgnr, year=year, api_entry=api_entry)
        summary["setup"] = record.get("setup")

        if record.get("status") == "skipped_legacy_format":
            record["pdf_source"] = pdf_blob.name
            record["nokkeltall_source"] = api_blob.name
            out_bkt.blob(_record_uri(orgnr, year)).upload_from_string(
                json.dumps(record, ensure_ascii=False, default=str),
                content_type="application/json",
            )
            summary["status"] = "skipped_legacy"
            return summary

        validation = validate_against_nokkeltall(record, api_entry)
        record["validation"] = validation
        record["pdf_source"] = pdf_blob.name
        record["nokkeltall_source"] = api_blob.name
        record["api_entry_used"] = {
            "id": api_entry.get("id"),
            "journalnr": api_entry.get("journalnr"),
            "regnskapstype": api_entry.get("regnskapstype"),
            "oppstillingsplan": api_entry.get("oppstillingsplan"),
        }

        out_bkt.blob(_record_uri(orgnr, year)).upload_from_string(
            json.dumps(record, ensure_ascii=False, default=str),
            content_type="application/json",
        )

        summary["status"] = "ok"
        summary["n_pass"] = validation["n_pass"]
        summary["n_fail"] = validation["n_fail"]
        summary["n_missing_extract"] = validation["n_missing_extract"]
        summary["n_missing_api"] = validation["n_missing_api"]
        summary["disagreements"] = validation["disagreements"]
    except Exception as e:
        summary["status"] = "error"
        summary["error"] = f"{type(e).__name__}: {e}"
        summary["traceback"] = traceback.format_exc()[-1500:]
    finally:
        summary["elapsed"] = round(time.time() - t0, 2)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default="/home/claude/work/sample/sample_100.json")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--checkpoint-every", type=int, default=10)
    args = ap.parse_args()

    sample = json.load(open(args.sample))
    if args.limit:
        sample = sample[:args.limit]

    if args.tag is None:
        args.tag = datetime.now().strftime("%Y%m%d_%H%M%S")

    client = storage.Client()
    pdf_bkt = client.bucket(PDF_BUCKET)
    out_bkt = client.bucket(OUT_BUCKET)

    log.info("Processing %d (orgnr, year) pairs with %d workers, tag=%s",
             len(sample), args.workers, args.tag)
    t0 = time.time()
    summaries: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, str(o), int(y), args.tag, pdf_bkt, out_bkt): (o, y)
                for o, y in sample}
        for i, fut in enumerate(as_completed(futs)):
            try:
                s = fut.result()
            except Exception as e:
                o, y = futs[fut]
                s = {"orgnr": str(o), "year": int(y), "status": "uncaught",
                     "error": str(e), "disagreements": [],
                     "n_pass": 0, "n_fail": 0,
                     "n_missing_extract": 0, "n_missing_api": 0,
                     "setup": None, "elapsed": None}
            summaries.append(s)
            if (i + 1) % 1 == 0:
                log.info("[%d/%d] %s %s status=%s setup=%s pass=%d/%d miss=%d (%.1fs)",
                         i + 1, len(sample),
                         s.get("orgnr"), s.get("year"), s.get("status"),
                         s.get("setup"), s.get("n_pass", 0),
                         s.get("n_pass", 0) + s.get("n_fail", 0) + s.get("n_missing_extract", 0),
                         s.get("n_missing_extract", 0), s.get("elapsed", 0))
            if (i + 1) % args.checkpoint_every == 0:
                _checkpoint(summaries, out_bkt, args.tag, len(sample), t0)

    _checkpoint(summaries, out_bkt, args.tag, len(sample), t0, final=True)

    n_ok = sum(1 for s in summaries if s["status"] == "ok")
    print(f"\n{'='*60}\nDONE tag={args.tag}")
    print(f"OK: {n_ok}/{len(summaries)}  elapsed={time.time()-t0:.0f}s")
    print(f"{'='*60}")


def _checkpoint(summaries: list[dict], out_bkt, tag: str, n_total: int, t0: float, final: bool = False):
    payload = {
        "tag": tag,
        "checkpoint_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": round(time.time() - t0, 1),
        "n_processed": len(summaries),
        "n_total": n_total,
        "final": final,
        "counts": {
            "ok": sum(1 for s in summaries if s["status"] == "ok"),
            "already_exists": sum(1 for s in summaries if s["status"] == "already_exists"),
            "skipped_legacy": sum(1 for s in summaries if s["status"] == "skipped_legacy"),
            "no_pdf": sum(1 for s in summaries if s["status"] == "no_pdf"),
            "no_nokkeltall": sum(1 for s in summaries if s["status"] == "no_nokkeltall"),
            "error": sum(1 for s in summaries if s["status"] in ("error", "uncaught")),
        },
        "per_orgnr": summaries,
    }
    out_bkt.blob(f"{OUT_PREFIX_META}/run_progress_{tag}.json").upload_from_string(
        json.dumps(payload, ensure_ascii=False, default=str, indent=2),
        content_type="application/json",
    )


if __name__ == "__main__":
    main()
