# noter-text-extraction

Deterministic Python pipeline: regnskap PDF → structured JSON for Norwegian årsregnskap notes. Replaces Gemini extraction with Tesseract Norwegian OCR + a column-aware parser. Claude vision is the fallback for failure cases.

## Architecture

```
PDF → pdftoppm rasterize (200 DPI)
    → tesseract -l nor TSV output (per-word bounding boxes)
    → reconstruct lines with column-gap detection (tabs separate columns)
    → segment into notes by 'Note N' header detection
    → per-note: detect year-header or field-header
    → emit raw_amounts dict in noter_v5b-compatible schema
    → upload to gs://sondre_brreg_data/raw/noter_extraction_2025/extractions/tesseract_v1/
```

The downstream `noter-parser` repo consumes the JSON via the same `load_noter_json` interface that handles the Gemini noter_v5b output, so output is drop-in compatible.

## Why this works

Naive `tesseract image -` text output collapses adjacent column values into ambiguous space-separated tokens. For Norwegian financial reports, "522 504 331 492 928 614" must split as two amounts (522,504,331 and 492,928,614), not one 18-digit number — but no purely-textual regex can disambiguate. The TSV output of Tesseract gives per-word bounding boxes, and column boundaries are detectable as horizontal gaps significantly wider than typical intra-number spacing.

For a typical EPAX-style noter page:
- Within a number: ~150 px gap between digit groups
- Between columns: ~340 px gap
- Threshold: `max(median_gap * 2, 30)` reliably separates them

We replace the wider gaps with `\t` and produce a clean tab-separated reconstruction. Downstream parsers split on tabs to get unambiguous cells.

## Where data lives

| Asset | Path |
|---|---|
| Source PDFs | `gs://brreg-regnskap/regnskap/{orgnr}/aarsregnskap_{year}.pdf` |
| Tesseract-derived JSONs | `gs://sondre_brreg_data/raw/noter_extraction_2025/extractions/tesseract_v1/{orgnr}_{year}.json` |
| Claude-vision fallback | `gs://sondre_brreg_data/raw/noter_extraction_2025/extractions/claude_visual_v1/{orgnr}_{year}.json` |
| Manual transcriptions | `gs://sondre_brreg_data/raw/noter_extraction_2025/extractions/manual_claude_v1/{orgnr}_{year}.json` |

## Running

Install:
```bash
apt install tesseract-ocr-nor poppler-utils
pip install -e .
```

Single (orgnr, year):
```bash
python scripts/extract_orgnr.py 989100106 2024
```

Batch:
```bash
python scripts/extract_batch.py orgnrs.txt --workers 8
```

Audit a failure case (renders pages so a human/Claude can visually verify):
```bash
python scripts/audit_extraction.py 989100106 2015
```

## Validated on EPAX 2024

- 20/20 notes detected
- 238 raw_amounts extracted (vs Gemini's 371; ~64% recovery)
- Key values match Gemini exactly: `Langsiktig konsernintern gjeld 2024 = 522,504,331`, `Salgsinntekter Pelagia AS 2024 = 96,345,934`, `EK pr. 31.12.2024 = 449,542,025`

The remaining 36% gap is mostly:
- Narrative-only notes (Note 14 Bankinnskudd in EPAX; the value `kr 2 910 050` lives in prose)
- Multi-line column headers that get merged (Note 10 Egenkapital — recoverable downstream)
- Sub-table structures with no year header

These are addressed by (a) the schema-mapper layer in `noter-parser` (BGE-M3 embeddings) and (b) Claude vision fallback for genuinely ambiguous cases.

## Claude vision fallback

The OCR backend is pluggable. For failures where Tesseract recovery is poor, swap in a Claude-vision callable:

```python
from anthropic import Anthropic
import base64

def claude_vision_backend(jpg_paths: list[str]) -> list[str]:
    client = Anthropic()
    results = []
    for p in jpg_paths:
        with open(p, "rb") as f:
            img = base64.b64encode(f.read()).decode()
        msg = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=4096,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                    "media_type": "image/jpeg", "data": img}},
                {"type": "text", "text": "Transcribe this Norwegian financial report page exactly as printed. Preserve column alignment using tabs. Norwegian thousands separators are spaces; decimal is comma."}
            ]}]
        )
        results.append(msg.content[0].text)
    return results

from noter_text_extraction import extract_one
extract_one("989100106", 2015, ocr_backend=claude_vision_backend)
```

For interactive POC inside Claude.ai chat: `audit_extraction.py` renders pages to `/tmp/audit/{orgnr}_{year}/page-NN.jpg` and operator visually inspects via Claude's `view` tool.

## Status

POC validated on EPAX 2024. Single-page OCR ~10s on 200-DPI page; full 46-page document ~5 minutes on 2 cores, ~75s on Colab Pro 8 cores. For the full 12,860-firm population, run on Colab Pro with parallelism — total wall time ~6-12 hours.

## Coding conventions

Junior implementing senior: no defensive trycatches, no narrative comments, no message logging without request. Each parser fix should generalize across the population.

## Related

- [noter-parser](https://github.com/sondreskarsten/noter-parser) — schema matching layer
- [extraction-prompts](https://github.com/sondreskarsten/extraction-prompts) — original Gemini prompt (still useful for the noter_v5b format spec)
