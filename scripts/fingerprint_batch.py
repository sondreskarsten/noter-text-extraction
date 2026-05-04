"""Fingerprint a sample of regnskap PDFs and cluster by leverandør template.

Downloads PDFs from gs://brreg-regnskap/, runs fingerprint pipeline,
clusters by perceptual hash, outputs leverandør distribution and cluster
assignments to GCS.

Usage:
    python scripts/fingerprint_batch.py --n 200 --seed 42
    python scripts/fingerprint_batch.py --orgnrs 989100106 912345678
"""
import argparse
import csv
import io
import json
import logging
import os
import random
import sys
import tempfile
from collections import Counter
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from google.cloud import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

REGNSKAP_BUCKET = "brreg-regnskap"
DATA_BUCKET = "sondre_brreg_data"
PDF_PREFIX = "regnskap"
OUTPUT_PREFIX = "raw/noter_extraction_2025/_meta/fingerprints"


def sample_orgnrs_with_pdfs(n: int = 200, seed: int = 42, year: int = 2024) -> list[str]:
    client = storage.Client()
    bkt = client.bucket(REGNSKAP_BUCKET)
    all_orgnrs = set()
    for blob in bkt.list_blobs(prefix=f"{PDF_PREFIX}/"):
        parts = blob.name.split("/")
        if len(parts) >= 3 and parts[2] == f"aarsregnskap_{year}.pdf":
            all_orgnrs.add(parts[1])
    all_orgnrs = sorted(all_orgnrs)
    random.seed(seed)
    return random.sample(all_orgnrs, min(n, len(all_orgnrs)))


def download_pdf(client, orgnr: str, year: int = 2024) -> str:
    blob = client.bucket(REGNSKAP_BUCKET).blob(f"{PDF_PREFIX}/{orgnr}/aarsregnskap_{year}.pdf")
    if not blob.exists():
        return None
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    blob.download_to_filename(path)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--orgnrs", nargs="+")
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args()

    from noter_text_extraction.fingerprint import (
        fingerprint_pdf, cluster_fingerprints,
    )

    client = storage.Client()

    if args.orgnrs:
        orgnrs = args.orgnrs
    else:
        log.info("Sampling %d orgnrs with PDFs for year %d...", args.n, args.year)
        orgnrs = sample_orgnrs_with_pdfs(args.n, args.seed, args.year)

    log.info("Processing %d orgnrs", len(orgnrs))

    fingerprints = []
    rows = []
    for i, orgnr in enumerate(orgnrs):
        path = download_pdf(client, orgnr, args.year)
        if path is None:
            rows.append({"orgnr": orgnr, "year": args.year, "status": "no_pdf"})
            continue
        try:
            fp = fingerprint_pdf(path, ocr=not args.no_ocr)
            fingerprints.append(fp)

            company_fps = [p for p in fp.pages if p.source == "company"]
            target = company_fps[1] if len(company_fps) > 1 else (company_fps[0] if company_fps else None)

            rows.append({
                "orgnr": orgnr,
                "year": args.year,
                "status": "ok",
                "n_pages": fp.n_pages,
                "brreg_boundary": fp.brreg_boundary,
                "leverandor": fp.leverandor,
                "leverandor_confidence": fp.leverandor_confidence,
                "n_company_pages": len(company_fps),
                "phash_footer": target.phash_footer if target else None,
                "footer_text_snippet": (target.footer_text or "")[:80] if target else None,
                "header_text_snippet": (target.header_text or "")[:80] if target else None,
            })
        except Exception as e:
            rows.append({"orgnr": orgnr, "year": args.year, "status": f"error: {e}"})
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

        if (i + 1) % 25 == 0:
            log.info("Progress: %d/%d", i + 1, len(orgnrs))

    clusters = cluster_fingerprints(fingerprints, max_distance=5)

    leverandor_dist = Counter(r.get("leverandor") for r in rows if r.get("status") == "ok")
    print(f"\n{'='*60}")
    print(f"FINGERPRINT RESULTS (n={len(rows)})")
    print(f"{'='*60}")
    print(f"Processed: {sum(1 for r in rows if r['status'] == 'ok')}")
    print(f"Clusters:  {len(clusters)}")
    print(f"\nLeverandør distribution:")
    for lev, count in leverandor_dist.most_common():
        print(f"  {lev:25s}  {count:4d}  ({100*count/sum(leverandor_dist.values()):.0f}%)")
    print(f"\nCluster sizes:")
    for cid in sorted(clusters, key=lambda c: -len(clusters[c]))[:20]:
        print(f"  cluster_{cid:3d}: {len(clusters[cid]):4d} PDFs")

    bkt = client.bucket(DATA_BUCKET)

    all_keys = set()
    for r in rows:
        all_keys.update(r.keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=sorted(all_keys))
    writer.writeheader()
    writer.writerows(rows)

    blob = bkt.blob(f"{OUTPUT_PREFIX}/fingerprint_results_{args.year}.csv")
    blob.upload_from_string(buf.getvalue(), content_type="text/csv")
    log.info("Wrote results to gs://%s/%s", DATA_BUCKET, blob.name)

    cluster_data = {str(cid): paths for cid, paths in clusters.items()}
    blob = bkt.blob(f"{OUTPUT_PREFIX}/clusters_{args.year}.json")
    blob.upload_from_string(json.dumps(cluster_data, indent=2), content_type="application/json")
    log.info("Wrote clusters to gs://%s/%s", DATA_BUCKET, blob.name)


if __name__ == "__main__":
    main()
