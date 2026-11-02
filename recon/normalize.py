"""Normalisation of identifiers, numbers and dates coming from either side.

Every function here is pure and deterministic; formatting differences are
resolved here so that matching only ever compares canonical values.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from fractions import Fraction
from typing import Optional

_TOKEN = re.compile(r"[A-Z0-9]+")


def norm_doc_no(s: str) -> str:
    """Canonical document number: separators and leading zeros don't matter.

    PKI/INV/2026/09/0142, PKI-INV-2026-09-0142, 'pki inv 2026 09 142' -> PKI|INV|2026|9|142
    """
    if s is None:
        return ""
    toks = _TOKEN.findall(str(s).upper())
    return "|".join(t.lstrip("0") or "0" if t.isdigit() else t for t in toks)


def norm_sku(s: str) -> str:
    return "".join(_TOKEN.findall(str(s or "").upper()))


def norm_po(s: str) -> str:
    return norm_doc_no(s)


_LEGAL_FORMS = {"PT", "CV", "TBK", "UD", "PERSERO"}


def norm_vendor(s: str) -> str:
    toks = [t for t in _TOKEN.findall(str(s or "").upper()) if t not in _LEGAL_FORMS]
    return " ".join(toks)


def parse_number(raw, decimal_hint: str = "auto") -> Optional[Fraction]:
    """Parse '1.238.490', '15,619,088', '2,5', '49500', 13900.0 -> exact Fraction.

    decimal_hint: ',' (Indonesian), '.' (US) or 'auto'. In auto mode a separator
    followed by exactly three digits (and repeated consistently) is a thousands
    separator; otherwise it is the decimal mark.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return Fraction(raw)
    if isinstance(raw, float):
        return Fraction(str(raw))
    s = str(raw).strip()
    s = s.replace("Rp", "").replace("IDR", "").replace(" ", "").replace(" ", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith("-") or s.startswith("−"):
        neg, s = True, s[1:]
    s = s.strip(".,;:_'`")  # OCR punctuation noise at the edges ("14.", "_77.188")
    if not s or not re.fullmatch(r"[0-9.,]+", s):
        return None

    dec = None
    if "." in s and "," in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        parts = s.split(sep)
        thousands_like = len(parts) > 1 and all(len(p) == 3 for p in parts[1:]) and 1 <= len(parts[0]) <= 3
        if len(parts) > 2:
            if not thousands_like:
                return None          # e.g. '1.2.3' — not a number we can trust
            dec = None
        elif thousands_like and decimal_hint != sep:
            dec = None
        else:
            dec = sep
    if dec:
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
        whole, _, frac = s.partition(".")
        val = Fraction(int(whole or "0")) + (Fraction(int(frac), 10 ** len(frac)) if frac else 0)
    else:
        val = Fraction(int(s.replace(".", "").replace(",", "")))
    return -val if neg else val


_BLANK = {"", "-", "–", "—"}


def is_blank(raw) -> bool:
    """An empty cell ('', '-', None): for a discount that means no discount, not an unreadable value."""
    return raw is None or (isinstance(raw, str) and raw.strip() in _BLANK)


def parse_percent(raw, decimal_hint: str = "auto") -> Optional[Fraction]:
    """Percentage column value: '5', '5%', '2,5 %' -> exact Fraction (the column already means percent)."""
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.endswith("%"):
            raw = raw[:-1]
    return parse_number(raw, decimal_hint)


def parse_int_amount(raw, decimal_hint: str = "auto") -> Optional[int]:
    v = parse_number(raw, decimal_hint)
    if v is None:
        return None
    if v.denominator != 1:
        return None
    return int(v)


_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"), "ymd"),
    (re.compile(r"^(\d{1,2})[-./](\d{1,2})[-./](\d{4})$"), "dmy_or_mdy"),
]


def parse_date(raw, order: str = "DMY") -> tuple[Optional[date], bool]:
    """Return (date, ambiguous). `order` is the source's declared convention
    (DMY for Indonesian documents, MDY for some ERP text exports)."""
    if raw is None or raw == "":
        return None, False
    if isinstance(raw, datetime):
        return raw.date(), False
    if isinstance(raw, date):
        return raw, False
    s = str(raw).strip()
    for pat, kind in _DATE_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        a, b, c = (int(x) for x in m.groups())
        if kind == "ymd":
            return _safe_date(a, b, c), False
        # a/b/year: decide between day-first and month-first
        if a > 12 and b <= 12:
            return _safe_date(c, b, a), False
        if b > 12 and a <= 12:
            return _safe_date(c, a, b), False
        ambiguous = a != b
        if order == "MDY":
            return _safe_date(c, a, b), ambiguous
        return _safe_date(c, b, a), ambiguous
    return None, False


def _safe_date(y, m, d) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def fmt_idr(v) -> str:
    """1238490 -> 'Rp 1,238,490'; negative -> 'Rp -1,238,490'."""
    if v is None:
        return "—"
    v = int(v)
    return f"Rp {v:,}" if v >= 0 else f"Rp -{abs(v):,}"


def fmt_money(v) -> str:
    """Like fmt_idr but keeps fractional rupiah (unit prices can have decimals)."""
    if isinstance(v, Fraction) and v.denominator != 1:
        return f"Rp {float(v):,.2f}"
    return fmt_idr(v)


def fmt_signed(v) -> str:
    """+Rp 1,000 / -Rp 1,000 (for differences)."""
    if v is None:
        return "—"
    v = int(v)
    return ("+" if v > 0 else "-" if v < 0 else "") + f"Rp {abs(v):,}"


def fmt_num(v) -> str:
    if v is None:
        return ""
    if isinstance(v, Fraction):
        if v.denominator == 1:
            return f"{int(v):,}"
        return f"{float(v):,.4f}".rstrip("0").rstrip(".")
    return f"{v:,}" if isinstance(v, int) else str(v)
