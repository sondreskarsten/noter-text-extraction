"""Nokkeltall validator.

Compares extracted primary-statement values against the flat regnskapsapi
record. Tolerance is per-field via `FieldSpec.tolerance`.

Output:
    {"checks": [
        {"field": str, "extracted": int|str|bool, "api": int|str|bool,
         "diff": float, "tolerance_ok": bool, "tolerance_class": str},
        ...],
     "n_pass": int, "n_fail": int, "n_missing_extract": int,
     "n_missing_api": int, "disagreements": [field_name, ...]}
"""

from __future__ import annotations

from .canonical_schema import ALL_FIELDS, flatten_api, FieldSpec


def _tolerance_ok(extracted, api, tol_class: str) -> tuple[bool, float]:
    if extracted is None or api is None:
        return False, float("nan")

    if tol_class == "exact":
        if isinstance(extracted, (int, float)) and isinstance(api, (int, float)):
            return float(extracted) == float(api), abs(float(extracted) - float(api))
        return str(extracted).strip() == str(api).strip(), 0.0

    if tol_class == "pct1":
        try:
            e = float(extracted)
            a = float(api)
        except (TypeError, ValueError):
            return False, float("nan")
        diff = abs(e - a)
        tol = max(abs(a) * 0.01, 1000.0)
        return diff <= tol, diff

    if tol_class == "bool_match":
        return bool(extracted) == bool(api), 0.0

    if tol_class == "str_match":
        return str(extracted).strip().lower() == str(api).strip().lower(), 0.0

    return False, float("nan")


def validate_against_nokkeltall(record: dict, api_entry: dict) -> dict:
    """Validate extracted record against an API regnskapsapi entry."""
    api_flat = flatten_api(api_entry)

    extracted_flat = {}
    extracted_flat.update(record.get("generell_info", {}))
    for k, v in record.get("primary", {}).items():
        if isinstance(v, dict) and "value" in v:
            extracted_flat[k] = v["value"]
        else:
            extracted_flat[k] = v

    checks = []
    n_pass = 0
    n_fail = 0
    n_missing_extract = 0
    n_missing_api = 0
    disagreements = []

    for spec in ALL_FIELDS:
        api_val = api_flat.get(spec.canonical)
        ext_val = extracted_flat.get(spec.canonical)

        if api_val is None and ext_val is None:
            continue
        if api_val is None:
            n_missing_api += 1
            checks.append({
                "field": spec.canonical, "extracted": ext_val, "api": None,
                "diff": None, "tolerance_ok": None,
                "tolerance_class": spec.tolerance,
                "missing": "api",
            })
            continue
        if ext_val is None:
            n_missing_extract += 1
            checks.append({
                "field": spec.canonical, "extracted": None, "api": api_val,
                "diff": None, "tolerance_ok": False,
                "tolerance_class": spec.tolerance,
                "missing": "extracted",
            })
            if spec.layout in ("resultat", "balanse"):
                disagreements.append(spec.canonical)
            continue

        ok, diff = _tolerance_ok(ext_val, api_val, spec.tolerance)
        checks.append({
            "field": spec.canonical, "extracted": ext_val, "api": api_val,
            "diff": diff, "tolerance_ok": ok,
            "tolerance_class": spec.tolerance,
            "missing": None,
        })
        if ok:
            n_pass += 1
        else:
            n_fail += 1
            if spec.layout in ("resultat", "balanse"):
                disagreements.append(spec.canonical)

    return {
        "checks": checks,
        "n_pass": n_pass,
        "n_fail": n_fail,
        "n_missing_extract": n_missing_extract,
        "n_missing_api": n_missing_api,
        "disagreements": disagreements,
    }
