import shutil
from fractions import Fraction
from pathlib import Path

import pytest

from recon.models import Document, Line
from recon.rules import expected_totals, line_amount

ROOT = Path(__file__).resolve().parent.parent


def mkline(sku, qty, price, disc=0, amount=None, line_no=None, uom="PCS", desc=""):
    amt = line_amount(qty, price, disc) if amount is None else amount
    return Line(line_no, sku, desc or sku, Fraction(qty), uom, Fraction(price), Fraction(disc), amt)


def mkdoc(side, doc_no, lines, po="PO-1", doc_type="INVOICE", entry_id="", override=None, vendor="PT PRIMA KERTAS INDONESIA",
          reference=""):
    """Build a document whose header follows R2-R4 unless overridden."""
    for i, l in enumerate(lines, 1):
        if l.line_no is None:
            l.line_no = i
    sub = sum(l.amount for l in lines)
    t = expected_totals(sub)
    t.update(override or {})
    d = Document(side=side, doc_type=doc_type, doc_no=doc_no, doc_date=None, vendor=vendor, po_no=po,
                 reference=reference, subtotal=t["subtotal"], dpp=t["dpp"], ppn=t["ppn"], total=t["total"],
                 lines=lines, entry_id=entry_id, pages=[1] if side == "PDF" else [])
    return d


@pytest.fixture
def data_dir():
    return ROOT / "data"


@pytest.fixture
def config_dir():
    return ROOT / "config"


@pytest.fixture(scope="session")
def september_batch():
    """The September 2026 sample run once, deterministic path (no LLM); shared by every test that reads it."""
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract not installed (needed for scanned p.10)")
    from recon.pipeline import run_batch
    return run_batch([ROOT / "data/Supplier_Invoices_PT_Prima_Kertas_Sep2026.pdf"],
                     ROOT / "data/ERP_AP_Export_Sep2026.xlsx", ROOT / "config", llm=None)
