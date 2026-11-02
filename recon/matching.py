"""Deterministic document and line matching.

Documents: grouped by canonical document number (normalize.norm_doc_no).
Leftovers on both sides get one more chance on a secondary key (same type and
PO and total, or same type and total and date). A secondary match is never
trusted silently: it is reported for review.

Lines: matched inside a document pair by SKU, never by line position (the ERP
and the supplier often order lines differently and the same SKU can appear
more than once with different discounts).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import Document, Line
from .normalize import norm_po, norm_vendor


@dataclass
class DocGroup:
    key: str
    pdf: list = field(default_factory=list)     # PDF documents with this key
    erp: list = field(default_factory=list)     # ERP entries with this key
    secondary: bool = False                     # paired by secondary key
    secondary_basis: str = ""


@dataclass
class LinePair:
    pdf: Optional[Line]
    erp: Optional[Line]
    diffs: list = field(default_factory=list)   # e.g. ["qty", "unit_price"]

    @property
    def amount_diff(self) -> int:
        return (self.pdf.amount if self.pdf else 0) - (self.erp.amount if self.erp else 0)

    @property
    def sku(self) -> str:
        return (self.pdf or self.erp).sku


def scope_erp(erp_docs: list[Document], vendor_names: list[str]) -> tuple[list, list]:
    """Keep ERP entries for the batch's supplier(s); the rest is out of scope."""
    wanted = {norm_vendor(v) for v in vendor_names if v}
    if not wanted:
        return erp_docs, []
    keep, out = [], []
    for d in erp_docs:
        (keep if norm_vendor(d.vendor) in wanted else out).append(d)
    return keep, out


def match_documents(pdf_docs: list[Document], erp_docs: list[Document]) -> list[DocGroup]:
    groups: dict[str, DocGroup] = {}
    order: list[str] = []

    def g(key):
        if key not in groups:
            groups[key] = DocGroup(key)
            order.append(key)
        return groups[key]

    for d in pdf_docs:
        g(d.key).pdf.append(d)
    for d in erp_docs:
        g(d.key).erp.append(d)

    # secondary key for leftovers
    lone_pdf = [k for k in order if groups[k].pdf and not groups[k].erp]
    lone_erp = [k for k in order if groups[k].erp and not groups[k].pdf]
    for pk in lone_pdf:
        p = groups[pk].pdf[0]
        cands = []
        for ek in lone_erp:
            e = groups[ek].erp[0]
            if e.doc_type != p.doc_type or e.total != p.total:
                continue
            if p.po_no and norm_po(p.po_no) == norm_po(e.po_no):
                cands.append((ek, "same PO and total"))
            elif p.doc_date and p.doc_date == e.doc_date:
                cands.append((ek, "same date and total"))
        if len(cands) == 1:
            ek, basis = cands[0]
            groups[pk].erp = groups[ek].erp
            groups[pk].secondary, groups[pk].secondary_basis = True, basis
            groups[ek].erp = []
            lone_erp.remove(ek)
    return [groups[k] for k in order if groups[k].pdf or groups[k].erp]


def _uom(s: str) -> str:
    return (s or "").strip().upper().rstrip(".")


def _uom_differs(a: str, b: str) -> bool:
    """Units differ unless equal or one OCR-confusable character apart (PES/PCS, ISN/LSN).
    Units are not part of any amount rule, so this never hides a money difference."""
    a, b = _uom(a), _uom(b)
    if not a or not b or a == b:
        return False
    return not (len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1)


def compare_lines(p: Line, e: Line) -> list[str]:
    diffs = []
    if p.qty != e.qty:
        diffs.append("qty")
    if p.unit_price != e.unit_price:
        diffs.append("unit_price")
    if p.disc_pct != e.disc_pct:
        diffs.append("disc_pct")
    if _uom_differs(p.uom, e.uom):
        diffs.append("uom")
    if p.amount != e.amount and not diffs:
        diffs.append("amount")      # same inputs, different amount
    return diffs


def match_lines(pdf_lines: list[Line], erp_lines: list[Line]) -> list[LinePair]:
    """Three passes: exact (sku+qty+price+disc+amount), same SKU closest, leftovers."""
    pairs: list[LinePair] = []
    free_e = list(erp_lines)
    rest_p = []

    def sig(l: Line):
        return (l.sku_key, l.qty, l.unit_price, l.disc_pct, l.amount)

    for p in pdf_lines:
        hit = next((e for e in free_e if sig(e) == sig(p)), None)
        if hit is not None:
            free_e.remove(hit)
            pairs.append(LinePair(p, hit, []))
        else:
            rest_p.append(p)

    unmatched_p = []
    for p in rest_p:
        same = [e for e in free_e if e.sku_key == p.sku_key]
        if not same:
            unmatched_p.append(p)
            continue
        best = min(same, key=lambda e: (len(compare_lines(p, e)), abs(p.amount - e.amount)))
        free_e.remove(best)
        pairs.append(LinePair(p, best, compare_lines(p, best)))

    for p in unmatched_p:
        pairs.append(LinePair(p, None, ["extra_in_pdf"]))
    for e in free_e:
        pairs.append(LinePair(None, e, ["missing_from_pdf"]))

    def order_key(lp: LinePair):
        if lp.pdf is not None:
            return (0, lp.pdf.line_no or 0)
        return (1, lp.erp.line_no or 0)
    pairs.sort(key=order_key)
    return pairs
