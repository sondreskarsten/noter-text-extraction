from __future__ import annotations
import warnings
import PIL.Image
PIL.Image.MAX_IMAGE_PIXELS = None
warnings.filterwarnings("ignore", category=PIL.Image.DecompressionBombWarning)
"""Phase 1 runner: process the 100-orgnr sample end-to-end."""


import argparse
import csv
import io
import json
import logging
import os
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from google.cloud import storage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from noter_text_extraction.full_pdf_orchestrator import extract_full_pdf
from noter_text_extraction.nokkeltall_validator import validate_against_nokkeltall
from noter_text_extraction.deep_dive import deep_dive
from noter_text_extraction.canonical_schema import get_field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("phase1_runner")

PDF_BUCKET = "brreg-regnskap"
OUT_BUCKET = "sondre_brreg_data"
OUT_PREFIX = "raw/noter_extraction_2025/extractions/tesseract_v2_full"
META_PREFIX = "raw/noter_extraction_2025/_meta"


def fetch_pdf_and_api(client, orgnr, year):
    bkt = client.bucket(PDF_BUCKET)
    pdf_blob = bkt.blob(f"regnskap/{orgnr}/aarsregnskap_{year}.pdf")
    api_blob = bkt.blob(f"regnskap/{orgnr}/regnskap_{year}.json")
    pdf = pdf_blob.download_as_bytes() if pdf_blob.exists() else None

    candidate_entries: list[dict] = []
    for vsuf in ("", "_v2", "_v3", "_v4"):
        b = bkt.blob(f"regnskap/{orgnr}/regnskap_{year}{vsuf}.json")
        if b.exists():
            try:
                d = json.loads(b.download_as_text())
                if isinstance(d, list):
                    candidate_entries.extend(d)
                elif isinstance(d, dict):
                    candidate_entries.append(d)
            except Exception as e:
                log.warning("api parse %s/%s%s failed: %s", orgnr, year, vsuf, e)

    api = None
    for e in candidate_entries:
        til = e.get("regnskapsperiode", {}).get("tilDato", "")
        if til.startswith(f"{year}-"):
            api = e
            break

    if api is None:
        try:
            import urllib.request as _ur
            with _ur.urlopen(
                f"https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}",
                timeout=15,
            ) as resp:
                live = json.loads(resp.read())
            for e in live if isinstance(live, list) else [live]:
                til = e.get("regnskapsperiode", {}).get("tilDato", "")
                if til.startswith(f"{year}-"):
                    api = e
                    break
        except Exception as e:
            log.warning("live api %s/%s failed: %s", orgnr, year, e)

    return pdf, api


def run_one(orgnr, year, client, skip_if_exists=True):
    import gc
    t0 = time.time()
    out = {"orgnr": orgnr, "year": year}

    if skip_if_exists:
        existing = client.bucket(OUT_BUCKET).blob(f"{OUT_PREFIX}/{orgnr}_{year}.json")
        if existing.exists():
            out["status"] = "already_done"
            out["elapsed_seconds"] = 0
            return out

    pdf_bytes, api_entry = fetch_pdf_and_api(client, orgnr, year)
    if pdf_bytes is None:
        out["status"] = "no_pdf"
        return out
    if api_entry is None:
        out["status"] = "no_api"
        return out

    try:
        record = extract_full_pdf(pdf_bytes, orgnr=orgnr, year=year,
                                   api_entry=api_entry, dpi=300)
    except Exception as e:
        out["status"] = "extract_failed"
        out["error"] = str(e)[:300]
        out["traceback"] = traceback.format_exc()[:1500]
        return out

    if record.get("status") != "ok":
        out.update({k: v for k, v in record.items() if k != "manifest"})
        return out

    validation = validate_against_nokkeltall(record, api_entry)
    record["validation"] = validation

    corrections = []
    if validation["disagreements"]:
        for fname in validation["disagreements"][:6]:  # cap deep-dives per firm
            spec = get_field(fname)
            if spec is None:
                continue
            primary_hit = record["primary"].get(fname)
            # Only deep-dive when Tesseract had a value (mismatch case),
            # not when value was missing entirely
            if primary_hit is None or primary_hit.get("value") is None:
                continue
            page_idx = primary_hit["page_idx"] + 1
            label = primary_hit["label_matched"]
            tess_val = primary_hit["value"]

            api_flat_val = None
            for c in validation["checks"]:
                if c["field"] == fname:
                    api_flat_val = c["api"]
                    break
            if api_flat_val is None:
                continue

            try:
                dd = deep_dive(pdf_bytes, page_idx, label,
                               tesseract_v1_value=tess_val,
                               api_value=int(api_flat_val))
            except Exception as e:
                dd = {"diagnosis": f"deep_dive_exception_{type(e).__name__}",
                      "reconciled_value": None,
                      "field_label": label,
                      "tesseract_v1_value": tess_val,
                      "api_value": int(api_flat_val)}

            dd["field"] = fname
            corrections.append(dd)

            if dd.get("reconciled_value") is not None:
                if fname not in record["primary"] or record["primary"].get(fname) is None:
                    record["primary"][fname] = {"value": dd["reconciled_value"],
                                                  "_source": "deep_dive"}
                else:
                    record["primary"][fname]["value_corrected"] = dd["reconciled_value"]
                    record["primary"][fname]["correction_diagnosis"] = dd["diagnosis"]
                    if dd["diagnosis"] == "ocr_misread_corrected":
                        record["primary"][fname]["value"] = dd["reconciled_value"]
                        record["primary"][fname]["_source"] = "deep_dive"

    record["corrections"] = corrections
    record["elapsed_seconds"] = round(time.time() - t0, 1)

    out_blob = client.bucket(OUT_BUCKET).blob(f"{OUT_PREFIX}/{orgnr}_{year}.json")
    out_blob.upload_from_string(json.dumps(record, default=str), content_type="application/json")

    out["status"] = "ok"
    out["setup"] = record.get("setup")
    out["validation"] = {
        "n_pass": validation["n_pass"],
        "n_fail": validation["n_fail"],
        "n_missing_extract": validation["n_missing_extract"],
        "disagreements": validation["disagreements"],
    }
    out["n_corrections"] = len(corrections)
    out["n_corrections_resolved"] = sum(
        1 for c in corrections
        if c.get("diagnosis") == "ocr_misread_corrected"
    )
    out["elapsed_seconds"] = record["elapsed_seconds"]
    del pdf_bytes
    del record
    gc.collect()
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="/home/claude/work/sample/sample_100.json")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--tag", default="phase1")
    parser.add_argument("--force", action="store_true",
                        help="Re-run even if output already exists")
    args = parser.parse_args()

    sample = json.load(open(args.sample))
    if args.limit:
        sample = sample[:args.limit]
    log.info("Running on %d (orgnr,year) pairs with %d workers",
             len(sample), args.workers)

    client = storage.Client()

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, o, y, client, skip_if_exists=not args.force): (o, y)
                for o, y in sample}
        for i, fut in enumerate(as_completed(futs)):
            o, y = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"orgnr": o, "year": y, "status": "uncaught_exception",
                     "error": str(e)[:300]}
            results.append(r)
            if (i + 1) % 5 == 0 or (i + 1) == len(sample):
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                log.info("Progress: %d/%d  (%.1f s, %.2f firm/s)",
                         i + 1, len(sample), elapsed, rate)

    write_summary(results, args.tag)


def write_summary(results, tag):
    client = storage.Client()
    bkt = client.bucket(OUT_BUCKET)

    fieldnames = ["orgnr", "year", "status", "setup", "n_pass", "n_fail",
                  "n_missing_extract", "n_corrections", "n_corrections_resolved",
                  "elapsed_seconds", "disagreements", "error"]
    csv_buf = io.StringIO()
    writer = csv.DictWriter(csv_buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in results:
        v = r.get("validation") or {}
        writer.writerow({
            "orgnr": r.get("orgnr"),
            "year": r.get("year"),
            "status": r.get("status"),
            "setup": r.get("setup"),
            "n_pass": v.get("n_pass"),
            "n_fail": v.get("n_fail"),
            "n_missing_extract": v.get("n_missing_extract"),
            "n_corrections": r.get("n_corrections"),
            "n_corrections_resolved": r.get("n_corrections_resolved"),
            "elapsed_seconds": r.get("elapsed_seconds"),
            "disagreements": ",".join(v.get("disagreements", []) or []),
            "error": (r.get("error") or "")[:200],
        })
    bkt.blob(f"{META_PREFIX}/run_{tag}_per_firm.csv").upload_from_string(
        csv_buf.getvalue(), content_type="text/csv"
    )

    n_total = len(results)
    n_ok = sum(1 for r in results if r.get("status") == "ok")
    n_no_pdf = sum(1 for r in results if r.get("status") == "no_pdf")
    n_no_api = sum(1 for r in results if r.get("status") == "no_api")
    n_skipped_legacy = sum(1 for r in results if r.get("status") == "skipped_legacy_format")
    n_extract_failed = sum(1 for r in results
                            if r.get("status") in ("extract_failed", "uncaught_exception"))
    setup_counts = Counter(r.get("setup") for r in results if r.get("status") == "ok")

    field_disagree = Counter()
    for r in results:
        v = r.get("validation") or {}
        for f in v.get("disagreements", []) or []:
            field_disagree[f] += 1

    sum_pass = sum((r.get("validation") or {}).get("n_pass", 0) for r in results)
    sum_fail = sum((r.get("validation") or {}).get("n_fail", 0) for r in results)
    sum_missing = sum((r.get("validation") or {}).get("n_missing_extract", 0) for r in results)
    sum_corrections = sum(r.get("n_corrections", 0) or 0 for r in results)
    sum_corrections_resolved = sum(r.get("n_corrections_resolved", 0) or 0 for r in results)

    summary = {
        "tag": tag,
        "n_total": n_total,
        "n_ok": n_ok,
        "n_no_pdf": n_no_pdf,
        "n_no_api": n_no_api,
        "n_skipped_legacy": n_skipped_legacy,
        "n_extract_failed": n_extract_failed,
        "setup_distribution": dict(setup_counts),
        "field_pass_total": sum_pass,
        "field_fail_total": sum_fail,
        "field_missing_total": sum_missing,
        "field_disagree_top": field_disagree.most_common(20),
        "n_corrections_attempted": sum_corrections,
        "n_corrections_resolved": sum_corrections_resolved,
    }

    bkt.blob(f"{META_PREFIX}/run_{tag}_summary.json").upload_from_string(
        json.dumps(summary, indent=2, default=str), content_type="application/json"
    )

    log.info("Summary: ok=%d no_pdf=%d no_api=%d legacy=%d failed=%d",
             n_ok, n_no_pdf, n_no_api, n_skipped_legacy, n_extract_failed)
    log.info("Field totals: pass=%d fail=%d missing=%d  corrections_attempted=%d resolved=%d",
             sum_pass, sum_fail, sum_missing, sum_corrections, sum_corrections_resolved)
    log.info("Top field disagreements: %s", field_disagree.most_common(10))
    log.info("Setup distribution: %s", dict(setup_counts))


if __name__ == "__main__":
    main()
