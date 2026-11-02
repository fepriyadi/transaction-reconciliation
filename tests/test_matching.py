from fractions import Fraction

from recon.matching import match_documents, match_lines, scope_erp
from tests.conftest import mkdoc, mkline


def test_lines_match_by_sku_not_position():
    pdf = [mkline("A", 1, 100), mkline("B", 2, 200), mkline("C", 3, 300)]
    erp = [mkline("C", 3, 300), mkline("A", 1, 100), mkline("B", 2, 200)]
    pairs = match_lines(pdf, erp)
    assert all(not p.diffs for p in pairs) and len(pairs) == 3


def test_repeated_sku_with_different_discounts_pairs_exactly():
    # invoice 0153: TNR-85A 28 @ 0% and 8 @ 10%, in a different order in the ERP
    pdf = [mkline("TNR-85A", 28, 245000, 0), mkline("TNR-85A", 8, 245000, 10)]
    erp = [mkline("TNR-85A", 8, 245000, 10), mkline("TNR-85A", 28, 245000, 0)]
    pairs = match_lines(pdf, erp)
    assert [p.diffs for p in pairs] == [[], []]


def test_quantity_difference():
    pairs = match_lines([mkline("AMP-110", 120, 23750)], [mkline("AMP-110", 100, 23750)])
    assert pairs[0].diffs == ["qty"] and pairs[0].amount_diff == 475_000


def test_price_difference():
    pairs = match_lines([mkline("PK-A4", 200, 52500)], [mkline("PK-A4", 200, 49500)])
    assert pairs[0].diffs == ["unit_price"] and pairs[0].amount_diff == 600_000


def test_discount_difference():
    pairs = match_lines([mkline("X", 10, 1000, 5)], [mkline("X", 10, 1000, Fraction(5, 2))])
    assert pairs[0].diffs == ["disc_pct"]


def test_extra_and_missing_lines():
    pairs = match_lines([mkline("A", 1, 100), mkline("DLV-JKT", 1, 250000)],
                        [mkline("A", 1, 100), mkline("Z", 1, 5)])
    kinds = sorted(d for p in pairs for d in p.diffs)
    assert kinds == ["extra_in_pdf", "missing_from_pdf"]


def test_sku_formatting_is_normalised():
    pairs = match_lines([mkline("pk-a4-80-rm", 1, 100)], [mkline("PK A4 80 RM", 1, 100)])
    assert pairs[0].erp is not None and not pairs[0].diffs


def test_uom_case_is_ignored():
    p = mkline("A", 1, 100, uom="rim")
    e = mkline("A", 1, 100, uom="RIM")
    assert match_lines([p], [e])[0].diffs == []


def test_documents_match_across_number_formats():
    p = mkdoc("PDF", "PKI/INV/2026/09/0142", [mkline("A", 1, 100)])
    e = mkdoc("ERP", "PKI-INV-2026-09-0142", [mkline("A", 1, 100)], entry_id="E-1")
    groups = match_documents([p], [e])
    assert len(groups) == 1 and groups[0].pdf == [p] and groups[0].erp == [e]


def test_duplicates_are_grouped_not_dropped():
    p = mkdoc("PDF", "INV/1", [mkline("A", 1, 100)])
    e1 = mkdoc("ERP", "INV/1", [mkline("A", 1, 100)], entry_id="E-1")
    e2 = mkdoc("ERP", "INV/1", [mkline("A", 1, 100)], entry_id="E-2")
    g = match_documents([p], [e1, e2])
    assert len(g) == 1 and len(g[0].erp) == 2


def test_secondary_key_pairs_typo_in_doc_number():
    p = mkdoc("PDF", "INV/2026/0150", [mkline("A", 3, 100)], po="PO-77")
    e = mkdoc("ERP", "INV/2026/0105", [mkline("A", 3, 100)], po="PO-77", entry_id="E-9")
    g = match_documents([p], [e])
    assert len(g) == 1 and g[0].secondary and g[0].erp == [e]


def test_secondary_key_requires_unique_candidate():
    p = mkdoc("PDF", "INV/1", [mkline("A", 3, 100)], po="PO-77")
    e1 = mkdoc("ERP", "INV/8", [mkline("A", 3, 100)], po="PO-77", entry_id="E-1")
    e2 = mkdoc("ERP", "INV/9", [mkline("A", 3, 100)], po="PO-77", entry_id="E-2")
    g = match_documents([p], [e1, e2])
    assert len(g) == 3 and not any(x.secondary for x in g)


def test_erp_scoped_to_batch_vendor():
    a = mkdoc("ERP", "1", [], vendor="PT PRIMA KERTAS INDONESIA")
    b = mkdoc("ERP", "2", [], vendor="PT LAIN SEKALI")
    keep, out = scope_erp([a, b], ["Prima Kertas Indonesia"])
    assert keep == [a] and out == [b]
