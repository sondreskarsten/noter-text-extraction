"""Aggregate Phase 1 results into a morning summary."""
from __future__ import annotations
import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from google.cloud import storage

OUT_BUCKET = "sondre_brreg_data"
RECORDS_PREFIX = "raw/noter_extraction_2025/extractions/tesseract_v2_full"
META_PREFIX = "raw/noter_extraction_2025/_meta"


def load_record(blob):
    return json.loads(blob.download_as_text())


def main():
    c = storage.Client()
    bkt = c.bucket(OUT_BUCKET)
    blobs = list(bkt.list_blobs(prefix=f"{RECORDS_PREFIX}/"))
    print(f"Records: {len(blobs)}")

    with ThreadPoolExecutor(max_workers=16) as ex:
        records = list(ex.map(load_record, blobs))

    # Per-firm summaries
    setup_dist = Counter()
    status_dist = Counter()
    n_pass = 0
    n_fail = 0
    n_missing = 0
    field_pass = Counter()
    field_fail = Counter()
    field_missing = Counter()
    elapsed = []
    n_corrections_attempted = 0
    n_corrections_resolved = 0
    correction_diagnoses = Counter()
    per_firm_field_results = defaultdict(dict)
    nokkeltall_suspect = []

    for rec in records:
        status_dist[rec.get("status", "?")] += 1
        if rec.get("status") != "ok":
            continue
        setup_dist[rec.get("setup", "?")] += 1
        elapsed.append(rec.get("elapsed_seconds", 0))

        val = rec.get("validation", {})
        n_pass += val.get("n_pass", 0)
        n_fail += val.get("n_fail", 0)
        n_missing += val.get("n_missing_extract", 0)

        for ck in val.get("checks", []):
            field = ck.get("field")
            if ck.get("missing") == "extracted":
                field_missing[field] += 1
            elif ck.get("tolerance_ok") is True:
                field_pass[field] += 1
            elif ck.get("tolerance_ok") is False:
                field_fail[field] += 1

        for corr in rec.get("corrections", []):
            n_corrections_attempted += 1
            diag = corr.get("diagnosis", "unknown")
            correction_diagnoses[diag] += 1
            if diag == "ocr_misread_corrected":
                n_corrections_resolved += 1
            if diag == "nokkeltall_suspect":
                nokkeltall_suspect.append({
                    "orgnr": rec["orgnr"], "year": rec["year"],
                    "field": corr.get("field"),
                    "tesseract_value": corr.get("tesseract_v1_value"),
                    "api_value": corr.get("api_value"),
                    "pdf_observed_value": corr.get("pdf_observed_value"),
                })

    # Compose summary markdown
    n_ok = status_dist.get("ok", 0)
    out_lines = []
    out_lines.append("# Phase 1 Morning Summary — 2026-05-05")
    out_lines.append("")
    out_lines.append(f"**Sample**: 100 orgnrs, seed=42, latest fiscal year ∈ {{2024, 2025}} with PDF + nokkeltall both available")
    out_lines.append(f"**Extraction**: Tesseract full-PDF, DPI=300, setup-aware parser dispatch")
    out_lines.append(f"**Validation**: nokkeltall (BRREG regnskapsapi) at 1% tolerance / 1000 NOK floor")
    out_lines.append(f"**Schema**: 34 canonical fields (15 generell + 9 resultatregnskap + 10 balanse)")
    out_lines.append("")
    out_lines.append("## Coverage")
    out_lines.append("")
    out_lines.append(f"| Metric | Value |")
    out_lines.append(f"|---|---|")
    out_lines.append(f"| Records | {len(records)} |")
    out_lines.append(f"| Status `ok` | {n_ok} |")
    for s, n in status_dist.most_common():
        if s == "ok":
            continue
        out_lines.append(f"| Status `{s}` | {n} |")
    out_lines.append(f"| Total field checks | {n_pass + n_fail + n_missing} |")
    if n_pass + n_fail + n_missing > 0:
        out_lines.append(f"| Field pass-rate | {n_pass / (n_pass + n_fail + n_missing):.1%} |")
        out_lines.append(f"| Pass | {n_pass} |")
        out_lines.append(f"| Fail | {n_fail} |")
        out_lines.append(f"| Missing extract | {n_missing} |")
    if elapsed:
        out_lines.append(f"| Mean elapsed | {sum(elapsed) / len(elapsed):.1f}s/firm |")
    out_lines.append("")

    out_lines.append("## Setup distribution")
    out_lines.append("")
    out_lines.append("| Setup | n |")
    out_lines.append("|---|---|")
    for setup, n in setup_dist.most_common():
        out_lines.append(f"| {setup} | {n} |")
    out_lines.append("")

    out_lines.append("## Per-field results")
    out_lines.append("")
    out_lines.append("| Field | Pass | Fail | Missing | Pass % |")
    out_lines.append("|---|---|---|---|---|")
    fields = sorted(set(list(field_pass.keys()) + list(field_fail.keys()) + list(field_missing.keys())))
    for f in fields:
        p, fl, m = field_pass[f], field_fail[f], field_missing[f]
        total = p + fl + m
        pct = f"{100 * p / total:.0f}%" if total else "—"
        out_lines.append(f"| {f} | {p} | {fl} | {m} | {pct} |")
    out_lines.append("")

    out_lines.append("## Deep-dive corrections")
    out_lines.append("")
    out_lines.append(f"**Attempted**: {n_corrections_attempted}")
    out_lines.append(f"**Resolved (ocr_misread_corrected)**: {n_corrections_resolved}")
    out_lines.append("")
    out_lines.append("| Diagnosis | n |")
    out_lines.append("|---|---|")
    for diag, n in correction_diagnoses.most_common():
        out_lines.append(f"| {diag} | {n} |")
    out_lines.append("")

    if nokkeltall_suspect:
        out_lines.append("## Nokkeltall-suspect cases (PDF agrees with Tesseract, not API)")
        out_lines.append("")
        out_lines.append("| orgnr | year | field | tesseract | api | pdf_observed |")
        out_lines.append("|---|---|---|---|---|---|")
        for s in nokkeltall_suspect[:50]:
            out_lines.append(f"| {s['orgnr']} | {s['year']} | {s['field']} | {s['tesseract_value']} | {s['api_value']} | {s['pdf_observed_value']} |")
        out_lines.append("")

    out_lines.append("## Reproducibility")
    out_lines.append("")
    out_lines.append("Records: `gs://sondre_brreg_data/raw/noter_extraction_2025/extractions/tesseract_v2_full/{orgnr}_{year}.json`")
    out_lines.append("")
    out_lines.append("```")
    out_lines.append("python scripts/run_phase1.py --sample sample_100.json --workers 3 --tag <tag>")
    out_lines.append("```")
    out_lines.append("")

    summary_md = "\n".join(out_lines)
    print(summary_md)

    bkt.blob(f"{META_PREFIX}/MORNING_SUMMARY_2026-05-05.md").upload_from_string(
        summary_md, content_type="text/markdown"
    )

    aggregate = {
        "n_records": len(records),
        "status_dist": dict(status_dist),
        "setup_dist": dict(setup_dist),
        "field_pass": dict(field_pass),
        "field_fail": dict(field_fail),
        "field_missing": dict(field_missing),
        "n_pass": n_pass, "n_fail": n_fail, "n_missing": n_missing,
        "n_corrections_attempted": n_corrections_attempted,
        "n_corrections_resolved": n_corrections_resolved,
        "correction_diagnoses": dict(correction_diagnoses),
        "mean_elapsed_seconds": sum(elapsed) / len(elapsed) if elapsed else None,
    }
    bkt.blob(f"{META_PREFIX}/aggregate_2026-05-05.json").upload_from_string(
        json.dumps(aggregate, indent=2, default=str),
        content_type="application/json",
    )
    print("\nWrote summary + aggregate to GCS")


if __name__ == "__main__":
    main()
