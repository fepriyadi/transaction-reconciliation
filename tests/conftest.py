import sys
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recon.models import Document, Line  # noqa: E402
from recon.rules import expected_totals, line_amount  # noqa: E402


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
