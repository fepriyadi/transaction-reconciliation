"""End-to-end regression on the September 2026 sample (deterministic path, no LLM)."""
import shutil
from pathlib import Path

import pytest

from recon.report import write_reports

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed (needed for scanned p.10)")

EXPECTED = {
    "PKI/INV/2026/09/0141": ("MATCHED", 0),
    "PKI/INV/2026/09/0142": ("MATCHED", 0),                       # ERP number uses dashes
    "PKI/INV/2026/09/0143": ("MATCHED_WITHIN_TOLERANCE", -1),     # ERP PPN rounded differently
    "PKI/INV/2026/09/0144": ("AMOUNT_MISMATCH", 527250),          # qty 120 vs 100
    "PKI/INV/2026/09/0145": ("AMOUNT_MISMATCH", 666000),          # price 52,500 vs 49,500
    "PKI/INV/2026/09/0146": ("MISSING_IN_ERP", 15688629),
    "PKI/INV/2026/09/0147": ("DUPLICATE_IN_ERP", -17238178),      # E-10456 + E-10460
    "PKI/INV/2026/09/0148": ("NEEDS_REVIEW", 204841),             # DPP not 11/12
    "PKI/INV/2026/09/0149": ("NEEDS_REVIEW", 111000),             # subtotal ≠ Σ lines
    "PKI/INV/2026/09/0150": ("MATCHED", 0),                       # scanned page, OCR
    "PKI/INV/2026/09/0151": ("AMOUNT_MISMATCH", 277500),          # delivery fee line
    "PKI/INV/2026/09/0152": ("MATCHED", 0),                       # ERP text date/amount
    "PKI/INV/2026/09/0153": ("MATCHED", 0),                       # 2 pages, 34 lines
    "PKI/INV/2026/09/0154": ("NEEDS_REVIEW", 0),                  # PO-88239 vs PO-88293
    "PKI/CN/2026/09/0012": ("MATCHED", 0),                        # credit note
    "PKI/INV/2026/09/0155": ("MISSING_IN_PDF", -3493714),         # parked, no invoice
}


@pytest.fixture
def batch(september_batch):
    return september_batch


def test_every_document_has_expected_status_and_difference(batch):
    got = {r.doc_no: (r.status, r.difference) for r in batch.results}
    assert got == EXPECTED


def test_every_page_accounted_for(batch):
    assert len(batch.page_logs) == 16
    assert all(l.role in ("start", "continuation") for l in batch.page_logs)
    assert [l.role for l in batch.page_logs if l.page in (13, 14)] == ["start", "continuation"]


def test_control_totals_fully_explained(batch):
    t = batch.totals
    assert t.pdf_total == 307_896_675
    assert t.erp_total == 311_153_348
    assert t.difference == t.sum_of_document_differences == -3_256_673
    assert t.unexplained == 0 and t.fully_explained


def test_every_figure_has_a_source(batch):
    for r in batch.results:
        for d in r.pdf_docs + r.erp_docs:
            for k in ("total",):
                assert d.fields[k].src is not None, (r.doc_no, d.side, k)
            for ln in d.lines:
                assert ln.fields["amount"].src is not None
                if d.side == "PDF":
                    assert ln.fields["amount"].src.page in d.pages and ln.fields["amount"].src.bbox
                else:
                    assert ln.fields["amount"].src.sheet == "AP_Lines"


def test_reports_written(batch, tmp_path):
    paths = write_reports(batch, tmp_path)
    import pandas as pd
    docs = pd.read_csv(paths["documents_csv"])
    assert paths["documents_csv"].name == "document_report.csv"       # not reconciliation_report.*, the Excel's name
    assert len(docs) == 16
    assert {"PDF total", "ERP total", "Difference (PDF-ERP)", "Status", "Reason codes", "Explanation",
            "Suggested action"} <= set(docs.columns)
    lines = pd.read_csv(paths["lines_csv"])
    assert set(lines["Document no."]) == {"PKI/INV/2026/09/0144", "PKI/INV/2026/09/0145", "PKI/INV/2026/09/0151"}


# ERP_AP_Export_Sep2026_corrected.xlsx: the same export after AP fixed the ERP-side errors above
# (0144, 0145, 0151 lines, 0147 duplicate and 0155 parked entry removed, 0154 PO, 0152 amount as a number).
# What is left is not the ERP's to fix: 0146 still to be booked, 0148/0149 errors on the supplier's PDF.
CORRECTED_CHANGES = {
    "PKI/INV/2026/09/0144": ("MATCHED", 0),
    "PKI/INV/2026/09/0145": ("MATCHED", 0),
    "PKI/INV/2026/09/0147": ("MATCHED", 0),
    "PKI/INV/2026/09/0151": ("MATCHED", 0),
    "PKI/INV/2026/09/0154": ("MATCHED", 0),
}


@pytest.fixture(scope="module")
def corrected_batch():
    from recon.pipeline import run_batch
    root = Path(__file__).resolve().parent.parent
    return run_batch([root / "data/Supplier_Invoices_PT_Prima_Kertas_Sep2026.pdf"],
                     root / "data/ERP_AP_Export_Sep2026_corrected.xlsx", root / "config", llm=None)


def test_corrected_erp_leaves_only_what_the_erp_cannot_fix(corrected_batch):
    expected = {k: v for k, v in EXPECTED.items() if k != "PKI/INV/2026/09/0155"} | CORRECTED_CHANGES
    assert {r.doc_no: (r.status, r.difference) for r in corrected_batch.results} == expected
    t = corrected_batch.totals
    assert (t.pdf_total, t.erp_total) == (307_896_675, 291_892_206)
    assert t.difference == 15_688_629 + 204_841 + 111_000 - 1
    assert t.unexplained == 0 and t.fully_explained
