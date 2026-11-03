"""End-to-end batch run: extract -> load ERP -> match -> reconcile -> totals -> summary.

`run_batch` only sequences the steps below and times them; each step is a plain
function of its inputs.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

from .config import DEFAULT_CONFIG, Config, SupplierProfile
from .erp_loader import ErpLoad, load_erp
from .extract import PageLog, extract_pdf
from .llm import LLMClient, PageReader
from .matching import match_documents, scope_erp
from .models import Document
from .reconcile import ControlTotals, DocResult, control_totals, extraction_failure_results, reconcile_all
from .rules import RuleConfig
from .summary import deterministic_summary, summarise, summary_facts

__all__ = ["BatchResult", "DEFAULT_CONFIG", "run_batch", "summary_facts", "deterministic_summary"]

Progress = Callable[[str, float], object]      # (message, fraction done); return value ignored

log = logging.getLogger(__name__)


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


def run_batch(pdf_paths: list, erp_path, config: Union[Config, Path] = DEFAULT_CONFIG, llm: Optional[LLMClient] = None,
              llm_extract: bool = True, llm_summary: bool = True, progress: Optional[Progress] = None) -> BatchResult:
    """config: a loaded Config or a config directory (validated on load, before any page is read).
    llm_extract: use the LLM for scanned pages / unknown layouts; llm_summary: word the CFO summary."""
    t0 = time.time()
    conf = config if isinstance(config, Config) else Config.load(Path(config))
    profiles = list(conf.profiles)

    pdf_docs, logs = extract_all(pdf_paths, profiles, llm if llm_extract else None, progress)
    t_extract = time.time()

    if progress:
        progress("Loading ERP export", 0.75)
    erp = load_erp(Path(erp_path), conf.erp)
    vendors = batch_vendors(pdf_docs, profiles)
    in_scope, out_scope = scope_erp(erp.documents, sorted(vendors))

    if progress:
        progress("Matching and reconciling", 0.85)
    results = reconcile_batch(pdf_docs, in_scope, logs, conf)
    totals = control_totals(results, pdf_docs, in_scope, out_scope)
    t_recon = time.time()

    br = BatchResult(pdf_files=[Path(p).name for p in pdf_paths], erp_file=Path(erp_path).name,
                     pdf_docs=pdf_docs, erp=erp, erp_in_scope=in_scope, erp_out_of_scope=out_scope,
                     page_logs=logs, results=results, totals=totals, rule_config=conf.rule_config,
                     period_label=period_label(pdf_docs), supplier_label=supplier_label(vendors, profiles))
    br.summary, br.summary_source = summarise(br, llm if llm_summary else None, progress)
    t_end = time.time()

    br.timings = {"extract_s": round(t_extract - t0, 2), "reconcile_s": round(t_recon - t_extract, 2),
                  "summary_s": round(t_end - t_recon, 2), "total_s": round(t_end - t0, 2),
                  "pages": len(logs)}
    br.llm_usage = usage_report(llm)
    log.info("batch %s: %d pages, %d documents, unexplained %s, %.2f s",
             br.supplier_label, len(logs), len(results), totals.unexplained, br.timings["total_s"])
    return br


# --------------------------------------------------------------------------- steps

def extract_all(pdf_paths: list, profiles: list[SupplierProfile], llm: Optional[PageReader],
                progress: Optional[Progress] = None) -> tuple[list[Document], list[PageLog]]:
    """Every supplier PDF of the batch; page progress fills the first 70% of the bar."""
    docs: list[Document] = []
    logs: list[PageLog] = []
    for path in pdf_paths:
        name = Path(path).name

        def page_progress(i: int, n: int, name: str = name) -> None:
            if progress:
                progress(f"Reading {name} page {i}/{n}", i / n * 0.7)
        d, pl = extract_pdf(Path(path), profiles, llm=llm, progress=page_progress if progress else None)
        docs += d
        logs += pl
    return docs, logs


def batch_vendors(pdf_docs: list[Document], profiles: list[SupplierProfile]) -> set[str]:
    """Vendor names of the batch, plus the names each supplier goes by in the ERP."""
    vendors = {d.vendor for d in pdf_docs if d.vendor}
    for p in profiles:
        if p.vendor_name in vendors:
            vendors |= set(p.erp_vendor_names)
    return vendors


def reconcile_batch(pdf_docs: list[Document], erp_in_scope: list[Document], logs: list[PageLog],
                    conf: Config) -> list[DocResult]:
    groups = match_documents(pdf_docs, erp_in_scope)
    return (reconcile_all(groups, conf.rule_config, conf.rules.auto_approve_min_confidence, conf.register)
            + extraction_failure_results(logs))


def supplier_label(vendors: set[str], profiles: list[SupplierProfile]) -> str:
    return ", ".join(sorted({p.name for p in profiles if p.vendor_name in vendors})) or ", ".join(sorted(vendors))


def period_label(pdf_docs: list[Document]) -> str:
    dates = sorted(d.doc_date for d in pdf_docs if d.doc_date)
    return dates[-1].strftime("%B %Y") if dates else ""


def usage_report(llm: Optional[LLMClient]) -> dict:
    if llm is None:
        return {}
    u = llm.usage
    return {"calls": u.calls, "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
            "llm_seconds": round(u.seconds, 2), "cost_usd": round(u.cost_usd, 5), "log": u.log}
