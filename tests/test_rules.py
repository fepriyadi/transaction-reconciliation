from fractions import Fraction

import pytest

from recon.rules import (RuleConfig, check_document, dpp, expected_totals, hard_failures, line_amount, ppn,
                         round_half_up, rounding_deviations)
from tests.conftest import mkdoc, mkline


@pytest.mark.parametrize("x,expected", [
    (Fraction(1, 2), 1), (Fraction(3, 2), 2), (Fraction(5, 2), 3),       # half-up, not banker's
    (Fraction(149, 100), 1), (Fraction(-1, 2), -1), (Fraction(-5, 2), -3),  # symmetric for credit notes
    (Fraction(2744777.75), 2744778),
])
def test_round_half_up(x, expected):
    assert round_half_up(x) == expected


@pytest.mark.parametrize("qty,price,disc,expected", [
    (99, 13900, 10, 1238490),
    (65, 4750, 5, 293313),        # 293,312.5 -> half-up
    (25, 3250, 5, 77188),         # 77,187.5
    (27, 18500, Fraction(5, 2), 487013),
    (35, 23750, Fraction(5, 2), 810469),
    (-15, 44000, 0, -660000),     # credit note line
])
def test_r1_line_amount(qty, price, disc, expected):
    assert line_amount(qty, price, disc) == expected


def test_r3_r4_from_invoice_0141():
    t = expected_totals(2994303)
    assert t == {"subtotal": 2994303, "dpp": 2744778, "ppn": 329373, "total": 3323676}


def test_r4_effective_rate_is_11_percent_before_rounding():
    assert dpp(1_200_000) == 1_100_000
    assert ppn(1_100_000) == 132_000


def test_tax_rates_come_from_config():
    cfg = RuleConfig(dpp_factor=Fraction(1), ppn_rate=Fraction(11, 100))
    assert expected_totals(1_000_000, cfg)["ppn"] == 110_000


def test_clean_document_passes_all_checks():
    d = mkdoc("PDF", "X/1", [mkline("A", 99, 13900, 10), mkline("B", 65, 4750, 5)])
    assert all(c.passed for c in check_document(d))


def test_r2_subtotal_error_detected_once():
    # invoice 0149: subtotal overstated by 100,000, everything after computed from it
    lines = [mkline("A", 180, 47500), mkline("B", 230, 49500, Fraction(5, 2))]
    bad_sub = sum(l.amount for l in lines) + 100_000
    t = expected_totals(bad_sub)
    d = mkdoc("PDF", "X/2", lines, override=t)
    fails = hard_failures(check_document(d))
    assert [(c.rule, c.target) for c in fails] == [("R2", "subtotal")]
    assert fails[0].delta == 100_000


def test_r3_full_base_dpp_detected():
    # invoice 0148: DPP printed equal to subtotal (no 11/12)
    lines = [mkline("A", 190, 49500)]
    sub = lines[0].amount
    d = mkdoc("PDF", "X/3", lines, override={"dpp": sub, "ppn": round_half_up(Fraction(sub) * Fraction(12, 100)),
                                             "total": sub + round_half_up(Fraction(sub) * Fraction(12, 100))})
    fails = hard_failures(check_document(d))
    assert [c.rule for c in fails] == ["R3"]


def test_rounding_only_tax_deviation_is_not_a_hard_failure():
    # ERP E-10453: PPN 1,749,259 where DPP x 12% = 1,749,258.48 -> 1,749,258
    lines = [mkline("A", 1, 15902350)]
    d = mkdoc("ERP", "X/4", lines, override={"ppn": 1749259, "total": 15902350 + 1749259})
    chk = check_document(d)
    assert hard_failures(chk) == []
    dev = rounding_deviations(chk)
    assert len(dev) == 1 and dev[0].target == "PPN" and dev[0].delta == 1


def test_r1_line_error_detected():
    d = mkdoc("PDF", "X/5", [mkline("A", 10, 1000, 0, amount=10_500)])
    fails = hard_failures(check_document(d))
    assert fails[0].rule == "R1" and fails[0].expected == 10_000
