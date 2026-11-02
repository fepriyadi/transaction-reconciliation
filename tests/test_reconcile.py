import random
from fractions import Fraction

import pytest

from recon.matching import match_documents
from recon.reconcile import control_totals, reconcile_all
from recon.rules import RuleConfig, expected_totals
from tests.conftest import mkdoc, mkline

CFG = RuleConfig()


def run(pdf, erp):
    return reconcile_all(match_documents(pdf, erp), CFG)


def base_lines():
    return [mkline("A", 99, 13900, 10), mkline("B", 65, 4750, 5), mkline("C", 13, 112500)]


def test_matched():
    r, = run([mkdoc("PDF", "I/1", base_lines())], [mkdoc("ERP", "I/1", base_lines(), entry_id="E1")])
    assert r.status == "MATCHED" and r.difference == 0 and not r.review_required


def test_within_tolerance_when_only_tax_rounding_differs():
    p = mkdoc("PDF", "I/1", base_lines())
    e = mkdoc("ERP", "I/1", base_lines(), entry_id="E1", override={"ppn": p.ppn + 1, "total": p.total + 1})
    r, = run([p], [e])
    assert r.status == "MATCHED_WITHIN_TOLERANCE" and r.difference == -1
    assert "ERP_TAX_ROUNDING" in r.reasons


def test_small_total_difference_with_line_difference_is_not_tolerance():
    p = mkdoc("PDF", "I/1", [mkline("A", 1, 5)])
    e = mkdoc("ERP", "I/1", [mkline("A", 1, 4)], entry_id="E1")
    r, = run([p], [e])
    assert r.status == "AMOUNT_MISMATCH" and abs(r.difference) <= 10


def test_amount_mismatch_quantity():
    pl = base_lines(); pl[1] = mkline("B", 120, 4750, 5)
    r, = run([mkdoc("PDF", "I/1", pl)], [mkdoc("ERP", "I/1", base_lines(), entry_id="E1")])
    assert r.status == "AMOUNT_MISMATCH" and "LINE_QTY_DIFF" in r.reasons
    assert "quantity 120" in r.explanation


def test_missing_both_ways():
    res = run([mkdoc("PDF", "I/1", base_lines())], [mkdoc("ERP", "I/2", base_lines(), entry_id="E2", po="PO-2")])
    assert {r.status for r in res} == {"MISSING_IN_ERP", "MISSING_IN_PDF"}


def test_duplicate_in_erp_difference_is_the_extra_copy():
    p = mkdoc("PDF", "I/1", base_lines())
    e1 = mkdoc("ERP", "I/1", base_lines(), entry_id="E1")
    e2 = mkdoc("ERP", "I/1", base_lines(), entry_id="E2")
    r, = run([p], [e1, e2])
    assert r.status == "DUPLICATE_IN_ERP" and r.difference == -e2.total
    assert "E2" in r.action


def test_duplicate_in_pdf():
    p1 = mkdoc("PDF", "I/1", base_lines())
    p2 = mkdoc("PDF", "I/1", base_lines())
    r, = run([p1, p2], [mkdoc("ERP", "I/1", base_lines(), entry_id="E1")])
    assert r.status == "DUPLICATE_IN_PDF" and r.difference == p2.total


def test_po_mismatch_needs_review_even_when_amounts_agree():
    r, = run([mkdoc("PDF", "I/1", base_lines(), po="PO-88239")],
             [mkdoc("ERP", "I/1", base_lines(), po="PO-88293", entry_id="E1")])
    assert r.status == "NEEDS_REVIEW" and "PO_MISMATCH" in r.reasons and r.difference == 0
    assert "transposed" in r.explanation


def test_internal_error_beats_amount_mismatch():
    lines = base_lines()
    wrong = expected_totals(sum(l.amount for l in lines) + 100_000)
    r, = run([mkdoc("PDF", "I/1", lines, override=wrong)], [mkdoc("ERP", "I/1", base_lines(), entry_id="E1")])
    assert r.status == "NEEDS_REVIEW" and "PDF_SUBTOTAL_ERROR" in r.reasons
    assert r.bridge.pdf_subtotal_error == 100_000


def test_credit_note_reference_mismatch():
    cn = [mkline("A", -15, 44000)]
    r, = run([mkdoc("PDF", "CN/1", cn, doc_type="CREDIT_NOTE", reference="INV/8/131")],
             [mkdoc("ERP", "CN/1", [mkline("A", -15, 44000)], doc_type="CREDIT_NOTE", reference="INV/8/130", entry_id="E1")])
    assert r.status == "NEEDS_REVIEW" and "CN_REFERENCE_MISMATCH" in r.reasons


def test_credit_note_matched_and_negative():
    r, = run([mkdoc("PDF", "CN/1", [mkline("A", -15, 44000)], doc_type="CREDIT_NOTE")],
             [mkdoc("ERP", "CN/1", [mkline("A", -15, 44000)], doc_type="CREDIT_NOTE", entry_id="E1")])
    assert r.status == "MATCHED" and r.pdf_total < 0


def test_secondary_match_is_flagged_for_review():
    r, = run([mkdoc("PDF", "INV/0150", base_lines(), po="PO-7")],
             [mkdoc("ERP", "INV/0105", base_lines(), po="PO-7", entry_id="E1")])
    assert r.status == "NEEDS_REVIEW" and "MATCHED_BY_SECONDARY_KEY" in r.reasons


def test_low_extraction_confidence_blocks_auto_approval_unless_corroborated():
    p = mkdoc("PDF", "I/1", base_lines()); p.confidence = 0.5
    r, = run([p], [mkdoc("ERP", "I/1", base_lines(), entry_id="E1")])
    assert r.status == "MATCHED" and r.confidence >= 0.95     # every line corroborated by ERP
    p2 = mkdoc("PDF", "I/2", base_lines()); p2.confidence = 0.5
    r2, = run([p2], [])
    assert "EXTRACTION_LOW_CONFIDENCE" in r2.reasons and r2.review_required


def test_unread_figure_is_an_extraction_problem_not_a_supplier_error():
    # PDF line amount shows the 5% discount, but the discount itself was not read
    p = mkdoc("PDF", "I/1", [mkline("A", 2, 12500000, 0, amount=23750000)])
    p.extraction_notes.append("EXTRACTION_INCOMPLETE: p.1 LLM line 1 has an unreadable figure")
    r, = run([p], [mkdoc("ERP", "I/1", [mkline("A", 2, 12500000, 5)], entry_id="E1")])
    assert r.status == "NEEDS_REVIEW" and "EXTRACTION_INCOMPLETE" in r.reasons and r.review_required
    assert "ask the supplier" not in r.action and r.action.startswith("Read the unreadable figure")
    assert r.headline.startswith("A figure could not be read")


@pytest.mark.parametrize("seed", range(25))
def test_batch_bridge_always_fully_explains_difference(seed):
    """Property test: random perturbations of either side never leave an unexplained remainder."""
    rnd = random.Random(seed)
    pdf, erp = [], []
    for i in range(8):
        lines = [mkline(f"S{j}", rnd.randint(1, 200), rnd.choice([3250, 13900, 49500, 108000]),
                        rnd.choice([0, Fraction(5, 2), 5, 10])) for j in range(rnd.randint(1, 6))]
        e_lines = [mkline(l.sku, l.qty, l.unit_price, l.disc_pct) for l in lines]
        kind = rnd.choice(["same", "qty", "price", "extra", "sub_err", "ppn_round", "missing_erp", "missing_pdf", "dup_erp"])
        if kind == "qty":
            lines[0] = mkline(lines[0].sku, lines[0].qty + 5, lines[0].unit_price, lines[0].disc_pct)
        if kind == "price":
            lines[0] = mkline(lines[0].sku, lines[0].qty, lines[0].unit_price + 1000, lines[0].disc_pct)
        if kind == "extra":
            lines.append(mkline("DLV", 1, 250000))
        override = None
        if kind == "sub_err":
            override = expected_totals(sum(l.amount for l in lines) + rnd.randint(1, 99999))
        p = mkdoc("PDF", f"I/{i}", lines, override=override)
        e = mkdoc("ERP", f"I/{i}", e_lines, entry_id=f"E{i}")
        if kind == "ppn_round":
            e.ppn += 1; e.total += 1
        if kind != "missing_erp":
            erp.append(e)
        if kind == "dup_erp":
            erp.append(mkdoc("ERP", f"I/{i}", e_lines, entry_id=f"E{i}b"))
        if kind != "missing_pdf":
            pdf.append(p)
    res = run(pdf, erp)
    t = control_totals(res, pdf, erp, [])
    assert t.difference == t.sum_of_document_differences
    assert t.unexplained == 0
