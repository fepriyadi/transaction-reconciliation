from datetime import date, datetime
from fractions import Fraction

import pytest

from recon.normalize import is_blank, norm_doc_no, norm_vendor, parse_date, parse_int_amount, parse_number, parse_percent


@pytest.mark.parametrize("a,b", [
    ("PKI/INV/2026/09/0142", "PKI-INV-2026-09-0142"),
    ("PKI/INV/2026/09/0142", " pki inv 2026 09 142 "),
    ("PKI/CN/2026/09/0012", "PKI_CN_2026_09_12"),
])
def test_doc_no_formats_are_equal(a, b):
    assert norm_doc_no(a) == norm_doc_no(b)


def test_doc_no_different_numbers_stay_different():
    assert norm_doc_no("PKI/INV/2026/09/0147") != norm_doc_no("PKI/INV/2026/09/0148")
    assert norm_doc_no("PKI/INV/2026/09/0141") != norm_doc_no("PKI/CN/2026/09/0141")


@pytest.mark.parametrize("raw,expected", [
    ("1.238.490", 1238490),       # Indonesian thousands
    ("15,619,088", 15619088),     # US thousands (ERP text cell)
    ("101.656.444", 101656444),
    ("77.188", 77188),
    ("14,858,000", 14858000),     # OCR read the dots as commas
    ("14.", 14),                  # OCR trailing punctuation
    ("_77.188", 77188),
    (3323676, 3323676),
    (13900.0, 13900),
    ("-972360", -972360),
    ("(972.360)", -972360),
])
def test_parse_amount(raw, expected):
    assert parse_int_amount(raw) == expected


def test_decimal_comma_discount():
    assert parse_number("2,5") == Fraction(5, 2)
    assert parse_number(2.5) == Fraction(5, 2)
    assert parse_number("12,75") == Fraction(51, 4)


@pytest.mark.parametrize("raw,expected", [
    ("5%", 5), ("2.5%", Fraction(5, 2)), ("2,5 %", Fraction(5, 2)), ("0%", 0), ("10", 10), (5, 5), ("%", None), ("abc", None),
])
def test_parse_percent(raw, expected):
    assert parse_percent(raw) == expected


def test_percent_sign_is_not_a_number_elsewhere():
    assert parse_number("5%") is None and parse_int_amount("12%") is None   # amounts stay strict


def test_blank_discount_cells():
    assert all(is_blank(v) for v in (None, "", "  ", "-", "—"))
    assert not any(is_blank(v) for v in ("0", 0, "5%", "abc"))


def test_decimal_hint_resolves_three_digit_ambiguity():
    assert parse_number("1.250", decimal_hint=",") == 1250
    assert parse_number("1.250", decimal_hint=".") == Fraction(5, 4)


def test_non_numbers_rejected():
    assert parse_number("roll") is None
    assert parse_number("1.2.3") is None
    assert parse_number("") is None
    assert parse_int_amount("2,5") is None   # not a whole rupiah amount


def test_dates():
    assert parse_date("01-09-2026") == (date(2026, 9, 1), True)       # DMY default, ambiguous
    assert parse_date("23-09-2026") == (date(2026, 9, 23), False)
    assert parse_date("09/23/2026", "MDY") == (date(2026, 9, 23), False)
    assert parse_date("09/23/2026", "DMY") == (date(2026, 9, 23), False)  # 23 can't be a month
    assert parse_date("2026-09-13") == (date(2026, 9, 13), False)
    assert parse_date(datetime(2026, 9, 1)) == (date(2026, 9, 1), False)
    assert parse_date("09/10/2026", "MDY") == (date(2026, 9, 10), True)


def test_vendor_normalisation_ignores_legal_form():
    assert norm_vendor("PT Prima Kertas Indonesia") == norm_vendor("PRIMA KERTAS INDONESIA, PT")
