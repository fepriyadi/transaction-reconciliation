import copy

import pytest

from recon.matching import match_documents
from recon.reason_codes import (REASON_CODES, blocks_approval, highlighted_fields, registry_problems, status_for,
                                status_rank)
from recon.reconcile import reconcile_all
from recon.rules import RuleConfig
from tests.conftest import mkdoc, mkline


def test_registry_is_consistent():
    assert registry_problems() == []


def test_every_status_forcing_code_blocks_auto_approval():
    for c in REASON_CODES.values():
        if c.status and c.status not in ("MATCHED", "MATCHED_WITHIN_TOLERANCE"):
            assert blocks_approval([c.code]), c.code


@pytest.mark.parametrize("codes, status", [
    ([], "MATCHED"),
    (["DOC_NO_FORMAT_DIFF", "CREDIT_NOTE"], "MATCHED"),
    (["TOTAL_ROUNDING_DIFF", "ERP_TAX_ROUNDING"], "MATCHED_WITHIN_TOLERANCE"),
    (["LINE_QTY_DIFF", "TOTAL_DIFF"], "AMOUNT_MISMATCH"),
    (["LINE_QTY_DIFF", "PDF_SUBTOTAL_ERROR"], "NEEDS_REVIEW"),       # internal error beats amount mismatch
    (["PO_MISMATCH", "NOT_IN_ERP"], "MISSING_IN_ERP"),
    (["NOT_IN_PDF", "ERP_DUPLICATE_ENTRY"], "DUPLICATE_IN_ERP"),
    (["NEW_SUPPLIER"], "MATCHED"),                                   # held for a person, status unchanged
])
def test_status_precedence(codes, status):
    assert status_for(codes) == status


def test_new_supplier_blocks_approval_without_changing_status():
    assert blocks_approval(["NEW_SUPPLIER"]) and not blocks_approval(["DATE_MISMATCH", "EXTRACTED_BY_LLM"])


def test_unknown_code_fails_loudly():
    with pytest.raises(KeyError, match="unknown reason code"):
        status_for(["LINE_SKU_ALIAS"])


def test_review_queue_order_follows_precedence():
    assert status_rank("DUPLICATE_IN_ERP") < status_rank("NEEDS_REVIEW") < status_rank("AMOUNT_MISMATCH")


def test_highlighted_fields():
    assert highlighted_fields(["PO_MISMATCH", "PDF_DPP_ERROR", "ERP_DPP_ERROR"]) == {"po_no", "dpp"}


def test_reconciliation_does_not_modify_its_input_documents():
    lines = lambda: [mkline("A", 99, 13900, 10), mkline("B", 13, 112500)]
    p = mkdoc("PDF", "I/1", lines(), override={"subtotal": 1})
    e = mkdoc("ERP", "I/1", lines(), entry_id="E1")
    before = copy.deepcopy((vars(p), vars(e)))
    r, = reconcile_all(match_documents([p], [e]), RuleConfig())
    assert (vars(p), vars(e)) == before
    assert r.checks_for(p).hard and not r.checks_for(e).hard
