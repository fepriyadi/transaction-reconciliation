"""Old command name, kept as an alias: `python run_batch.py ARGS` is `recon run ARGS`.

    python run_batch.py --pdf data/Supplier_Invoices_*.pdf --erp data/ERP_AP_Export_*.xlsx --out output/sep2026
"""
import sys

from recon.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["run", *sys.argv[1:]]))
