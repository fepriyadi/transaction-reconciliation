from datetime import date

import pytest

from recon.config import load_register
from recon.matching import match_documents
from recon.onboarding import SupplierRegister
from recon.reconcile import reconcile_all
from recon.rules import RuleConfig
from tests.conftest import mkdoc, mkline

REG = SupplierRegister.from_dates({"PT PRIMA KERTAS INDONESIA": date(2026, 10, 1)}, review_days=31)


def test_register_window():
    assert REG.review_reason("PT Prima Kertas Indonesia", date(2026, 10, 15))     # first month
    assert REG.review_reason("PT PRIMA KERTAS INDONESIA", date(2026, 10, 31))
    assert REG.review_reason("PT PRIMA KERTAS INDONESIA", date(2026, 11, 1)) == ""   # established
    assert REG.review_reason("PT PRIMA KERTAS INDONESIA", None)                    # undated: stay safe
    assert "not in the supplier register" in REG.review_reason("PT Sagara Digital Supply", date(2027, 1, 1))


def test_register_loads_config(config_dir):
    reg = load_register(config_dir)
    assert reg.review_reason("PT PRIMA KERTAS INDONESIA", date(2026, 9, 15)) == ""    # September batch: established


def run(pdf, erp, register=REG):
    r, = reconcile_all(match_documents([pdf], [erp]), RuleConfig(), 0.9, register)
    return r


def pair(doc_date, method, vendor="PT PRIMA KERTAS INDONESIA"):
    lines = lambda: [mkline("A", 99, 13900, 10), mkline("B", 13, 112500)]
    p = mkdoc("PDF", "I/1", lines(), vendor=vendor)
    e = mkdoc("ERP", "I/1", lines(), entry_id="E1", vendor=vendor)
    p.doc_date = e.doc_date = doc_date
    p.method, p.confidence = method, (0.99 if method == "text_layer" else 0.6)
    return p, e


@pytest.mark.parametrize("method", ["text_layer", "llm_text"])
def test_first_month_is_reviewed_whatever_the_reading_path(method):
    r = run(*pair(date(2026, 10, 15), method))
    assert r.status == "MATCHED" and r.review_required and "NEW_SUPPLIER" in r.reasons
    assert r.action.startswith("New supplier")


@pytest.mark.parametrize("method", ["text_layer", "llm_text"])
def test_established_supplier_matched_can_be_approved(method):
    r = run(*pair(date(2026, 11, 15), method))
    assert r.status == "MATCHED" and not r.review_required and "NEW_SUPPLIER" not in r.reasons


def test_unregistered_supplier_is_always_reviewed():
    r = run(*pair(date(2027, 6, 1), "text_layer", vendor="PT SAGARA DIGITAL SUPPLY"))
    assert r.review_required and "NEW_SUPPLIER" in r.reasons
