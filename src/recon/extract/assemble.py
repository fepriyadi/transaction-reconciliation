"""Page reads -> documents. Multi-page invoices are stitched; every page is logged."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import Document, Field
from ..normalize import norm_doc_no, parse_date
from .page import PageParse, page_self_consistent


@dataclass
class PageLog:
    file: str
    page: int
    method: str
    profile: str
    doc_no: str = ""
    role: str = ""          # start | continuation | unassigned
    confidence: float = 1.0
    notes: list = field(default_factory=list)


def assemble(parses: list[PageParse], file: str, date_order: str = "DMY") -> tuple[list[Document], list[PageLog]]:
    docs: list[Document] = []
    logs: list[PageLog] = []
    cur: Optional[Document] = None
    for pp in parses:
        log = PageLog(file, pp.page, pp.method, pp.profile, notes=list(pp.notes))
        conf = page_confidence(pp)
        log.confidence = conf
        starts_new = bool(pp.doc_no) and not (
            cur is not None and cur.key == norm_doc_no(pp.doc_no) and cur.total is None)
        if starts_new:
            d, amb = parse_date(pp.header["doc_date"].value, date_order) if "doc_date" in pp.header else (None, False)
            cur = Document(side="PDF", doc_type=pp.doc_type or "INVOICE", doc_no=pp.doc_no, doc_date=d,
                           vendor=pp.vendor, po_no=pp.header_value("po_no"), reference=pp.header_value("reference"),
                           method=pp.method, confidence=conf)
            cur.fields.update(pp.header)
            cur.fields["file"] = Field(file)
            docs.append(cur)
            log.role = "start"
        elif cur is not None and pp.has_table and cur.total is None:
            log.role = "continuation"
            cur.confidence = min(cur.confidence, conf)
            if pp.method != cur.method:
                cur.method = f"{cur.method}+{pp.method}"
        else:
            log.role = "unassigned"
            log.notes.append("page has no document number and does not continue an open document")
            logs.append(log)
            continue
        log.doc_no = cur.doc_no
        cur.pages.append(pp.page)
        cur.lines.extend(pp.lines)
        for k, f in pp.totals.items():
            setattr(cur, k, f.value)
            cur.fields[k] = f
        for k, lab in pp.totals_labels.items():
            cur.fields[f"{k}_label"] = Field(lab, lab)
        cur.extraction_notes.extend(pp.notes)
        logs.append(log)

    for doc in docs:
        _post_checks(doc)
    return docs, logs


def _post_checks(d: Document):
    """Extraction sanity checks + credit-note sign normalisation."""
    nos = [l.line_no for l in d.lines if l.line_no is not None]
    if nos and nos != list(range(1, len(nos) + 1)):
        d.extraction_notes.append(f"line numbers not continuous: {nos}")
        d.confidence = min(d.confidence, 0.5)
    for need in ("doc_no", "total"):
        if getattr(d, need) in (None, ""):
            d.extraction_notes.append(f"missing {need}")
            d.confidence = min(d.confidence, 0.3)
    if not d.lines:
        d.extraction_notes.append("no line items extracted")
        d.confidence = min(d.confidence, 0.3)
    if len(d.pages) > 1:
        d.extraction_notes.append(f"multi-page document (pages {d.pages[0]}–{d.pages[-1]})")
    if d.doc_type == "CREDIT_NOTE" and (d.total or 0) > 0:
        # Credit notes reduce the amount owed (R6): store with negative sign like the ERP.
        for k in ("subtotal", "dpp", "ppn", "total"):
            if getattr(d, k) is not None:
                setattr(d, k, -getattr(d, k))
        for l in d.lines:
            l.qty, l.amount = -l.qty, -l.amount
        d.extraction_notes.append("credit note printed with positive amounts; stored as negative (reduces amount owed)")


def page_confidence(pp: PageParse) -> float:
    if pp.method == "text_layer":
        base = 0.99
    elif pp.method == "ocr":
        # weakest figure-bearing word sets the floor; a fully self-consistent read is arithmetic-verified
        base = 0.6 + 0.3 * max(0.0, min(pp.min_conf, 100)) / 100
        if page_self_consistent(pp):
            base = max(base, 0.9)
    elif pp.method == "ocr+llm_vision":
        base = 0.97
    else:
        base = 0.85
    if pp.notes:
        base = min(base, 0.6)
    if pp.method != "text_layer" and not page_self_consistent(pp):
        base = min(base, 0.5)
    return round(base, 2)
