"""The AP-manager / CFO summary: computed facts, a deterministic wording, and the
optional LLM wording that is accepted only if every number in it is a fact."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional

from .llm import SummaryWriter, unsupported_numbers
from .normalize import fmt_idr, fmt_signed
from .reason_codes import APPROVABLE_STATUSES

if TYPE_CHECKING:
    from .pipeline import BatchResult

log = logging.getLogger(__name__)


def summary_facts(br: BatchResult) -> dict:
    t = br.totals
    issues = []
    for r in sorted(br.results, key=lambda r: -abs(r.difference or 0)):
        if r.status in APPROVABLE_STATUSES:
            continue
        issues.append({"document": r.doc_no, "status": r.status, "issue": r.headline,
                       "difference": fmt_signed(r.difference), "action": r.action})
    return {
        "supplier": br.supplier_label,
        "period": br.period_label,
        "supplier_documents": t.pdf_documents,
        "erp_entries": t.erp_entries,
        "supplier_total": fmt_idr(t.pdf_total),
        "erp_total": fmt_idr(t.erp_total),
        "difference": fmt_signed(t.difference),
        "unexplained": fmt_idr(t.unexplained),
        "status_counts": {k: v for k, v in t.status_counts.items() if v},
        "agree_with_erp": sum(t.status_counts[s] for s in APPROVABLE_STATUSES),
        "approvable_without_review": t.auto_approvable,
        "double_payment_prevented": fmt_idr(t.exposure.duplicate_erp),
        "overbilling_held": fmt_idr(t.exposure.overbilled_on_matched_docs),
        "issues": issues,
    }


def deterministic_summary(br: BatchResult) -> str:
    t = br.totals
    ok = sum(t.status_counts[s] for s in APPROVABLE_STATUSES)
    explained = "fully explained" if t.fully_explained else f"NOT fully explained ({fmt_idr(t.unexplained)} open)"
    lines = [
        f"{br.supplier_label} — {br.period_label}: {t.pdf_documents} supplier documents "
        f"({fmt_idr(t.pdf_total)}) against {t.erp_entries} ERP entries ({fmt_idr(t.erp_total)}). "
        f"The difference of {fmt_signed(t.difference)} is {explained}.",
        f"{ok} of {t.documents} documents agree with the ERP; {t.auto_approvable} can be approved as is "
        f"and {t.review_required} need a decision.",
    ]
    issues = [r for r in br.results if r.status not in APPROVABLE_STATUSES]
    issues.sort(key=lambda r: -abs(r.difference or 0))
    for r in issues:
        lines.append(f"• {r.doc_no}: {r.headline} — {r.action.split('. ')[0].rstrip('.')}.")
    ex = t.exposure
    if ex.duplicate_erp or ex.overbilled_on_matched_docs:
        lines.append(f"Payment protected: {fmt_idr(ex.duplicate_erp)} double booking and "
                     f"{fmt_idr(ex.overbilled_on_matched_docs)} over-billing are on hold pending the actions above.")
    return "\n".join(lines)


def summarise(br: BatchResult, llm: Optional[SummaryWriter] = None,
              progress: Optional[Callable[[str, float], object]] = None) -> tuple[str, str]:
    """(summary, source). The LLM wording is used only when it states no number
    that is not in the facts; otherwise, or without an LLM, the deterministic one."""
    text = deterministic_summary(br)
    if llm is None or not llm.available:
        return text, "deterministic"
    if progress:
        progress("Writing summary", 0.95)
    try:
        facts = summary_facts(br)
        worded, _ = llm.write_summary(facts)
    except Exception as e:
        log.warning("LLM summary failed (%s: %s); using the deterministic summary", type(e).__name__, e)
        return text, f"deterministic (LLM unavailable: {type(e).__name__})"
    bad = unsupported_numbers(worded, facts)
    if worded.strip() and not bad:
        return worded.strip(), f"LLM ({llm.text_model}), numbers verified against facts"
    log.warning("LLM summary rejected: numbers not in the facts %s", sorted(bad)[:5])
    return text, f"deterministic (LLM summary rejected: unsupported numbers {sorted(bad)[:5]})"
