#!/usr/bin/env python3
"""Visual audit: render every page of a regnskap PDF and compare against
extraction. Use when extract_orgnr produces thin output to see what was missed.

Usage:
  python scripts/audit_extraction.py <orgnr> <year>

Output: rasterizes /tmp/audit/<orgnr>_<year>/page-NN.jpg for visual inspection
(via Claude's `view` tool in chat, or any image viewer).
"""
import argparse
import json
import os
import sys

from google.cloud import storage

from noter_text_extraction.config import DATA_BUCKET, TESSERACT_PREFIX
from noter_text_extraction.pdf_loader import prepare_pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("orgnr")
    ap.add_argument("year", type=int)
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()

    out_dir = f"/tmp/audit/{args.orgnr}_{args.year}"
    os.makedirs(out_dir, exist_ok=True)

    # Override WORK_DIR locally
    from noter_text_extraction import config
    config.WORK_DIR = out_dir
    prep = prepare_pages(args.orgnr, args.year, dpi=args.dpi)

    # Pull the existing extraction (if any) to count what we got
    client = storage.Client()
    bkt = client.bucket(DATA_BUCKET)
    blob = bkt.blob(f"{TESSERACT_PREFIX}/{args.orgnr}_{args.year}.json")
    extraction = None
    if blob.exists():
        extraction = json.loads(blob.download_as_text())

    flags = []
    if extraction:
        n_amounts = sum(len(n.get("raw_amounts", {})) for n in extraction.get("noter", []))
        n_notes = extraction.get("n_notes", 0)
        if n_notes < 3:
            flags.append(f"thin_extraction: only {n_notes} notes")
        if n_amounts < 20:
            flags.append(f"few_amounts: only {n_amounts} amounts")

    report = {
        "orgnr": args.orgnr,
        "year": args.year,
        "pdf_pages": prep["n_pages"],
        "extraction_present": extraction is not None,
        "n_notes": extraction.get("n_notes") if extraction else None,
        "total_amounts": (sum(len(n.get("raw_amounts", {})) for n in extraction.get("noter", []))
                          if extraction else None),
        "rendered_to": out_dir,
        "first_page_jpg": prep["page_jpgs"][0] if prep["page_jpgs"] else None,
        "last_page_jpg": prep["page_jpgs"][-1] if prep["page_jpgs"] else None,
        "flags": flags,
    }
    print(json.dumps(report, indent=2))
    return 2 if flags else 0


if __name__ == "__main__":
    sys.exit(main())
