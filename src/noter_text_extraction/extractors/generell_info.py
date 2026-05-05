"""Generell info extractor — page 1 KV grid.

The BRREG-rendered first page of every årsregnskap is a fixed-form summary
with `Label: Value` rows. We OCR the page, then label-match each canonical
metadata field.
"""

from __future__ import annotations

import re
from io import BytesIO

from PIL import Image
import pytesseract


_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_PERIOD_RE = re.compile(
    r"(\d{2})\.(\d{2})\.(\d{4})\s*[-–]\s*(\d{2})\.(\d{2})\.(\d{4})"
)
_ORGNR_RE = re.compile(r"(\d{3})\s*(\d{3})\s*(\d{3})")
_JOURNAL_RE = re.compile(r"(\d[\d\s]{6,})")


def _to_bool_no_yes(s: str | None) -> bool | None:
    if s is None:
        return None
    s = s.strip().lower()
    if s in ("ja", "ja.", "true", "yes"):
        return True
    if s in ("nei", "nei.", "false", "no"):
        return False
    return None


def _ddmmyyyy_to_iso(s: str) -> str | None:
    m = _DATE_RE.search(s)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


def _split_period(s: str) -> tuple[str | None, str | None]:
    m = _PERIOD_RE.search(s)
    if not m:
        return None, None
    d1, mo1, y1, d2, mo2, y2 = m.groups()
    return f"{y1}-{mo1}-{d1}", f"{y2}-{mo2}-{d2}"


def _normalize_organisasjonsform(s: str) -> str | None:
    if not s:
        return None
    s = s.strip().split()[0].upper()
    mapping = {
        "AKSJESELSKAP": "AS",
        "ALLMENNAKSJESELSKAP": "ASA",
        "ANSVARLIG": "ANS",
        "STIFTELSE": "STI",
    }
    return mapping.get(s, s)


def _value_after_label(text: str, label: str) -> str | None:
    """Find a labeled line and return the value after the colon."""
    for line in text.splitlines():
        if label.lower() in line.lower():
            if ":" in line:
                return line.split(":", 1)[1].strip()
            else:
                idx = line.lower().find(label.lower())
                tail = line[idx + len(label):].strip()
                if tail:
                    return tail
    return None


def extract_generell_info(page1_image: Image.Image) -> dict:
    text = pytesseract.image_to_string(page1_image, lang="nor", config="--psm 6")

    out: dict = {"_ocr_text": text}

    # orgnr — accept space-separated 9-digit
    raw = _value_after_label(text, "Organisasjonsnummer")
    if raw:
        m = _ORGNR_RE.search(raw)
        if m:
            out["orgnr"] = "".join(m.groups())

    # foretaksnavn
    raw = _value_after_label(text, "Foretaksnavn")
    if raw:
        out["foretaksnavn"] = raw.strip()

    # organisasjonsform — Aksjeselskap → AS
    raw = _value_after_label(text, "Organisasjonsform")
    if raw:
        out["organisasjonsform"] = _normalize_organisasjonsform(raw)

    # regnskapsperiode — split fra/til
    raw = _value_after_label(text, "Årsregnskapets periode")
    if raw:
        fra, til = _split_period(raw)
        out["regnskapsperiode_fra"] = fra
        out["regnskapsperiode_til"] = til

    # journalnr
    raw = _value_after_label(text, "Journalnummer")
    if raw:
        m = _JOURNAL_RE.search(raw)
        if m:
            out["journalnr"] = m.group(1).replace(" ", "")

    # morselskap — "Morselskap i konsern: Nei"
    raw = _value_after_label(text, "Morselskap i konsern")
    if raw is None:
        raw = _value_after_label(text, "Morselskap")
    out["morselskap"] = _to_bool_no_yes(raw)

    # smaa_foretak — "Regler for små foretak benyttet: Nei" / "Ja"
    raw = _value_after_label(text, "Regler for små foretak")
    if raw is None:
        raw = _value_after_label(text, "Smaa foretak")
    out["smaa_foretak"] = _to_bool_no_yes(raw)

    # regnskapsregler — read from text (independent of smaa_foretak flag)
    text_low = text.lower()
    if ("alminnelige regler" in text_low or "alminnelig regler" in text_low
            or "alminnelige regnskapsregler" in text_low
            or "regnskapslovens alminnelige" in text_low
            or "reqgnskapslovens alminnelige" in text_low):
        out["regnskapsregler"] = "regnskapslovenAlminneligRegler"
    elif ("regler for små foretak" in text_low and "regler for små foretak benyttet: ja" in text_low):
        out["regnskapsregler"] = "regnskapslovenSmaaForetak"
    elif out.get("smaa_foretak") is True:
        out["regnskapsregler"] = "regnskapslovenSmaaForetak"
    else:
        out["regnskapsregler"] = "regnskapslovenAlminneligRegler"

    # oppstillingsplan — most filings use "store" plan, not derivable from smaa flag alone
    out["oppstillingsplan"] = "store"  # default; small foretak use small layout but oppstillingsplan="store" is most common
    if False:
        if "smaa" in text_low.split("oppstillingsplan")[1][:200]:
            out["oppstillingsplan"] = "smaa"
        else:
            out["oppstillingsplan"] = "store"
    else:
        out["oppstillingsplan"] = "store"

    # dato_fastsettelse
    raw = _value_after_label(text, "Dato for fastsettelse")
    if raw is None:
        raw = _value_after_label(text, "fastsettelse av årsregnskapet")
    if raw:
        out["dato_fastsettelse"] = _ddmmyyyy_to_iso(raw)

    # avviklingsregnskap
    if re.search(r"\bavviklingsregnskap\b", text, re.IGNORECASE):
        out["avviklingsregnskap"] = True
    else:
        out["avviklingsregnskap"] = False

    # ikke_revidert
    if "ikke revidert" in text_low or "ikke-revidert" in text_low:
        out["ikke_revidert"] = True
    else:
        out["ikke_revidert"] = False

    # fravalg_revisjon — explicit phrases first
    if ("fravalg av revisjon" in text_low or "fravalg revisjon" in text_low
            or "ikke skal revideres: ja" in text_low
            or "ikke revideres: ja" in text_low
            or re.search(r"valgt\s+bort\s+revisjon", text_low)
            or re.search(r"revisjon\s+er\s+fravalgt", text_low)):
        out["fravalg_revisjon"] = True
    elif "ikke skal revideres: nei" in text_low or "skal revideres: nei" in text_low:
        out["fravalg_revisjon"] = False
    else:
        out["fravalg_revisjon"] = False

    # regnskapstype — SELSKAP/KONSERN
    if "konsernregnskap" in text.lower() or "konsern" in text.lower() and "morselskap i konsern: ja" in text.lower():
        out["regnskapstype"] = "KONSERN"
    else:
        out["regnskapstype"] = "SELSKAP"

    # valuta — almost always NOK in Norwegian filings
    if re.search(r"Beløp i:\s*NOK", text):
        out["valuta"] = "NOK"
    else:
        out["valuta"] = "NOK"  # default

    return out
