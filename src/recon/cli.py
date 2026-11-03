"""The single entry point.

    recon run --pdf data/Supplier_Invoices_*.pdf --erp data/ERP_AP_Export_*.xlsx --out output/sep2026
    recon ui                     # Streamlit review UI (extra arguments go to streamlit)
    recon check [config_dir]     # validate config/ (profiles, rules, ERP mapping, LLM models)
    recon eval [--quick] [--llm] # evaluation suites; writes eval/RESULTS.md

`recon run` works on any new batch without code changes; add a supplier layout in
config/suppliers/ (or set GROQ_API_KEY to let the LLM read unknown layouts).

Exit codes of `recon run`: 0 difference fully explained, 2 unexplained difference, 3 invalid configuration.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import config as config_mod
from .config import DEFAULT_CONFIG, PROJECT_ROOT, Config, ConfigError
from .llm import GroqLLM
from .normalize import fmt_idr, fmt_signed
from .pipeline import run_batch
from .report import write_reports

PASSTHROUGH = ("ui", "eval")       # their extra arguments go to streamlit / the eval script


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="recon", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{run,ui,check,eval}")

    run = sub.add_parser("run", help="reconcile a batch and write the reports")
    run.add_argument("--pdf", nargs="+", required=True, help="supplier PDF file(s)")
    run.add_argument("--erp", required=True, help="ERP AP export (.xlsx)")
    run.add_argument("--out", default="output", help="output directory")
    run.add_argument("--config", default=str(DEFAULT_CONFIG))
    run.add_argument("--no-llm", action="store_true", help="deterministic only (OCR for scans, template summary)")
    run.add_argument("-v", "--verbose", action="count", default=0, help="-v: steps and warnings, -vv: every LLM call")

    sub.add_parser("ui", help="start the Streamlit review UI")
    check = sub.add_parser("check", help="validate a config directory")
    check.add_argument("config_dir", nargs="?", default=str(DEFAULT_CONFIG))
    sub.add_parser("eval", help="run the evaluation suites (--quick, --llm)")

    args, extra = ap.parse_known_args(argv)
    if extra and args.cmd not in PASSTHROUGH:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")

    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "check":
        return config_mod.main([args.config_dir])
    if args.cmd == "ui":
        return _script(["-m", "streamlit", "run", str(PROJECT_ROOT / "app.py")], extra)
    return _script([str(PROJECT_ROOT / "eval" / "evaluate.py")], extra)


def _script(cmd: list[str], extra: list[str]) -> int:
    """Run a project script (it lives next to the package, not in it) with this interpreter."""
    target = Path(cmd[-1])
    if not target.exists():
        print(f"{target} not found: `recon ui` and `recon eval` need the source checkout (pip install -e .)",
              file=sys.stderr)
        return 1
    return subprocess.call([sys.executable, *cmd, *extra], cwd=PROJECT_ROOT)


def cmd_run(args: argparse.Namespace) -> int:
    load_dotenv()
    logging.basicConfig(level=(logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)],
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
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
        print(f"{r.doc_no:26} {r.status:26} {fmt_idr(r.pdf_total):>16} {fmt_idr(r.erp_total):>16} "
              f"{fmt_signed(r.difference):>14}")
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
