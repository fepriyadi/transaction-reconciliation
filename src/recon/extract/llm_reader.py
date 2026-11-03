"""LLM JSON extraction -> PageParse. The reply is schema-checked, then every value is re-parsed by code."""
from __future__ import annotations

from fractions import Fraction

from ..llm import LLMPage
from ..models import Field, Line, SourceRef
from ..normalize import is_blank, parse_int_amount, parse_number, parse_percent
from .page import PageParse


def _s(v) -> str:
    return "" if v is None else str(v)


def _line_no(v, default: int) -> int:
    try:
        return int(_s(v).strip().rstrip(".")) if not is_blank(v) else default
    except ValueError:
        return default


def parse_page_from_llm(data, page_no: int, file: str, method: str, vendor_default: str = "") -> PageParse:
    """Convert an LLM extraction (LLMPage or its raw JSON dict) into a PageParse."""
    page, problem = LLMPage.from_untrusted(data)
    pp = PageParse(page=page_no, method=method, profile="(llm)")
    if problem:
        pp.notes.append(problem)
    src = SourceRef(file=file, page=page_no, method=method)
    pp.doc_type = "CREDIT_NOTE" if _s(page.doc_type).upper().startswith("CREDIT") else (
        "INVOICE" if page.doc_no else "")
    pp.vendor = _s(page.vendor) or vendor_default
    for k in ("doc_no", "doc_date", "due_date", "po_no", "reference"):
        v = getattr(page, k)
        if v:
            pp.header[k] = Field(str(v), str(v), src)
    for i, ln in enumerate(page.lines or [], 1):
        qty = parse_number(ln.qty)
        price = parse_number(ln.unit_price)
        # blank discount = no discount; a printed value we can't parse is unknown, never 0
        disc = Fraction(0) if is_blank(ln.disc_pct) else parse_percent(ln.disc_pct)
        amt = parse_int_amount(ln.amount)
        if qty is None or price is None or disc is None or amt is None:
            pp.notes.append(f"EXTRACTION_INCOMPLETE: p.{page_no} LLM line {i} has an unreadable figure, line skipped: "
                            f"{ln.model_dump()}")
            continue
        no = _line_no(ln.line_no, i)
        f = {k: Field(v, _s(getattr(ln, k)), src) for k, v in
             (("line_no", no), ("sku", _s(ln.sku)), ("description", _s(ln.description)),
              ("qty", qty), ("uom", _s(ln.uom)), ("unit_price", price), ("disc_pct", disc), ("amount", amt))}
        pp.lines.append(Line(no, _s(ln.sku), _s(ln.description), qty, _s(ln.uom), price, disc, amt, f))
    pp.has_table = bool(pp.lines)
    for k in ("subtotal", "dpp", "ppn", "total"):
        raw = getattr(page, k)
        v = parse_int_amount(raw)
        if v is not None:
            pp.totals[k] = Field(v, _s(raw), src)
    if page.dpp_label:
        pp.totals_labels["dpp"] = _s(page.dpp_label)
    return pp
