#!/usr/bin/env python3
"""Extract noter from one regnskap PDF and write to GCS.

Usage:
  python scripts/extract_orgnr.py <orgnr> <year> [--dpi 200] [--no-upload]
"""
import argparse
import json
import sys

from noter_text_extraction import extract_one


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("orgnr")
    ap.add_argument("year", type=int)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args()

    payload = extract_one(args.orgnr, args.year, dpi=args.dpi, upload=not args.no_upload)
    summary = {
        "orgnr": payload["orgnr"],
        "year": payload["year"],
        "n_pages": payload["n_pages_sent"],
        "n_notes": payload["n_notes"],
        "total_amounts": sum(len(n["raw_amounts"]) for n in payload["noter"]),
    }
    if "_uploaded_to" in payload:
        summary["uploaded"] = payload["_uploaded_to"]
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
