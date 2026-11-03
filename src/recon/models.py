"""Core records. Every figure carries a SourceRef so it can be traced back."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from typing import Optional


@dataclass(frozen=True)
class SourceRef:
    """Where a figure came from: a PDF page (+ bbox) or an ERP sheet cell."""
    file: str
    page: Optional[int] = None          # 1-based PDF page
    bbox: Optional[tuple] = None        # (x0, y0, x1, y1) in PDF points
    sheet: Optional[str] = None
    row: Optional[int] = None           # 1-based Excel row
    cell: Optional[str] = None          # e.g. "J5"
    method: str = ""                    # text_layer | ocr | llm_vision | llm_text | erp

    def label(self) -> str:
        if self.sheet:
            return f"{self.file} › {self.sheet}!{self.cell or ('row ' + str(self.row))}"
        loc = f"p.{self.page}"
        if self.bbox:
            x0, y0, x1, y1 = (round(v) for v in self.bbox)
            loc += f" @({x0},{y0},{x1},{y1})"
        return f"{self.file} › {loc} [{self.method}]"


@dataclass
class Field:
    """A single extracted value with its raw text and provenance."""
    value: object
    raw: str = ""
    src: Optional[SourceRef] = None


@dataclass
class Line:
    line_no: Optional[int]
    sku: str
    description: str
    qty: Fraction
    uom: str
    unit_price: Fraction
    disc_pct: Fraction
    amount: int
    fields: dict = field(default_factory=dict)   # name -> Field (provenance)

    @property
    def sku_key(self) -> str:
        from .normalize import norm_sku
        return norm_sku(self.sku)


@dataclass
class Document:
    side: str                       # "PDF" | "ERP"
    doc_type: str                   # "INVOICE" | "CREDIT_NOTE"
    doc_no: str
    doc_date: Optional[date]
    vendor: str
    po_no: str = ""
    reference: str = ""             # original invoice for credit notes
    subtotal: Optional[int] = None
    dpp: Optional[int] = None
    ppn: Optional[int] = None
    total: Optional[int] = None
    lines: list = field(default_factory=list)
    fields: dict = field(default_factory=dict)   # header field name -> Field
    entry_id: str = ""              # ERP entry id
    status_in_source: str = ""      # e.g. ERP Posted/Parked
    pages: list = field(default_factory=list)    # PDF pages this doc spans
    method: str = ""                # extraction method
    confidence: float = 1.0         # extraction confidence 0..1
    extraction_notes: list = field(default_factory=list)

    @property
    def key(self) -> str:
        from .normalize import norm_doc_no
        return norm_doc_no(self.doc_no)

    @property
    def unread_notes(self) -> list[str]:
        """Printed figures that could not be read (arithmetic flags on this side are then unreliable)."""
        return [n for n in self.extraction_notes if n.startswith("EXTRACTION_INCOMPLETE")]

    @property
    def normalisation_notes(self) -> list[str]:
        return [n for n in self.extraction_notes if not n.startswith("EXTRACTION_INCOMPLETE")]

    @property
    def ref_label(self) -> str:
        if self.side == "ERP":
            return self.entry_id
        return "p." + ",".join(str(p) for p in self.pages)


@dataclass
class CheckResult:
    """Outcome of an internal arithmetic check (R1–R4) on one document."""
    rule: str          # R1..R4
    target: str        # e.g. "line 3", "subtotal"
    expected: int      # value the rule produces
    stated: int        # value printed / booked
    passed: bool
    rounding_only: bool = False   # failed by <= tolerance on a tax figure

    @property
    def delta(self) -> int:
        return self.stated - self.expected


@dataclass(frozen=True, eq=False)
class DocChecks:
    """R7 results for one document, held by the reconciliation result (never written onto the Document)."""
    doc: Document
    results: tuple = ()

    @property
    def hard(self) -> list[CheckResult]:
        """Failed by more than rounding — these force NEEDS_REVIEW."""
        return [c for c in self.results if not c.passed and not c.rounding_only]

    @property
    def rounding(self) -> list[CheckResult]:
        return [c for c in self.results if not c.passed and c.rounding_only]
