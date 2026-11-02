"""Command-line batch run.

    python run_batch.py --pdf data/Supplier_Invoices_*.pdf --erp data/ERP_AP_Export_*.xlsx --out output/sep2026

Runs on any new batch without code changes; add a supplier layout in
config/suppliers/ (or set GROQ_API_KEY to let the LLM read unknown layouts).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from recon.config import Config, ConfigError
from recon.llm import GroqLLM
from recon.normalize import fmt_idr, fmt_signed
from recon.pipeline import DEFAULT_CONFIG, run_batch
from recon.report import write_reports


def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", nargs="+", required=True, help="supplier PDF file(s)")
    ap.add_argument("--erp", required=True, help="ERP AP export (.xlsx)")
    ap.add_argument("--out", default="output", help="output directory")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--no-llm", action="store_true", help="deterministic only (OCR for scans, template summary)")
    args = ap.parse_args(argv)

    try:
        conf = Config.load(Path(args.config))
    except ConfigError as e:
        print(f"Configuration is invalid: {e}", file=sys.stderr)
        return 3
    llm = None if args.no_llm else GroqLLM(conf.llm)
    if llm is not None and not llm.available:
        print("GROQ_API_KEY not set: running without LLM (OCR only for scanned pages).", file=sys.stderr)
        llm = None
    br = run_batch(args.pdf, args.erp, conf, llm=llm,
                   progress=lambda msg, frac: print(f"  [{frac:4.0%}] {msg}", file=sys.stderr))
    paths = write_reports(br, Path(args.out))

    t = br.totals
    print(f"\n{br.supplier_label} — {br.period_label}")
    print(f"{'Document':26} {'Status':26} {'PDF total':>16} {'ERP total':>16} {'Difference':>14}")
    for r in br.results:
        print(f"{r.doc_no:26} {r.status:26} {fmt_idr(r.pdf_total):>16} {fmt_idr(r.erp_total):>16} {fmt_signed(r.difference):>14}")
    print(f"\nPDF {fmt_idr(t.pdf_total)}  ERP {fmt_idr(t.erp_total)}  difference {fmt_signed(t.difference)}  "
          f"unexplained {fmt_idr(t.unexplained)}")
    print(f"Runtime {br.timings['total_s']} s for {br.timings['pages']} pages"
          + (f"; LLM {br.llm_usage['calls']} calls, ~${br.llm_usage['cost_usd']}" if br.llm_usage else ""))
    print("\nReports:")
    for k, p in paths.items():
        print(f"  {k:14} {p}")
    return 0 if t.unexplained == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
