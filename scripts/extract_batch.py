#!/usr/bin/env python3
"""Batch-extract many (orgnr, year) PDFs in parallel.

Usage:
  python scripts/extract_batch.py specs.txt [--workers 8]

specs.txt format: 'orgnr year' per line
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from noter_text_extraction import extract_one


def parse_one(spec: tuple[str, int]):
    orgnr, year = spec
    try:
        payload = extract_one(orgnr, year, upload=True)
        return (orgnr, year, "ok", payload["n_notes"], sum(len(n["raw_amounts"]) for n in payload["noter"]))
    except Exception as e:
        return (orgnr, year, f"error: {type(e).__name__}: {e}", 0, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="Path to file with 'orgnr year' per line")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    specs: list[tuple[str, int]] = []
    with open(args.file) as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = ln.split()
            if len(parts) >= 2:
                specs.append((parts[0], int(parts[1])))

    print(f"Extracting {len(specs)} (orgnr, year) pairs with {args.workers} workers")
    ok = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(parse_one, s): s for s in specs}
        for fut in as_completed(futures):
            orgnr, year, status, n_notes, n_amts = fut.result()
            print(f"  {orgnr} {year}  {status}  notes={n_notes}  amts={n_amts}")
            if status == "ok":
                ok += 1
    print(f"\n{ok}/{len(specs)} succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
