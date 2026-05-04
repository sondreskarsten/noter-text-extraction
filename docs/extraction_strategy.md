# Extraction Strategy

## The two hard problems

**Problem 1: Column boundary detection.** OCR produces a single-line text stream where adjacent column values are space-separated. Norwegian thousands separators are also spaces. So `"522 504 331 492 928 614"` is ambiguous — could be 522,504,331,492,928,614 (one 18-digit number) or 522,504,331 + 492,928,614 (two amounts in two columns). No purely textual rule resolves this.

**Solution**: Use Tesseract's TSV output to recover per-word bounding boxes. Compute gaps between adjacent words on a line. The intra-number gap (between digit groups within one number) is ~1× the typical word gap; column boundaries are 2-5× wider. We replace wide gaps with `\t` to produce unambiguous tab-separated output.

**Problem 2: Header layout variance.** Norwegian noter use multiple table layouts:
- **Year-as-column**: `2024 | 2023` header, rows are line items
- **Field-as-column**: `Aksjekapital | Annen innskutt | Sum` header, rows are dates
- **No header**: prose-like notes with values mid-sentence

**Solution**: Detect both kinds of headers. A line whose trailing tab-cells are all 4-digit years is a year-header. A line whose tab-cells are all short text labels (no numbers) is a field-header. Apply the most recent header to subsequent rows.

## Validated against EPAX 2024

- All 20 notes detected (matches Gemini)
- 238 of 371 raw_amounts recovered (64%)
- Critical values match exactly: long-term debt to parent, intercompany transactions, EK movements, deferred tax breakdown

## What's NOT recovered

1. **Narrative-only values** like Note 14 Bankinnskudd's `kr 2 910 050` — printed in prose, not in a table. The downstream noter-parser handles this via raw_text fallback.

2. **Multi-line column headers** — Note 10 Egenkapital's header `"Aksjekapital | Annen innskutt | Annen opptjent | Sum"` wraps to a second line `"egenkapital egenkapital"` because of column widths. We currently capture column 1 as `"Aksjekapital Annen innskutt"` (merged), which is imperfect but downstream regex still matches.

3. **Page-header watermarks** like Docusign Envelope IDs — filtered via blacklist.

## OCR backend pluggability

The pipeline accepts any callable `list[jpg_path] -> list[text]` as the OCR backend. Default is Tesseract Norwegian. To use Claude vision instead in Colab:

```python
from anthropic import Anthropic
def claude_vision(jpg_paths):
    client = Anthropic()
    out = []
    for p in jpg_paths:
        with open(p, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        msg = client.messages.create(
            model="claude-opus-4-7", max_tokens=4096,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                    "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": "Transcribe this Norwegian financial report page exactly. Preserve column alignment with tabs."}
            ]}]
        )
        out.append(msg.content[0].text)
    return out

extract_one("989100106", 2015, ocr_backend=claude_vision)
```

Cost estimate: at ~$15/MTok Opus and ~2K tokens/page, full population (~12,860 firms × 30 pages avg) ≈ $1,200. Use Tesseract for the bulk and Claude vision only for failures (low-confidence pages flagged by audit_extraction.py).

## Failure modes to monitor

1. **Low-page-coverage** — rig page-selection bug (EPAX 2015 case). audit_extraction.py flags when n_pages < 5 or n_notes < 3.
2. **Garbage labels** — watermarks leaking in. Blacklist in kv_extractor._is_blacklisted_label.
3. **Long single-row numbers** — column merge bug. The 4-thousands-group cap in `_NUMBER_TOKEN` prevents extraction of >12-digit numbers (rare in real Norwegian filings).
