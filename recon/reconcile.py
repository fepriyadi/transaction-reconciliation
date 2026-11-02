"""Status, reason codes, difference bridge and control totals.

All deterministic. Which status a set of reason codes produces, and whether a
person must review it, is decided by the registry in reason_codes.py; the
wording of explanations and actions lives in explain.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Optional

from .matching import DocGroup, LinePair, match_lines
from .models import DocChecks, Document
from .normalize import norm_po, parse_date
from .onboarding import SupplierRegister
from .explain import explain
from .reason_codes import LINE_CODE, STATUSES, blocks_approval, check_code, code, status_for
from .rules import RuleConfig, check_document, rule_ppn

BRIDGE_COMPONENTS = [
    ("missing_in_erp", "Supplier documents not booked in ERP"),
    ("missing_in_pdf", "ERP entries with no supplier document"),
    ("duplicate_erp", "Duplicate ERP entries"),
    ("duplicate_pdf", "Duplicate supplier documents"),
    ("line_diff", "Line-level differences (qty / price / discount / extra or missing lines)"),
    ("pdf_subtotal_error", "Invoice subtotal ≠ sum of its lines"),
    ("erp_subtotal_error", "ERP subtotal ≠ sum of its lines"),
    ("tax_on_subtotal_diff", "PPN effect of subtotal differences (per R3/R4)"),
    ("pdf_tax_calc", "Invoice PPN not per R3/R4"),
    ("erp_tax_calc", "ERP PPN not per R3/R4"),
    ("pdf_total_error", "Invoice total ≠ subtotal + PPN"),
    ("erp_total_error", "ERP total ≠ subtotal + PPN"),
    ("unexplained", "Unexplained"),
]


@dataclass
class Bridge:
    """A document's (or batch's) difference split into named, additive components (IDR).
    `unexplained` is None when a figure could not be read, so the remainder is unknown."""
    missing_in_erp: int = 0
    missing_in_pdf: int = 0
    duplicate_erp: int = 0
    duplicate_pdf: int = 0
    line_diff: int = 0
    pdf_subtotal_error: int = 0
    erp_subtotal_error: int = 0
    tax_on_subtotal_diff: int = 0
    pdf_tax_calc: int = 0
    erp_tax_calc: int = 0
    pdf_total_error: int = 0
    erp_total_error: int = 0
    unexplained: Optional[int] = 0

    def items(self) -> list[tuple[str, Optional[int]]]:
        """(component, amount) in BRIDGE_COMPONENTS order."""
        return [(k, getattr(self, k)) for k, _ in BRIDGE_COMPONENTS]

    @property
    def explained(self) -> int:
        return sum(v for k, v in self.items() if k != "unexplained")


assert [f.name for f in fields(Bridge)] == [k for k, _ in BRIDGE_COMPONENTS]


@dataclass(frozen=True)
class Exposure:
    duplicate_erp: int
    overbilled_on_matched_docs: int
    not_booked_in_erp: int
    booked_without_document: int


@dataclass(frozen=True)
class ControlTotals:
    pdf_documents: int
    pdf_invoices: int
    pdf_credit_notes: int
    pdf_lines: int
    erp_entries: int
    erp_lines: int
    erp_out_of_scope: int
    pdf_total: int
    erp_total: int
    difference: int
    sum_of_document_differences: int
    bridge: Bridge
    explained: int
    unexplained: int
    unknown_amount_docs: list
    status_counts: dict           # status -> number of documents
    status_amounts: dict          # status -> Σ difference
    documents: int
    review_required: int
    auto_approvable: int
    exposure: Exposure

    @property
    def fully_explained(self) -> bool:
        return self.unexplained == 0 and not self.unknown_amount_docs


@dataclass
class DocResult:
    key: str
    doc_no: str
    doc_type: str
    pdf_docs: list[Document]
    erp_docs: list[Document]
    pdf_total: Optional[int]
    erp_total: Optional[int]
    difference: Optional[int]
    status: str = ""
    reasons: list[str] = field(default_factory=list)
    explanation: str = ""
    action: str = ""
    confidence: float = 1.0
    review_required: bool = False
    line_pairs: list[LinePair] = field(default_factory=list)
    bridge: Bridge = field(default_factory=Bridge)
    checks: list[DocChecks] = field(default_factory=list)
    headline: str = ""
    new_supplier_note: str = ""

    @property
    def pdf_ref(self) -> str:
        return "; ".join(f"{d.fields.get('file').value if d.fields.get('file') else ''} p.{','.join(map(str, d.pages))}"
                         for d in self.pdf_docs)

    @property
    def erp_ref(self) -> str:
        return "; ".join(f"{d.entry_id} ({d.fields['entry_id'].src.sheet}!row {d.fields['entry_id'].src.row})"
                         for d in self.erp_docs if d.fields.get("entry_id") and d.fields["entry_id"].src)

    @property
    def line_discrepancies(self) -> list[LinePair]:
        return [lp for lp in self.line_pairs if lp.diffs]

    def checks_for(self, doc: Document) -> DocChecks:
        return next((c for c in self.checks if c.doc is doc), DocChecks(doc))


def _sum(docs, attr="total") -> Optional[int]:
    vals = [getattr(d, attr) for d in docs]
    if any(v is None for v in vals):
        return None
    return sum(vals)


def bridge_for(g: DocGroup, cfg: RuleConfig) -> Bridge:
    """Split the document difference into named, additive components.

    The pair components form an identity:
      total_p - total_e = line_diff + (sub_p - Σlines_p) - (sub_e - Σlines_e)
                        + [rulePPN(sub_p) - rulePPN(sub_e)] + [ppn_p - rulePPN(sub_p)] - [ppn_e - rulePPN(sub_e)]
                        + [total_p - sub_p - ppn_p] - [total_e - sub_e - ppn_e]
    so 'unexplained' is non-zero only when a figure could not be read.
    """
    b = Bridge()
    pdf, erp = g.pdf, g.erp
    if pdf and not erp:
        b.missing_in_erp = sum(d.total or 0 for d in pdf)
        b.unexplained = 0 if _sum(pdf) is not None else None
        return b
    if erp and not pdf:
        b.missing_in_pdf = -sum(d.total or 0 for d in erp)
        return b
    b.duplicate_pdf = sum(d.total or 0 for d in pdf[1:])
    b.duplicate_erp = -sum(d.total or 0 for d in erp[1:])
    p, e = pdf[0], erp[0]
    diff = (p.total or 0) - (e.total or 0)
    if None in (p.subtotal, p.ppn, p.total, e.subtotal, e.ppn, e.total):
        b.unexplained = diff
        return b
    lp = sum(l.amount for l in p.lines)
    le = sum(l.amount for l in e.lines)
    b.line_diff = lp - le
    b.pdf_subtotal_error = p.subtotal - lp
    b.erp_subtotal_error = -(e.subtotal - le)
    rp, re_ = rule_ppn(p.subtotal, cfg), rule_ppn(e.subtotal, cfg)
    b.tax_on_subtotal_diff = rp - re_
    b.pdf_tax_calc = p.ppn - rp
    b.erp_tax_calc = -(e.ppn - re_)
    b.pdf_total_error = p.total - p.subtotal - p.ppn
    b.erp_total_error = -(e.total - e.subtotal - e.ppn)
    b.unexplained = diff - (b.explained - b.duplicate_pdf - b.duplicate_erp)
    return b


# ---------------------------------------------------------------------------

def reconcile_group(g: DocGroup, cfg: RuleConfig, min_conf: float = 0.9,
                    register: Optional[SupplierRegister] = None) -> DocResult:
    pdf, erp = g.pdf, g.erp
    first = (pdf or erp)[0]
    r = DocResult(key=g.key, doc_no=first.doc_no if pdf else erp[0].doc_no,
                  doc_type=first.doc_type, pdf_docs=pdf, erp_docs=erp,
                  pdf_total=_sum(pdf) if pdf else None, erp_total=_sum(erp) if erp else None,
                  difference=None)
    if pdf and pdf[0].doc_no:
        r.doc_no = pdf[0].doc_no
    r.difference = (r.pdf_total or 0) - (r.erp_total or 0) if (r.pdf_total is not None or r.erp_total is not None) else None
    codes: list[str] = []

    def add(c: str):
        code(c)                     # unknown codes fail here, not silently later
        if c not in codes:
            codes.append(c)

    # internal arithmetic on every document of the group (R7)
    for d in pdf + erp:
        chk = DocChecks(d, tuple(check_document(d, cfg)))
        r.checks.append(chk)
        for c in chk.hard:
            add(check_code(c.rule, c.target, d.side))
        if chk.rounding:
            add(f"{d.side}_TAX_ROUNDING")

    if len(erp) > 1:
        add("ERP_DUPLICATE_ENTRY")
    if len(pdf) > 1:
        add("PDF_DUPLICATE_DOC")
    if not erp:
        add("NOT_IN_ERP")
    if not pdf:
        add("NOT_IN_PDF")

    p = pdf[0] if pdf else None
    e = erp[0] if erp else None
    if p and e:
        if g.secondary:
            add("MATCHED_BY_SECONDARY_KEY")
        elif p.doc_no.strip().upper() != e.doc_no.strip().upper():
            add("DOC_NO_FORMAT_DIFF")
        if p.po_no and e.po_no and norm_po(p.po_no) != norm_po(e.po_no):
            add("PO_MISMATCH")
        if p.doc_type != e.doc_type:
            add("DOC_TYPE_MISMATCH")
        if p.doc_type == "CREDIT_NOTE" and p.reference and e.reference and \
                norm_po(p.reference) != norm_po(e.reference):
            add("CN_REFERENCE_MISMATCH")
        if p.doc_date and e.doc_date and p.doc_date != e.doc_date and not _ambiguous_date_agrees(p, e):
            add("DATE_MISMATCH")
        r.line_pairs = match_lines(p.lines, e.lines)
        for lp in r.line_pairs:
            for dfield in lp.diffs:
                add(LINE_CODE[dfield])
        pair_diff = (p.total or 0) - (e.total or 0)
        if pair_diff and abs(pair_diff) <= cfg.total_tolerance and not r.line_discrepancies:
            add("TOTAL_ROUNDING_DIFF")
        elif abs(pair_diff) > cfg.total_tolerance:
            add("TOTAL_DIFF")

    for d in erp:
        if d.normalisation_notes:
            add("ERP_VALUE_STORED_AS_TEXT")
        if d.status_in_source.lower() == "parked":
            add("ERP_PARKED")
    for d in pdf:
        if "llm" in d.method:
            add("EXTRACTED_BY_LLM")
        elif "ocr" in d.method:
            add("EXTRACTED_BY_OCR")
        if any("EXTRACTION_CONFLICT" in n for n in d.extraction_notes):
            add("EXTRACTION_CONFLICT")
        if len(d.pages) > 1:
            add("MULTI_PAGE_DOC")
    if any(d.unread_notes for d in pdf + erp):
        add("EXTRACTION_INCOMPLETE")
    if first.doc_type == "CREDIT_NOTE":
        add("CREDIT_NOTE")

    # confidence: extraction confidence, raised when the ERP independently corroborates every line
    conf = min([d.confidence for d in pdf] or [1.0])
    corroborated = bool(p and e and r.line_pairs and not r.line_discrepancies
                        and abs((p.total or 0) - (e.total or 0)) <= cfg.total_tolerance)
    if corroborated:
        conf = max(conf, 0.95)
    if g.secondary:
        conf = min(conf, 0.6)
    r.confidence = round(conf, 2)
    if r.confidence < min_conf:
        add("EXTRACTION_LOW_CONFIDENCE")

    # new supplier: held for a person whatever the reading path (status itself is unchanged)
    if register is not None:
        vendor = (erp or pdf)[0].vendor          # ERP vendor master data first
        r.new_supplier_note = register.review_reason(vendor, (pdf or erp)[0].doc_date)
        if r.new_supplier_note:
            add("NEW_SUPPLIER")

    r.reasons = codes
    r.status = status_for(codes)
    r.review_required = blocks_approval(codes)
    r.bridge = bridge_for(g, cfg)
    r.explanation, r.action, r.headline = explain(r, g, cfg)
    return r


def _ambiguous_date_agrees(p: Document, e: Document) -> bool:
    f = e.fields.get("doc_date_ambiguous")
    if not f:
        return False
    alt, _ = parse_date(f.raw, "DMY")
    alt2, _ = parse_date(f.raw, "MDY")
    return p.doc_date in (alt, alt2)


# --------------------------------------------------------------------------- batch

def reconcile_all(groups: list[DocGroup], cfg: RuleConfig, min_conf: float = 0.9,
                  register: Optional[SupplierRegister] = None) -> list[DocResult]:
    res = [reconcile_group(g, cfg, min_conf, register) for g in groups]
    # PDF order first (page), then ERP-only entries by entry id
    res.sort(key=lambda r: (0, min(r.pdf_docs[0].pages or [0])) if r.pdf_docs else (1, r.erp_docs[0].entry_id))
    return res


def extraction_failure_results(page_logs) -> list[DocResult]:
    """Pages that produced no document become explicit NEEDS_REVIEW rows."""
    out = []
    for log in page_logs:
        if log.role != "unassigned":
            continue
        reasons = ["EXTRACTION_FAILED"]
        r = DocResult(key=f"PAGE-{log.file}-{log.page}", doc_no=f"(unreadable page {log.page})", doc_type="UNKNOWN",
                      pdf_docs=[], erp_docs=[], pdf_total=None, erp_total=None, difference=None,
                      status=status_for(reasons), reasons=reasons, confidence=0.0,
                      review_required=blocks_approval(reasons))
        r.explanation = f"PDF {log.file} p.{log.page} could not be read into a document: {'; '.join(log.notes)}."
        r.action = "Open the page, key the document manually or add a supplier profile, then re-run the batch."
        r.headline = "Page could not be extracted"
        r.bridge = Bridge(unexplained=None)
        out.append(r)
    return out


def control_totals(results: list[DocResult], pdf_docs: list[Document], erp_docs: list[Document],
                   out_of_scope: list[Document]) -> ControlTotals:
    pdf_total = sum(d.total or 0 for d in pdf_docs)
    erp_total = sum(d.total or 0 for d in erp_docs)
    bridge = Bridge()
    unknown = []
    for r in results:
        for k, v in r.bridge.items():
            if v is None:
                unknown.append(r.doc_no)
            else:
                setattr(bridge, k, getattr(bridge, k) + v)
    explained = bridge.explained
    status_counts = {s: 0 for s in STATUSES}
    status_amounts = {s: 0 for s in STATUSES}
    for r in results:
        status_counts[r.status] += 1
        status_amounts[r.status] += r.difference or 0
    overbilled = sum(max(0, r.difference or 0) for r in results
                     if r.status in ("AMOUNT_MISMATCH", "NEEDS_REVIEW") and r.pdf_docs and r.erp_docs)
    return ControlTotals(
        pdf_documents=len(pdf_docs),
        pdf_invoices=sum(1 for d in pdf_docs if d.doc_type == "INVOICE"),
        pdf_credit_notes=sum(1 for d in pdf_docs if d.doc_type == "CREDIT_NOTE"),
        pdf_lines=sum(len(d.lines) for d in pdf_docs),
        erp_entries=len(erp_docs),
        erp_lines=sum(len(d.lines) for d in erp_docs),
        erp_out_of_scope=len(out_of_scope),
        pdf_total=pdf_total,
        erp_total=erp_total,
        difference=pdf_total - erp_total,
        sum_of_document_differences=sum(r.difference or 0 for r in results),
        bridge=bridge,
        explained=explained,
        unexplained=(pdf_total - erp_total) - explained,
        unknown_amount_docs=unknown,
        status_counts=status_counts,
        status_amounts=status_amounts,
        documents=len(results),
        review_required=sum(1 for r in results if r.review_required),
        auto_approvable=sum(1 for r in results if not r.review_required),
        exposure=Exposure(
            duplicate_erp=-bridge.duplicate_erp,
            overbilled_on_matched_docs=overbilled,
            not_booked_in_erp=bridge.missing_in_erp,
            booked_without_document=-bridge.missing_in_pdf,
        ),
    )
