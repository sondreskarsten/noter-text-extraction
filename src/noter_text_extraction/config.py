"""Configuration for the text-extraction pipeline. Mirrors the noter-parser
output paths so the parser can consume what we produce here."""

PROJECT_ID = "sondreskarsten-d7d14"
DATA_BUCKET = "sondre_brreg_data"
PDF_BUCKET = "brreg-regnskap"

# Where the parser reads from. Our OCR-derived JSONs land here so that
# downstream noter-parser picks them up via the same load_noter_json path.
EXTRACTIONS_BASE = "raw/noter_extraction_2025/extractions"
TESSERACT_PREFIX = f"{EXTRACTIONS_BASE}/tesseract_v1"
CLAUDE_VISUAL_PREFIX = f"{EXTRACTIONS_BASE}/claude_visual_v1"
MANUAL_PREFIX = f"{EXTRACTIONS_BASE}/manual_claude_v1"

# Source PDFs
PDF_PREFIX = "regnskap"

# Local working dir
WORK_DIR = "/tmp/noter_text_extraction"
