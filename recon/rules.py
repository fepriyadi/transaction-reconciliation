"""Business rules R1–R5, implemented with exact rational arithmetic.

No floats anywhere: Fraction keeps 11/12 exact, and rounding is explicit
half-up (half away from zero, so credit notes round symmetrically).
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .models import CheckResult, Document


@dataclass(frozen=True)
class RuleConfig:
    dpp_factor: Fraction = Fraction(11, 12)     # R3
    ppn_rate: Fraction = Fraction(12, 100)      # R4
    total_tolerance: int = 10                   # R5
    tax_rounding_tolerance: int = 10            # R7: tax deviations this small are rounding, not errors


def round_half_up(x: Fraction) -> int:
    """Round to whole rupiah, .5 away from zero."""
    x = Fraction(x)
    sign = -1 if x < 0 else 1
    a = abs(x)
    whole = a.numerator // a.denominator
    if a - whole >= Fraction(1, 2):
        whole += 1
    return sign * whole


def line_amount(qty, unit_price, disc_pct) -> int:
    """R1: qty x unit price x (1 - disc/100), rounded half-up."""
    return round_half_up(Fraction(qty) * Fraction(unit_price) * (1 - Fraction(disc_pct) / 100))


def subtotal(amounts) -> int:
    """R2."""
    return sum(int(a) for a in amounts)


def dpp(sub: int, cfg: RuleConfig = RuleConfig()) -> int:
    """R3: DPP Nilai Lain = subtotal x 11/12."""
    return round_half_up(Fraction(sub) * cfg.dpp_factor)


def ppn(dpp_value: int, cfg: RuleConfig = RuleConfig()) -> int:
    """R4: PPN = DPP x 12%."""
    return round_half_up(Fraction(dpp_value) * cfg.ppn_rate)


def rule_ppn(sub: int, cfg: RuleConfig = RuleConfig()) -> int:
    """PPN the rules give for a subtotal (R3 then R4)."""
    return ppn(dpp(sub, cfg), cfg)


def expected_totals(sub: int, cfg: RuleConfig = RuleConfig()) -> dict:
    d = dpp(sub, cfg)
    p = ppn(d, cfg)
    return {"subtotal": sub, "dpp": d, "ppn": p, "total": sub + p}


def check_document(doc: Document, cfg: RuleConfig = RuleConfig()) -> list[CheckResult]:
    """R7: check that a document's own numbers add up. Never alters the document.

    Each step is checked against the *stated* previous figure, so one error is
    reported once instead of cascading through every later figure.
    """
    out: list[CheckResult] = []
    for i, ln in enumerate(doc.lines, 1):
        exp = line_amount(ln.qty, ln.unit_price, ln.disc_pct)
        out.append(CheckResult("R1", f"line {ln.line_no or i} {ln.sku}", exp, ln.amount, exp == ln.amount))

    if doc.subtotal is not None and doc.lines:
        exp = subtotal(ln.amount for ln in doc.lines)
        out.append(CheckResult("R2", "subtotal", exp, doc.subtotal, exp == doc.subtotal))

    if doc.subtotal is not None and doc.dpp is not None:
        exp = dpp(doc.subtotal, cfg)
        delta = abs(doc.dpp - exp)
        out.append(CheckResult("R3", "DPP", exp, doc.dpp, delta == 0,
                               rounding_only=0 < delta <= cfg.tax_rounding_tolerance))

    if doc.dpp is not None and doc.ppn is not None:
        exp = ppn(doc.dpp, cfg)
        delta = abs(doc.ppn - exp)
        out.append(CheckResult("R4", "PPN", exp, doc.ppn, delta == 0,
                               rounding_only=0 < delta <= cfg.tax_rounding_tolerance))

    if doc.subtotal is not None and doc.ppn is not None and doc.total is not None:
        exp = doc.subtotal + doc.ppn
        out.append(CheckResult("R4", "total", exp, doc.total, exp == doc.total))
    return out


def hard_failures(checks: list[CheckResult]) -> list[CheckResult]:
    """Checks that failed by more than rounding — these force NEEDS_REVIEW."""
    return [c for c in checks if not c.passed and not c.rounding_only]


def rounding_deviations(checks: list[CheckResult]) -> list[CheckResult]:
    return [c for c in checks if not c.passed and c.rounding_only]
