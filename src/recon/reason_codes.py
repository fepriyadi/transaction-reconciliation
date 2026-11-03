"""Reason codes (R8) and the status policy derived from them.

This module is the single owner of "what does a code mean for the document":
its severity (info = no action, warn = check, review = blocks auto-approval),
the status it forces, and which PDF figures it puts in red. Status, review
requirement, review-queue order and audit highlighting are all read from here;
nothing else matches code names by prefix or suffix.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

# Display order (reports, filters, counts).
STATUSES = ["MATCHED", "MATCHED_WITHIN_TOLERANCE", "AMOUNT_MISMATCH", "MISSING_IN_ERP",
            "MISSING_IN_PDF", "DUPLICATE_IN_ERP", "DUPLICATE_IN_PDF", "NEEDS_REVIEW"]

# Precedence: when several codes force a status, the earliest one here wins.
# Also the order of the review queue (most urgent first).
STATUS_PRECEDENCE = ("DUPLICATE_IN_ERP", "DUPLICATE_IN_PDF", "MISSING_IN_ERP", "MISSING_IN_PDF",
                     "NEEDS_REVIEW", "AMOUNT_MISMATCH", "MATCHED_WITHIN_TOLERANCE", "MATCHED")

APPROVABLE_STATUSES = frozenset({"MATCHED", "MATCHED_WITHIN_TOLERANCE"})
EXISTENCE_STATUSES = frozenset({"DUPLICATE_IN_ERP", "DUPLICATE_IN_PDF", "MISSING_IN_ERP", "MISSING_IN_PDF"})


@dataclass(frozen=True)
class ReasonCode:
    code: str
    severity: str                   # info | warn | review
    group: str                      # identity | existence | line | pdf_arithmetic | erp_arithmetic | rounding | totals | extraction | supplier | document
    meaning: str
    status: Optional[str] = None    # status this code forces (highest precedence wins)
    highlight: tuple = ()           # PDF header fields shown as discrepant in the audit view
    short: str = ""                 # headline phrase, where one is used


_R = ReasonCode
_CODES = [
    # identity / references
    _R("DOC_NO_FORMAT_DIFF", "info", "identity", "Document number written differently on each side (same number after normalisation)."),
    _R("MATCHED_BY_SECONDARY_KEY", "review", "identity", "Document numbers differ; paired on PO/date + total instead.", "NEEDS_REVIEW"),
    _R("PO_MISMATCH", "review", "identity", "PO number on the invoice differs from the PO booked in the ERP.", "NEEDS_REVIEW", ("po_no",)),
    _R("CN_REFERENCE_MISMATCH", "review", "identity", "Credit note refers to a different original invoice than the ERP.", "NEEDS_REVIEW", ("reference",)),
    _R("DOC_TYPE_MISMATCH", "review", "identity", "One side is an invoice, the other a credit note.", "NEEDS_REVIEW"),
    _R("DATE_MISMATCH", "warn", "identity", "Document date differs between PDF and ERP."),
    _R("ERP_VALUE_STORED_AS_TEXT", "info", "identity", "ERP cell held a number or date as text; normalised for comparison."),
    _R("ERP_PARKED", "info", "identity", "ERP entry is parked (not posted)."),
    # existence
    _R("NOT_IN_ERP", "review", "existence", "Supplier document has no ERP entry.", "MISSING_IN_ERP"),
    _R("NOT_IN_PDF", "review", "existence", "ERP entry has no supplier document in this batch.", "MISSING_IN_PDF"),
    _R("ERP_DUPLICATE_ENTRY", "review", "existence", "Same supplier document booked more than once in the ERP.", "DUPLICATE_IN_ERP"),
    _R("PDF_DUPLICATE_DOC", "review", "existence", "Same document number appears more than once in the PDF batch.", "DUPLICATE_IN_PDF"),
    # line level (PDF vs ERP)
    _R("LINE_QTY_DIFF", "review", "line", "Quantity differs on a line.", "AMOUNT_MISMATCH"),
    _R("LINE_PRICE_DIFF", "review", "line", "Unit price differs on a line.", "AMOUNT_MISMATCH"),
    _R("LINE_DISC_DIFF", "review", "line", "Discount % differs on a line.", "AMOUNT_MISMATCH"),
    _R("LINE_UOM_DIFF", "review", "line", "Unit of measure differs on a line.", "AMOUNT_MISMATCH"),
    _R("LINE_AMOUNT_DIFF", "review", "line", "Line amount differs although qty, price and discount agree.", "AMOUNT_MISMATCH"),
    _R("LINE_EXTRA_IN_PDF", "review", "line", "Invoice has a line that is not in the ERP entry.", "AMOUNT_MISMATCH"),
    _R("LINE_MISSING_FROM_PDF", "review", "line", "ERP entry has a line that is not on the invoice.", "AMOUNT_MISMATCH"),
    # document's own arithmetic (R7)
    _R("PDF_LINE_CALC_ERROR", "review", "pdf_arithmetic", "Invoice line amount ≠ qty × price × (1 − disc%) (R1).", "NEEDS_REVIEW",
       short="line amount ≠ qty × price"),
    _R("PDF_SUBTOTAL_ERROR", "review", "pdf_arithmetic", "Invoice subtotal ≠ sum of its lines (R2).", "NEEDS_REVIEW", ("subtotal",),
       short="subtotal ≠ sum of lines"),
    _R("PDF_DPP_ERROR", "review", "pdf_arithmetic", "Invoice DPP ≠ subtotal × 11/12 (R3).", "NEEDS_REVIEW", ("dpp",),
       short="PPN charged on wrong tax base"),
    _R("PDF_PPN_ERROR", "review", "pdf_arithmetic", "Invoice PPN ≠ DPP × 12% (R4).", "NEEDS_REVIEW", ("ppn",),
       short="PPN ≠ DPP × 12%"),
    _R("PDF_TOTAL_ERROR", "review", "pdf_arithmetic", "Invoice total ≠ subtotal + PPN (R4).", "NEEDS_REVIEW", ("total",),
       short="total ≠ subtotal + PPN"),
    _R("ERP_LINE_CALC_ERROR", "review", "erp_arithmetic", "ERP line amount ≠ qty × price × (1 − disc%) (R1).", "NEEDS_REVIEW"),
    _R("ERP_SUBTOTAL_ERROR", "review", "erp_arithmetic", "ERP subtotal ≠ sum of its lines (R2).", "NEEDS_REVIEW"),
    _R("ERP_DPP_ERROR", "review", "erp_arithmetic", "ERP DPP ≠ subtotal × 11/12 (R3).", "NEEDS_REVIEW"),
    _R("ERP_PPN_ERROR", "review", "erp_arithmetic", "ERP PPN ≠ DPP × 12% (R4).", "NEEDS_REVIEW"),
    _R("ERP_TOTAL_ERROR", "review", "erp_arithmetic", "ERP total ≠ subtotal + PPN (R4).", "NEEDS_REVIEW"),
    _R("PDF_TAX_ROUNDING", "info", "rounding", "Invoice tax figure deviates from R3/R4 by rounding only (≤ tolerance)."),
    _R("ERP_TAX_ROUNDING", "info", "rounding", "ERP tax figure deviates from R3/R4 by rounding only (≤ tolerance)."),
    # totals
    _R("TOTAL_ROUNDING_DIFF", "info", "totals", "Totals differ by ≤ IDR 10 with no line differences (R5).", "MATCHED_WITHIN_TOLERANCE"),
    _R("TOTAL_DIFF", "review", "totals", "Totals differ by more than the rounding tolerance.", "AMOUNT_MISMATCH"),
    # extraction
    _R("EXTRACTED_BY_OCR", "info", "extraction", "Figures read by OCR from a scanned page."),
    _R("EXTRACTED_BY_LLM", "warn", "extraction", "Figures read by an LLM (no supplier layout profile, or OCR fallback)."),
    _R("EXTRACTION_LOW_CONFIDENCE", "review", "extraction", "Extraction confidence below the auto-approval threshold.", "NEEDS_REVIEW"),
    _R("EXTRACTION_CONFLICT", "review", "extraction", "Two independent reads of the page disagree.", "NEEDS_REVIEW"),
    _R("EXTRACTION_FAILED", "review", "extraction", "Page could not be turned into a document.", "NEEDS_REVIEW"),
    _R("EXTRACTION_INCOMPLETE", "review", "extraction",
       "A printed figure could not be read; arithmetic flags on that side may come from the unread figure.", "NEEDS_REVIEW"),
    # supplier / document
    _R("NEW_SUPPLIER", "review", "supplier",
       "Supplier in its first month, or not in the supplier register: a person approves every document."),
    _R("MULTI_PAGE_DOC", "info", "document", "Document spans several PDF pages."),
    _R("CREDIT_NOTE", "info", "document", "Credit note: reduces the amount owed (R6)."),
]

REASON_CODES: dict[str, ReasonCode] = {c.code: c for c in _CODES}

# R7 check (rule, side) -> code; the R4 total check has its own code (see check_code).
CHECK_CODE = {
    ("R1", "PDF"): "PDF_LINE_CALC_ERROR", ("R2", "PDF"): "PDF_SUBTOTAL_ERROR",
    ("R3", "PDF"): "PDF_DPP_ERROR", ("R4", "PDF"): "PDF_PPN_ERROR",
    ("R1", "ERP"): "ERP_LINE_CALC_ERROR", ("R2", "ERP"): "ERP_SUBTOTAL_ERROR",
    ("R3", "ERP"): "ERP_DPP_ERROR", ("R4", "ERP"): "ERP_PPN_ERROR",
}

LINE_CODE = {
    "qty": "LINE_QTY_DIFF", "unit_price": "LINE_PRICE_DIFF", "disc_pct": "LINE_DISC_DIFF",
    "uom": "LINE_UOM_DIFF", "amount": "LINE_AMOUNT_DIFF",
    "extra_in_pdf": "LINE_EXTRA_IN_PDF", "missing_from_pdf": "LINE_MISSING_FROM_PDF",
}


def code(c: str) -> ReasonCode:
    """Registry lookup; an unknown code is a programming error, not a silent 'review'."""
    try:
        return REASON_CODES[c]
    except KeyError:
        raise KeyError(f"unknown reason code {c!r}: add it to recon/reason_codes.py") from None


def severity(c: str) -> str:
    return code(c).severity


def check_code(rule: str, target: str, side: str) -> str:
    if rule == "R4" and target == "total":
        return f"{side}_TOTAL_ERROR"
    return CHECK_CODE[(rule, side)]


def status_for(codes: Iterable[str]) -> str:
    forced = [s for s in (code(c).status for c in codes) if s]
    return min(forced, key=STATUS_PRECEDENCE.index, default="MATCHED")


def blocking(codes: Iterable[str]) -> list[str]:
    """The codes that hold a document for a person (severity 'review')."""
    return [c for c in codes if code(c).severity == "review"]


def blocks_approval(codes: Iterable[str]) -> bool:
    return bool(blocking(codes))


def in_group(codes: Iterable[str], group: str) -> list[str]:
    """Codes of one group, in the order given."""
    return [c for c in codes if code(c).group == group]


def highlighted_fields(codes: Iterable[str]) -> set[str]:
    return {f for c in codes for f in code(c).highlight}


def status_rank(status: str) -> int:
    return STATUS_PRECEDENCE.index(status)


def registry_problems() -> list[str]:
    """Invariants of the policy table (checked by the test suite)."""
    out = []
    for c in _CODES:
        if c.severity not in ("info", "warn", "review"):
            out.append(f"{c.code}: unknown severity {c.severity}")
        if c.status is not None and c.status not in STATUSES:
            out.append(f"{c.code}: unknown status {c.status}")
        if c.status is not None and c.status not in APPROVABLE_STATUSES and c.severity != "review":
            out.append(f"{c.code}: forces {c.status} but does not block approval")
    if len(REASON_CODES) != len(_CODES):
        out.append("duplicate reason code")
    for name in list(CHECK_CODE.values()) + list(LINE_CODE.values()) + ["PDF_TOTAL_ERROR", "ERP_TOTAL_ERROR"]:
        if name not in REASON_CODES:
            out.append(f"{name}: mapped but not registered")
    return out
