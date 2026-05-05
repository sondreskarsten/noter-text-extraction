"""Parse Norwegian-formatted financial amount strings into floats.

Reference for Norwegian formatting conventions:
- ISO 31-0 / Norwegian convention: comma '.' is decimal, space (or NBSP) is thousands
- Common variants: '1 234,56', '1\u00a0234,56', '1.234,56', '1234,56', '1,234.56'
- Negative: parentheses '(1 234)' OR leading '-'
- Empty / placeholder: '-', '–', '—', '', '0', 'N/A'
- Scale prefix in note caption: 'Beløp i 1000 NOK', 'Tall i hele tusen', 'tNOK', 'MNOK'
- A trailing 'kr' / 'NOK' / '%' may decorate values

The normalizer NEVER guesses scale — that's the caller's responsibility based on
the note's scale-context. This module only does string → float conversion."""
import re
from typing import Optional

# Whitespace separators (regular space + non-breaking space + thin space)
_WS = "\u0020\u00a0\u2009"

# Standard placeholders for "no value"
_PLACEHOLDERS = {"", "-", "–", "—", "—", "n/a", "na", "."}

# Pattern matches Norwegian-style amounts. Capture groups:
#   1: optional sign or open paren
#   2: integer part (digits + optional thousands separators)
#   3: fractional part (after comma OR period if no thousands separator)
#   4: optional close paren
#
# Leading group allowed 1-4 digits to handle OCR cases that drop the first
# thousands separator on amounts like "1016 792 573".
_NORWEGIAN_NUMBER = re.compile(
    rf"""
    ^\s*
    (?P<sign>[-+(])?
    (?P<int>\d{{1,4}}(?:[{_WS}.]\d{{3}})*|\d+)
    (?:,(?P<frac>\d+))?
    (?P<close>\))?
    \s*$
    """,
    re.VERBOSE,
)

# Fallback for English-formatted: 1,234.56
_ENGLISH_NUMBER = re.compile(
    r"""
    ^\s*
    (?P<sign>[-+(])?
    (?P<int>\d{1,3}(?:,\d{3})+|\d+)
    (?:\.(?P<frac>\d+))?
    (?P<close>\))?
    \s*$
    """,
    re.VERBOSE,
)


def normalize_amount(value, scale: float = 1.0) -> Optional[float]:
    """Parse a string or pre-numeric value into a float.

    Args:
        value: Input from raw_amounts dict. May be str, int, float, None.
        scale: Multiplier applied AFTER parsing — e.g. 1000 for tNOK notes.
            Default 1.0 means values are returned in their printed unit.

    Returns:
        Parsed float, or None for unparseable / placeholder inputs.

    Examples:
        >>> normalize_amount("1 234,56")
        1234.56
        >>> normalize_amount("(1 234)")
        -1234.0
        >>> normalize_amount("145.650")  # Norwegian thousands
        145650.0
        >>> normalize_amount("145,65")   # Norwegian decimal
        145.65
        >>> normalize_amount("-")
        None
        >>> normalize_amount("3,5", scale=1000)  # MNOK
        3500.0
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) * scale
    if not isinstance(value, str):
        return None

    s = value.strip()
    s_low = s.lower().strip(" :;")
    if s_low in _PLACEHOLDERS:
        return None

    # Strip currency / unit decorations
    s = re.sub(r"\s*(?:kr\.?|NOK|nok|MNOK|mnok|tNOK|tnok|%)\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^\s*(?:kr\.?|NOK)\s+", "", s, flags=re.IGNORECASE)

    # OCR digit-letter confusions: 'l'/'I' → '1', 'O'/'o' → '0' when adjacent to digits
    s = _ocr_digit_substitute(s)

    # Handle leading minus inside parens style: "( -1 234 )"
    s = s.replace(" ", " ").replace("\xa0", " ")  # normalize NBSP

    # Try Norwegian first
    m = _NORWEGIAN_NUMBER.match(s)
    norwegian_match = False
    if m:
        # Norwegian format requires either a comma OR no period at all
        # (because period would be ambiguous with English decimal)
        # We accept the Norwegian match if:
        #   - has comma (definitely Norwegian decimal), OR
        #   - has no period at all (just integer with spaces), OR
        #   - period appears only as thousands separator (multiple of 3 digit groups)
        int_part = m.group("int")
        if m.group("frac") is not None:
            norwegian_match = True
        elif "." not in int_part:
            norwegian_match = True
        elif _looks_like_thousands_separator(int_part, "."):
            norwegian_match = True

    if not norwegian_match:
        # Try English format
        m = _ENGLISH_NUMBER.match(s)
        if m:
            int_part = m.group("int")
            if m.group("frac") is not None:
                pass
            elif "," not in int_part:
                pass
            elif _looks_like_thousands_separator(int_part, ","):
                pass
            else:
                m = None

    if m is None:
        return None

    int_part = m.group("int")
    frac_part = m.group("frac")
    sign = m.group("sign")
    close = m.group("close")

    if norwegian_match:
        # Strip whitespace and dot thousands separators
        int_clean = re.sub(r"[\s.]", "", int_part)
    else:
        # English: strip comma thousands
        int_clean = int_part.replace(",", "")

    try:
        if frac_part:
            result = float(f"{int_clean}.{frac_part}")
        else:
            result = float(int_clean)
    except ValueError:
        return None

    if sign == "-" or (sign == "(" and close == ")"):
        result = -result

    return result * scale


def _looks_like_thousands_separator(int_part: str, sep: str) -> bool:
    """Return True if `sep` appears as a thousands separator in `int_part`.

    Rule: every group between separators must be exactly 3 digits, except
    the leading group which may be 1-3 digits."""
    if sep not in int_part:
        return True
    groups = int_part.split(sep)
    if len(groups[0]) > 3 or len(groups[0]) == 0:
        return False
    return all(len(g) == 3 and g.isdigit() for g in groups[1:])


def _ocr_digit_substitute(s: str) -> str:
    """Substitute OCR digit-letter confusions when adjacent to digits/spaces.

    Rules:
        'l' / 'I' → '1' when surrounded by digits, spaces, or string boundaries
        'O' / 'o' → '0' when in a clearly-numeric context (digit cluster)

    Conservative: a token is only treated as numeric-context if it consists
    entirely of [digits, l, I, O, o, spaces, NBSP, '.', ',', '-', '(', ')'].
    """
    tokens = s.split(" ")
    out = []
    for tok in tokens:
        clean = tok.replace("\u00a0", "")
        if not clean:
            out.append(tok)
            continue
        candidate = re.sub(r"[lI]", "1", clean)
        candidate = re.sub(r"[Oo]", "0", candidate)
        if re.match(r"^[\-(]?[\d.,]+\)?$", candidate):
            out.append(re.sub(r"[lI]", "1", re.sub(r"[Oo]", "0", tok)))
        else:
            out.append(tok)
    return " ".join(out)


def detect_scale(text: str) -> float:
    """Detect a 'scale this note by N' instruction in note caption / preamble.

    Returns the multiplier (e.g., 1000 for tNOK). Default 1.0 if not found.

    Examples:
        >>> detect_scale("Beløp i 1000 NOK")
        1000.0
        >>> detect_scale("Tall i hele tusen")
        1000.0
        >>> detect_scale("All amounts in MNOK")
        1000000.0
        >>> detect_scale("Note 4 Pensjonskostnader")
        1.0
    """
    if not text:
        return 1.0
    low = text.lower()
    # MNOK / millioner
    if re.search(r"\b(mnok|i\s+millioner|million(?:er)?(?:\s+kr|\s+nok)?|tall i million(er)?)\b", low):
        return 1_000_000.0
    # tNOK / 1000 NOK / hele tusen
    if re.search(r"\b(tnok|i\s+1[\s.]?000\s*(nok|kr)|i\s+tusen|hele\s+tusen|beløp\s+i\s+1\s*000)\b", low):
        return 1_000.0
    return 1.0
