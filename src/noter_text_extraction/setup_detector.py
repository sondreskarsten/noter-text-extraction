"""Regnskap setup detector.

Norwegian årsregnskap come in several structural variants. The detector
returns a setup tag that drives parser dispatch.

Setup tags:
    store_selskap     — "store" oppstillingsplan, single entity, full layout
    smaa_selskap      — small-firm rules, abbreviated layout
    store_konsern     — consolidated, 4-column layout (parent / parent-prior / consolidated / consolidated-prior)
    smaa_konsern      — small consolidated, rare
    avviklingsregnskap — winding-up; balanse layout differs
    unknown           — could not infer

Inputs available:
    - regnskapsapi entry (authoritative for oppstillingsplan / regnskapstype / smaaForetak / avvikling)
    - manifest from page_classifier (n_brreg, konsern_evidence, ..)
    - generell_info OCR record (smaa_foretak, morselskap, regnskapsregler)
"""

from __future__ import annotations


def detect_setup(api_entry: dict | None,
                 manifest: dict | None,
                 generell_info: dict | None) -> str:
    avvikling = False
    if api_entry is not None and api_entry.get("avviklingsregnskap"):
        avvikling = True
    if generell_info is not None and generell_info.get("avviklingsregnskap"):
        avvikling = True
    if avvikling:
        return "avviklingsregnskap"

    smaa = None
    konsern = None
    if api_entry is not None:
        smaa = api_entry.get("regnkapsprinsipper", {}).get("smaaForetak")
        konsern = api_entry.get("regnskapstype") == "KONSERN"
        oppstilling = api_entry.get("oppstillingsplan")
        if oppstilling == "smaa":
            smaa = True
        elif oppstilling == "store":
            smaa = False if smaa is None else smaa

    if generell_info is not None:
        if smaa is None:
            smaa = generell_info.get("smaa_foretak")
        if konsern is None:
            konsern = generell_info.get("regnskapstype") == "KONSERN"

    if konsern is None and manifest is not None:
        konsern = bool(manifest.get("konsern", {}).get("detected", False))

    if konsern is True:
        return "smaa_konsern" if smaa else "store_konsern"
    if smaa is True:
        return "smaa_selskap"
    if smaa is False:
        return "store_selskap"
    return "unknown"
