"""End-to-end batch run: extract -> load ERP -> match -> reconcile -> totals -> summary."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .config import Config
from .erp_loader import ErpLoad, load_erp
from .extract import extract_pdf
from .llm import GroqLLM, unsupported_numbers
from .matching import match_documents, scope_erp
from .normalize import fmt_idr, fmt_signed
from .reason_codes import APPROVABLE_STATUSES
from .reconcile import ControlTotals, DocResult, control_totals, extraction_failure_results, reconcile_all
from .rules import RuleConfig

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config"


@dataclass
class BatchResult:
    pdf_files: list
    erp_file: str
    pdf_docs: list
    erp: ErpLoad
    erp_in_scope: list
    erp_out_of_scope: list
    page_logs: list
    results: list[DocResult]
    totals: ControlTotals
    summary: str = ""
    summary_source: str = "deterministic"
    timings: dict = field(default_factory=dict)
    llm_usage: dict = field(default_factory=dict)
    rule_config: Optional[RuleConfig] = None
    period_label: str = ""
    supplier_label: str = ""


def run_batch(pdf_paths: list, erp_path, config: Union[Config, Path] = DEFAULT_CONFIG, llm: Optional[GroqLLM] = None,
              llm_extract: bool = True, llm_summary: bool = True, progress=None) -> BatchResult:
    """config: a loaded Config or a config directory (validated on load, before any page is read).
    llm_extract: use the LLM for scanned pages / unknown layouts; llm_summary: word the CFO summary."""
    t0 = time.time()
    conf = config if isinstance(config, Config) else Config.load(Path(config))
    cfg = conf.rule_config
    min_conf = conf.rules.auto_approve_min_confidence
    profiles = list(conf.profiles)

    pdf_docs, logs = [], []
    for path in pdf_paths:
        d, l = extract_pdf(Path(path), profiles, llm=llm if llm_extract else None,
                           progress=(lambda i, n, p=path: progress(f"Reading {Path(p).name} page {i}/{n}", i / n * 0.7))
                           if progress else None)
        pdf_docs += d
        logs += l
    t_extract = time.time()

    if progress:
        progress("Loading ERP export", 0.75)
    erp = load_erp(Path(erp_path), conf.erp)
    vendors = {d.vendor for d in pdf_docs if d.vendor}
    for p in profiles:
        if p.vendor_name in vendors:
            vendors |= set(p.erp_vendor_names)
    in_scope, out_scope = scope_erp(erp.documents, sorted(vendors))

    if progress:
        progress("Matching and reconciling", 0.85)
    groups = match_documents(pdf_docs, in_scope)
    results = reconcile_all(groups, cfg, min_conf, conf.register) + extraction_failure_results(logs)
    totals = control_totals(results, pdf_docs, in_scope, out_scope)
    t_recon = time.time()

    br = BatchResult(pdf_files=[Path(p).name for p in pdf_paths], erp_file=Path(erp_path).name,
                     pdf_docs=pdf_docs, erp=erp, erp_in_scope=in_scope, erp_out_of_scope=out_scope,
                     page_logs=logs, results=results, totals=totals, rule_config=cfg)
    br.supplier_label = ", ".join(sorted({p.name for p in profiles if p.vendor_name in vendors})) or \
        ", ".join(sorted(vendors))
    dates = sorted(d.doc_date for d in pdf_docs if d.doc_date)
    br.period_label = dates[-1].strftime("%B %Y") if dates else ""

    br.summary = deterministic_summary(br)
    if llm is not None and llm.available and llm_summary:
        if progress:
            progress("Writing summary", 0.95)
        try:
            facts = summary_facts(br)
            text, _ = llm.write_summary(facts)
            bad = unsupported_numbers(text, facts)
            if text.strip() and not bad:
                br.summary, br.summary_source = text.strip(), f"LLM ({llm.text_model}), numbers verified against facts"
            else:
                br.summary_source = f"deterministic (LLM summary rejected: unsupported numbers {sorted(bad)[:5]})"
        except Exception as e:
            br.summary_source = f"deterministic (LLM unavailable: {type(e).__name__})"
    t_end = time.time()

    br.timings = {"extract_s": round(t_extract - t0, 2), "reconcile_s": round(t_recon - t_extract, 2),
                  "summary_s": round(t_end - t_recon, 2), "total_s": round(t_end - t0, 2),
                  "pages": len(logs)}
    if llm is not None:
        u = llm.usage
        br.llm_usage = {"calls": u.calls, "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                        "llm_seconds": round(u.seconds, 2), "cost_usd": round(u.cost_usd, 5), "log": u.log}
    return br


# --------------------------------------------------------------------------- summary

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
