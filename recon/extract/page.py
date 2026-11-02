"""One page's read, whichever reader produced it (profile, OCR, LLM)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..rules import dpp, line_amount, ppn


@dataclass
class PageParse:
    page: int
    method: str
    profile: str = ""
    header: dict = field(default_factory=dict)     # doc_no, doc_date, due_date, po_no, reference -> Field
    doc_type: str = ""
    vendor: str = ""
    has_table: bool = False
    lines: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)     # subtotal, dpp, ppn, total -> Field
    totals_labels: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    min_conf: float = 100.0
    llm_usage: dict = field(default_factory=dict)

    @property
    def doc_no(self) -> str:
        f = self.header.get("doc_no")
        return f.value if f else ""

    def header_value(self, k: str) -> str:
        f = self.header.get(k)
        return f.value if f else ""


def page_self_consistent(pp: PageParse) -> bool:
    """Do the page's own figures satisfy R1-R4? Used to judge a scanned read: a
    misread digit would have to keep every equation true by coincidence."""
    if not pp.lines:
        return False
    if any(line_amount(l.qty, l.unit_price, l.disc_pct) != l.amount for l in pp.lines):
        return False
    t = {k: f.value for k, f in pp.totals.items()}
    if "subtotal" in t:
        if not t.get("total"):
            return True     # continuation page with lines only
        # subtotal is checked against all lines of the document at assembly; here only the page's own lines
        if pp.header.get("doc_no") and sum(l.amount for l in pp.lines) != t["subtotal"]:
            return False
        if "dpp" in t and abs(dpp(t["subtotal"]) - t["dpp"]) > 10:
            return False
        if "ppn" in t and "dpp" in t and abs(ppn(t["dpp"]) - t["ppn"]) > 10:
            return False
        if "ppn" in t and t["total"] != t["subtotal"] + t["ppn"]:
            return False
    return True
