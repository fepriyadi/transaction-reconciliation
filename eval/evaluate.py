"""Evaluation harness. Writes eval/RESULTS.md.

    recon eval            # all three suites (OCR stress takes ~1 min); same as python eval/evaluate.py
    recon eval --quick    # skip the OCR stress suite

1. September ground truth  — hand-verified status + cause per document.
2. ERP mutation suite      — inject one known error into a copy of the ERP export,
                             check the target document gets the expected status and
                             nothing else changes; the batch must still reconcile.
3. OCR stress suite        — re-render every text page as a degraded scan (rotation,
                             blur, noise, low dpi), extract with OCR only, and compare
                             with the text-layer read. Counts exact / flagged / SILENTLY
                             WRONG (the number that matters).
"""
from __future__ import annotations

import argparse
import io
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))      # for tests.test_september_batch (the ground truth); recon itself is installed

import openpyxl  # noqa: E402
import pymupdf  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402

from recon.pipeline import run_batch  # noqa: E402
from recon.rules import expected_totals, line_amount  # noqa: E402
from tests.test_september_batch import EXPECTED  # noqa: E402

PDF = ROOT / "data/Supplier_Invoices_PT_Prima_Kertas_Sep2026.pdf"
ERP = ROOT / "data/ERP_AP_Export_Sep2026.xlsx"
CFG = ROOT / "config"

EXPECTED_CAUSE = {
    "PKI/INV/2026/09/0142": "DOC_NO_FORMAT_DIFF", "PKI/INV/2026/09/0143": "ERP_TAX_ROUNDING",
    "PKI/INV/2026/09/0144": "LINE_QTY_DIFF", "PKI/INV/2026/09/0145": "LINE_PRICE_DIFF",
    "PKI/INV/2026/09/0146": "NOT_IN_ERP", "PKI/INV/2026/09/0147": "ERP_DUPLICATE_ENTRY",
    "PKI/INV/2026/09/0148": "PDF_DPP_ERROR", "PKI/INV/2026/09/0149": "PDF_SUBTOTAL_ERROR",
    "PKI/INV/2026/09/0150": "EXTRACTED_BY_OCR", "PKI/INV/2026/09/0151": "LINE_EXTRA_IN_PDF",
    "PKI/INV/2026/09/0152": "ERP_VALUE_STORED_AS_TEXT", "PKI/INV/2026/09/0153": "MULTI_PAGE_DOC",
    "PKI/INV/2026/09/0154": "PO_MISMATCH", "PKI/CN/2026/09/0012": "CREDIT_NOTE",
    "PKI/INV/2026/09/0155": "NOT_IN_PDF",
}


# --------------------------------------------------------------------------- 1. ground truth

def suite_ground_truth(out: list):
    t0 = time.time()
    br = run_batch([PDF], ERP, CFG, llm=None)
    dt = time.time() - t0
    rows, ok_s, ok_c = [], 0, 0
    for r in br.results:
        exp_s, exp_d = EXPECTED.get(r.doc_no, ("?", None))
        cause = EXPECTED_CAUSE.get(r.doc_no)
        s_ok = r.status == exp_s and r.difference == exp_d
        c_ok = cause is None or cause in r.reasons
        ok_s += s_ok
        ok_c += c_ok
        rows.append(f"| {r.doc_no} | {exp_s} | {r.status} | {r.difference:,} | {cause or '—'} | "
                    f"{'✅' if s_ok and c_ok else '❌'} |")
    t = br.totals
    out += ["## 1. September 2026 ground truth", "",
            f"Status + difference correct: **{ok_s}/{len(br.results)}** · expected cause among reason codes: "
            f"**{ok_c}/{len(br.results)}** · unexplained batch difference: **Rp {t.unexplained:,}** · "
            f"runtime {dt:.1f} s for 16 pages (deterministic path, no LLM).", "",
            "| Document | Expected | Got | Difference | Expected cause | OK |", "|---|---|---|---:|---|---|", *rows, ""]


# --------------------------------------------------------------------------- 2. ERP mutations

def _hdr(ws):
    return {c.value: i for i, c in enumerate(ws[1])}


def _find_row(ws, col, value):
    h = _hdr(ws)
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, h[col] + 1).value == value:
            return r
    raise KeyError(value)


def _recompute(wb, entry):
    """Recompute ERP header from its (mutated) lines so the ERP stays self-consistent."""
    wl, wi = wb["AP_Lines"], wb["AP_Invoices"]
    hl, hi = _hdr(wl), _hdr(wi)
    sub = 0
    for r in range(2, wl.max_row + 1):
        if wl.cell(r, hl["Entry ID"] + 1).value == entry:
            q, p, d = (wl.cell(r, hl[k] + 1).value for k in ("Qty", "Unit Price", "Disc %"))
            amt = line_amount(q, p, d)
            wl.cell(r, hl["Line Amount"] + 1).value = amt
            sub += amt
    t = expected_totals(sub)
    r = _find_row(wi, "Entry ID", entry)
    for col, k in (("Subtotal", "subtotal"), ("DPP", "dpp"), ("PPN", "ppn"), ("Grand Total", "total")):
        wi.cell(r, hi[col] + 1).value = t[k]


def m_qty(wb):
    wl = wb["AP_Lines"]; h = _hdr(wl)
    r = _find_row(wl, "Entry ID", "E-10451")
    wl.cell(r, h["Qty"] + 1).value = 90
    _recompute(wb, "E-10451")


def m_price(wb):
    wl = wb["AP_Lines"]; h = _hdr(wl)
    r = _find_row(wl, "Entry ID", "E-10452")
    wl.cell(r, h["Unit Price"] + 1).value = 26500
    _recompute(wb, "E-10452")


def m_disc(wb):
    wl = wb["AP_Lines"]; h = _hdr(wl)
    r = _find_row(wl, "Entry ID", "E-10463") + 1          # STP-NO10 line, 5% -> 2.5%
    wl.cell(r, h["Disc %"] + 1).value = 2.5
    _recompute(wb, "E-10463")


def m_delete(wb):
    for name in ("AP_Invoices", "AP_Lines"):
        ws = wb[name]; h = _hdr(ws)
        for r in range(ws.max_row, 1, -1):
            if ws.cell(r, h["Entry ID"] + 1).value == "E-10464":
                ws.delete_rows(r)


def m_duplicate(wb):
    wi, wl = wb["AP_Invoices"], wb["AP_Lines"]
    src = _find_row(wi, "Entry ID", "E-10465")
    wi.append([c.value for c in wi[src]]); wi.cell(wi.max_row, 1).value = "E-10499"
    h = _hdr(wl)
    for r in range(2, wl.max_row + 1):
        if wl.cell(r, h["Entry ID"] + 1).value == "E-10465":
            vals = [c.value for c in wl[r]]; vals[0] = "E-10499"; wl.append(vals)


def m_po(wb):
    wi = wb["AP_Invoices"]; h = _hdr(wi)
    wi.cell(_find_row(wi, "Entry ID", "E-10451"), h["PO No"] + 1).value = "PO-88201"


def m_docno_format(wb):
    wi = wb["AP_Invoices"]; h = _hdr(wi)
    wi.cell(_find_row(wi, "Entry ID", "E-10451"), h["Vendor Inv. No"] + 1).value = "pki inv 2026 09 141"


def m_total_text_id(wb):
    wi = wb["AP_Invoices"]; h = _hdr(wi)
    wi.cell(_find_row(wi, "Entry ID", "E-10451"), h["Grand Total"] + 1).value = "3.323.676"


def m_ppn_rounding(wb):
    wi = wb["AP_Invoices"]; h = _hdr(wi)
    r = _find_row(wi, "Entry ID", "E-10451")
    wi.cell(r, h["PPN"] + 1).value += 4
    wi.cell(r, h["Grand Total"] + 1).value += 4


def m_ppn_wrong(wb):
    wi = wb["AP_Invoices"]; h = _hdr(wi)
    r = _find_row(wi, "Entry ID", "E-10451")
    wi.cell(r, h["PPN"] + 1).value += 500
    wi.cell(r, h["Grand Total"] + 1).value += 500


def m_ambiguous_date(wb):
    wi = wb["AP_Invoices"]; h = _hdr(wi)
    wi.cell(_find_row(wi, "Entry ID", "E-10451"), h["Doc Date"] + 1).value = "01/09/2026"   # D/M or M/D?


def m_other_vendor(wb):
    wi = wb["AP_Invoices"]
    wi.append(["E-20001", "PT LAIN INDONESIA", "XYZ/INV/2026/09/0001", "2026-09-10", "PO-1", "IDR", 1000, 917, 110,
               1110, "Posted", "fin.ap01"])


def m_renamed_columns(wb):
    wi = wb["AP_Invoices"]
    for c in wi[1]:
        if c.value == "Grand Total":
            c.value = "Total"
        if c.value == "Vendor Inv. No":
            c.value = "Vendor Invoice No"
    wb["AP_Invoices"].title = "Invoices_Sept"


def m_cn_reference(wb):
    wc = wb["AP_CreditNotes"]; h = _hdr(wc)
    wc.cell(2, h["Original Invoice"] + 1).value = "PKI/INV/2026/08/0113"


MUTATIONS = [
    ("Quantity changed on a line (ERP self-consistent)", m_qty, "PKI/INV/2026/09/0141", "AMOUNT_MISMATCH", "LINE_QTY_DIFF"),
    ("Unit price changed", m_price, "PKI/INV/2026/09/0142", "AMOUNT_MISMATCH", "LINE_PRICE_DIFF"),
    ("Discount changed 5% → 2.5% (scanned invoice)", m_disc, "PKI/INV/2026/09/0150", "AMOUNT_MISMATCH", "LINE_DISC_DIFF"),
    ("ERP entry deleted", m_delete, "PKI/INV/2026/09/0151", "MISSING_IN_ERP", "NOT_IN_ERP"),
    ("Entry booked twice", m_duplicate, "PKI/INV/2026/09/0152", "DUPLICATE_IN_ERP", "ERP_DUPLICATE_ENTRY"),
    ("PO reference changed", m_po, "PKI/INV/2026/09/0141", "NEEDS_REVIEW", "PO_MISMATCH"),
    ("Invoice no. typed as 'pki inv 2026 09 141'", m_docno_format, "PKI/INV/2026/09/0141", "MATCHED", "DOC_NO_FORMAT_DIFF"),
    ("Grand total stored as text '3.323.676'", m_total_text_id, "PKI/INV/2026/09/0141", "MATCHED", "ERP_VALUE_STORED_AS_TEXT"),
    ("PPN +4 (rounding)", m_ppn_rounding, "PKI/INV/2026/09/0141", "MATCHED_WITHIN_TOLERANCE", "TOTAL_ROUNDING_DIFF"),
    ("PPN +500 (real error)", m_ppn_wrong, "PKI/INV/2026/09/0141", "NEEDS_REVIEW", "ERP_PPN_ERROR"),
    ("Date as ambiguous text '01/09/2026'", m_ambiguous_date, "PKI/INV/2026/09/0141", "MATCHED", None),
    ("Entry for another vendor added", m_other_vendor, "PKI/INV/2026/09/0141", "MATCHED", None),
    ("Columns + sheet renamed (Total, Vendor Invoice No)", m_renamed_columns, "PKI/INV/2026/09/0141", "MATCHED", None),
    ("Credit note linked to wrong invoice", m_cn_reference, "PKI/CN/2026/09/0012", "NEEDS_REVIEW", "CN_REFERENCE_MISMATCH"),
]


def suite_mutations(out: list):
    base = {r.doc_no: r.status for r in run_batch([PDF], ERP, CFG, llm=None).results}
    rows, passed = [], 0
    tmp = Path(tempfile.mkdtemp())
    for name, fn, target, exp_status, exp_code in MUTATIONS:
        wb = openpyxl.load_workbook(ERP)
        fn(wb)
        p = tmp / "erp.xlsx"
        wb.save(p)
        try:
            br = run_batch([PDF], p, CFG, llm=None)
            got = {r.doc_no: r for r in br.results}
            r = got.get(target)
            others_same = all(base.get(k) == v.status for k, v in got.items() if k != target and k in base)
            ok = (r is not None and r.status == exp_status and (exp_code is None or exp_code in r.reasons)
                  and others_same and br.totals.unexplained == 0)
            detail = f"{r.status} · {', '.join(r.reasons) or '—'}" if r else "target missing"
            if not others_same:
                detail += " · OTHER DOCUMENTS CHANGED"
        except Exception as e:  # report, don't crash the suite
            ok, detail = False, f"error: {type(e).__name__}: {e}"
        passed += ok
        rows.append(f"| {name} | {target} | {exp_status} | {detail} | {'✅' if ok else '❌'} |")
    shutil.rmtree(tmp, ignore_errors=True)
    out += ["## 2. ERP mutation suite (simulated new batches)", "",
            f"**{passed}/{len(MUTATIONS)}** mutations detected with the expected status, no collateral changes, "
            f"batch still fully explained.", "",
            "| Mutation | Target | Expected | Got (status · reasons) | OK |", "|---|---|---|---|---|", *rows, ""]


# --------------------------------------------------------------------------- 3. OCR stress

def _degrade(page, dpi, angle, blur, noise, rnd) -> bytes:
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if noise:
        px = img.load()
        w, h = img.size
        for _ in range(int(w * h * noise)):
            x, y = rnd.randrange(w), rnd.randrange(h)
            px[x, y] = rnd.choice((0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


PROFILES = [("mild: 200 dpi, 0.7°", 200, 0.7, 0, 0.0),
            ("medium: 150 dpi, 1.5°, blur 0.6, 0.5% noise", 150, 1.5, 0.6, 0.005),
            ("harsh: 110 dpi, -2.5°, blur 1.0, 1% noise", 110, -2.5, 1.0, 0.01)]


def suite_ocr(out: list, llm=None):
    from recon.config import load_profiles
    from recon.extract import extract_pdf
    profiles = load_profiles(CFG)
    truth_docs, _ = extract_pdf(PDF, profiles)
    truth = {d.key: d for d in truth_docs if d.method == "text_layer"}
    src = pymupdf.open(PDF)
    rnd = random.Random(7)
    rows, fail_rows = [], []
    tmp = Path(tempfile.mkdtemp())
    for label, dpi, angle, blur, noise in PROFILES:
        doc = pymupdf.open()
        for page in src:
            png = _degrade(page, dpi, angle, blur, noise, rnd)
            np_ = doc.new_page(width=page.rect.width, height=page.rect.height)
            np_.insert_image(np_.rect, stream=png)
        p = tmp / f"scan_{dpi}.pdf"
        doc.save(p)
        t0 = time.time()
        docs, logs = extract_pdf(p, profiles, llm=llm)
        dt = time.time() - t0
        got = {d.key: d for d in docs}
        exact = flagged = silent = missing = 0
        for k, td in truth.items():
            d = got.get(k)
            if d is None:
                missing += 1
                fail_rows.append(f"| {label} | {td.doc_no} | not found as a document (page logged for review) |")
                continue
            same = (d.total == td.total and d.subtotal == td.subtotal and d.ppn == td.ppn and d.dpp == td.dpp and
                    [(l.sku_key, l.qty, l.unit_price, l.disc_pct, l.amount) for l in d.lines] ==
                    [(l.sku_key, l.qty, l.unit_price, l.disc_pct, l.amount) for l in td.lines] and d.po_no == td.po_no)
            from recon.rules import check_document, hard_failures
            truth_fail = bool(hard_failures(check_document(td)))
            caught = (d.confidence < 0.9 or (bool(hard_failures(check_document(d))) and not truth_fail)
                      or bool(d.extraction_notes and any("line numbers" in n or "missing" in n for n in d.extraction_notes)))
            if same:
                exact += 1
            elif caught:
                flagged += 1
                fail_rows.append(f"| {label} | {td.doc_no} | misread, **caught** (confidence {d.confidence}) |")
            else:
                silent += 1
                fail_rows.append(f"| {label} | {td.doc_no} | misread, **not caught** |")
        n = len(truth)
        rows.append(f"| {label} | {n} | {exact} | {flagged} | {missing} | **{silent}** | {dt:.1f} s |")
    shutil.rmtree(tmp, ignore_errors=True)
    mode = f"OCR + Groq vision second read ({llm.vision_model})" if llm else "OCR only, no LLM"
    out += [f"## 3. Scan stress test (every text page re-rendered as a degraded scan; {mode})", "",
            "Ground truth = the text-layer read of the same page. *Caught* means the document is held for review "
            "(low confidence, an arithmetic check that the true document passes, or a structural check). "
            "*Silently wrong* is the failure that matters: a misread that could be approved.", "",
            "| Degradation | Docs | Exact | Misread but caught | Not found (page flagged) | Silently wrong | OCR time |",
            "|---|---:|---:|---:|---:|---:|---:|", *rows, ""]
    if llm:
        u = llm.usage
        out += [f"LLM usage: {u.calls} calls, {u.prompt_tokens:,} prompt + {u.completion_tokens:,} completion tokens, "
                f"~${u.cost_usd:.4f}.", ""]
    if fail_rows:
        out += ["Failure cases:", "", "| Degradation | Document | Outcome |", "|---|---|---|", *fail_rows, ""]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the scan stress suite")
    ap.add_argument("--llm", action="store_true", help="scan suite with Groq vision second read (needs GROQ_API_KEY)")
    a = ap.parse_args()
    llm = None
    if a.llm:
        from dotenv import load_dotenv
        from recon.config import load_llm_config
        from recon.llm import GroqLLM
        load_dotenv()
        llm = GroqLLM(load_llm_config(CFG))
        if not llm.available:
            sys.exit("--llm needs GROQ_API_KEY")
    out = ["# Evaluation results", "", f"Generated by `python eval/evaluate.py` on {time.strftime('%Y-%m-%d %H:%M')}.", ""]
    suite_ground_truth(out)
    suite_mutations(out)
    if not a.quick:
        suite_ocr(out, llm)
    (ROOT / "eval" / "RESULTS.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
