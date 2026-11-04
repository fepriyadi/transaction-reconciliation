"""Report tables (used by both the Excel/CSV writer and the Streamlit UI)."""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .explain import describe_line_pair
from .normalize import fmt_idr, fmt_num
from .reason_codes import LINE_CODE, REASON_CODES, STATUSES, highlighted_fields
from .reconcile import BRIDGE_COMPONENTS

STATUS_FILL = {
    "MATCHED": "C6EFCE", "MATCHED_WITHIN_TOLERANCE": "E2F0D9", "AMOUNT_MISMATCH": "FFEB9C",
    "MISSING_IN_ERP": "F8CBAD", "MISSING_IN_PDF": "F8CBAD", "DUPLICATE_IN_ERP": "FFC7CE",
    "DUPLICATE_IN_PDF": "FFC7CE", "NEEDS_REVIEW": "DDEBF7",
}


def _n(v):
    if isinstance(v, Fraction):
        return int(v) if v.denominator == 1 else float(v)
    return v


def _src(f) -> str:
    return f.src.label() if f is not None and f.src is not None else ""


def documents_table(br, decisions: dict | None = None) -> pd.DataFrame:
    """decisions: doc key -> review.DecisionState (from DecisionLog.state)."""
    decisions = decisions or {}
    rows = []
    for r in br.results:
        p = r.pdf_docs[0] if r.pdf_docs else None
        e = r.erp_docs[0] if r.erp_docs else None
        ds = decisions.get(r.key)
        dec = ds.decision if ds else None
        rows.append({
            "Document no.": r.doc_no,
            "Type": "Credit note" if r.doc_type == "CREDIT_NOTE" else ("Invoice" if r.doc_type == "INVOICE" else r.doc_type),
            "Doc date": either.doc_date if (either := p or e) and either.doc_date else None,
            "PO (PDF)": p.po_no if p else "",
            "PO (ERP)": e.po_no if e else "",
            "PDF source": r.pdf_ref,
            "ERP entry": r.erp_ref,
            "PDF total": r.pdf_total,
            "ERP total": r.erp_total,
            "Difference (PDF-ERP)": r.difference,
            "Status": r.status,
            "Reason codes": ", ".join(r.reasons),
            "Headline": r.headline,
            "Explanation": r.explanation,
            "Suggested action": r.action,
            "Confidence": r.confidence,
            "Review required": "YES" if r.review_required else "no",
            "Reviewer decision": dec.decision if dec else "",
            "Reviewer": dec.reviewer if dec else "",
            "Reviewer note": dec.note if dec else "",
            "Decided at": dec.at if dec else "",
            "Decision state": "" if not ds else ("current" if ds.current else f"STALE: {ds.stale}"),
            "_key": r.key,
        })
    return pd.DataFrame(rows)


def line_discrepancy_table(br) -> pd.DataFrame:
    rows = []
    for r in br.results:
        for lp in r.line_discrepancies:
            p, e = lp.pdf, lp.erp
            rows.append({
                "Document no.": r.doc_no, "Status": r.status,
                "PDF line": p.line_no if p else None, "ERP line": e.line_no if e else None,
                "SKU": lp.sku, "Description": (p or e).description,
                "Reason code": ", ".join(LINE_CODE[d] for d in lp.diffs),
                "PDF qty": _n(p.qty) if p else None, "ERP qty": _n(e.qty) if e else None,
                "PDF unit price": _n(p.unit_price) if p else None, "ERP unit price": _n(e.unit_price) if e else None,
                "PDF disc %": _n(p.disc_pct) if p else None, "ERP disc %": _n(e.disc_pct) if e else None,
                "PDF amount": p.amount if p else None, "ERP amount": e.amount if e else None,
                "Amount difference": lp.amount_diff,
                "Explanation": describe_line_pair(lp),
                "PDF source": _src(p.fields.get("amount")) if p else "",
                "ERP source": _src(e.fields.get("amount")) if e else "",
            })
    return pd.DataFrame(rows)


def bridge_table(br) -> pd.DataFrame:
    rows = []
    for r in br.results:
        row = {"Document no.": r.doc_no, "Status": r.status, "PDF total": r.pdf_total, "ERP total": r.erp_total,
               "Difference": r.difference}
        for k, label in BRIDGE_COMPONENTS:
            row[label] = getattr(r.bridge, k)
        rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        tot: dict[str, object] = {"Document no.": "BATCH TOTAL", "Status": ""}
        for c in df.columns[2:]:
            tot[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).sum()
        df = pd.concat([df, pd.DataFrame([tot])], ignore_index=True)
    return df


def control_totals_table(br) -> pd.DataFrame:
    t = br.totals
    rows = [
        ("Supplier documents in PDF", t.pdf_documents,
         f"{t.pdf_invoices} invoices, {t.pdf_credit_notes} credit notes, {t.pdf_lines} lines, {len(br.page_logs)} pages"),
        ("ERP entries (in scope)", t.erp_entries,
         f"{t.erp_lines} lines; {t.erp_out_of_scope} entries for other vendors excluded"),
        ("PDF batch total", t.pdf_total, "Σ document totals; credit notes negative"),
        ("ERP batch total", t.erp_total, "Σ Grand Total of in-scope entries; credit notes negative"),
        ("Difference (PDF − ERP)", t.difference, ""),
        ("Σ document-level differences", t.sum_of_document_differences, "must equal the line above"),
    ]
    for k, label in BRIDGE_COMPONENTS:
        if k == "unexplained":
            continue
        rows.append(("  explained by: " + label, getattr(t.bridge, k), ""))
    rows.append(("Total explained", t.explained, ""))
    rows.append(("UNEXPLAINED", t.unexplained, "0 = the batch difference is fully explained"
                 + (f"; amounts unknown for {t.unknown_amount_docs}" if t.unknown_amount_docs else "")))
    return pd.DataFrame(rows, columns=["Control", "Amount (IDR)", "Note"])


def checks_table(br) -> pd.DataFrame:
    rows = []
    for r in br.results:
        for chk in r.checks:
            d = chk.doc
            for c in chk.results:
                rows.append({"Document no.": r.doc_no, "Side": d.side,
                             "Source": d.entry_id if d.side == "ERP" else f"p.{','.join(map(str, d.pages))}",
                             "Rule": c.rule, "Target": c.target, "Rule result": c.expected, "Stated": c.stated,
                             "Delta": c.delta,
                             "Result": "PASS" if c.passed else ("ROUNDING" if c.rounding_only else "FAIL")})
    return pd.DataFrame(rows)


AUDIT_HEADER_FIELDS = ["doc_no", "doc_date", "po_no", "reference", "subtotal", "dpp", "ppn", "total", "entry_id", "doc_status"]
AUDIT_LINE_FIELDS = ["sku", "qty", "uom", "unit_price", "disc_pct", "amount"]


def audit_table(br, doc_key: str | None = None) -> pd.DataFrame:
    rows = []
    for r in br.results:
        if doc_key and r.key != doc_key:
            continue
        for d in r.pdf_docs + r.erp_docs:
            for k in AUDIT_HEADER_FIELDS:
                f = d.fields.get(k)
                if f is None:
                    continue
                rows.append({"Document no.": r.doc_no, "Side": d.side, "Field": k, "Line": None, "SKU": "",
                             "Value": str(_n(f.value)) if f.value is not None else "", "Raw text": f.raw,
                             "Source": _src(f), "Page": f.src.page if f.src else None,
                             "BBox": f.src.bbox if f.src else None})
            for ln in d.lines:
                for k in AUDIT_LINE_FIELDS:
                    f = ln.fields.get(k)
                    if f is None:
                        continue
                    rows.append({"Document no.": r.doc_no, "Side": d.side, "Field": k, "Line": ln.line_no,
                                 "SKU": ln.sku, "Value": str(_n(f.value)), "Raw text": f.raw, "Source": _src(f),
                                 "Page": f.src.page if f.src else None, "BBox": f.src.bbox if f.src else None})
    return pd.DataFrame(rows)


def extraction_table(br) -> pd.DataFrame:
    rows = []
    for l in br.page_logs:
        rows.append({"File": l.file, "Page": l.page, "Method": l.method, "Profile": l.profile, "Role": l.role,
                     "Document no.": l.doc_no, "Confidence": l.confidence, "Notes": "; ".join(l.notes)})
    return pd.DataFrame(rows)


def erp_load_table(br) -> pd.DataFrame:
    rows = [{"Item": f"Sheet '{s}'", "Detail": f"classified as {role}, {n} data rows"} for s, role, n in br.erp.sheets]
    rows += [{"Item": k, "Detail": v} for k, v in br.erp.metadata.items()]
    rows += [{"Item": "Normalised", "Detail": n} for n in br.erp.notes]
    rows += [{"Item": "Out of scope (other vendor)", "Detail": f"{d.entry_id} {d.vendor} {d.doc_no} {fmt_idr(d.total)}"}
             for d in br.erp_out_of_scope]
    return pd.DataFrame(rows)


def reason_code_table() -> pd.DataFrame:
    """The status policy itself: what each code means and what it does to a document."""
    return pd.DataFrame([{"Reason code": c.code, "Severity": c.severity, "Forces status": c.status or "",
                          "Blocks auto-approval": "yes" if c.severity == "review" else "no", "Meaning": c.meaning}
                         for c in REASON_CODES.values()])


def figure_boxes(r) -> dict:
    """page -> [(x0, y0, x1, y1, color)] for one result's PDF figures; figures behind a discrepancy in red."""
    red_header = highlighted_fields(r.reasons)
    red: set = {(id(d), k) for d in r.pdf_docs for k in red_header}
    for lp in r.line_discrepancies:
        if lp.pdf:
            red |= {(id(lp.pdf), d) for d in lp.diffs if d in ("qty", "unit_price", "disc_pct")}
            red.add((id(lp.pdf), "amount"))
            if "extra_in_pdf" in lp.diffs:
                red |= {(id(lp.pdf), k) for k in ("sku", "description", "qty", "unit_price")}
    return document_boxes(r.pdf_docs, red)


def document_boxes(docs, red: frozenset | set = frozenset()) -> dict:
    """page -> [(x0, y0, x1, y1, color)] for every figure with a box; `red` holds (id(document or line), field)."""
    out: dict = {}
    for d in docs:
        for k, f in d.fields.items():
            if f.src and f.src.bbox and f.src.page:
                out.setdefault(f.src.page, []).append((*f.src.bbox, "red" if (id(d), k) in red else "blue"))
        for ln in d.lines:
            for k, f in ln.fields.items():
                if f.src and f.src.bbox and f.src.page and k in ("sku", "qty", "unit_price", "disc_pct", "amount"):
                    out.setdefault(f.src.page, []).append((*f.src.bbox, "red" if (id(ln), k) in red else "blue"))
    return out


AUDIT_FIELDS = (("doc_no", "Document no."), ("doc_date", "Date"), ("po_no", "PO"), ("reference", "Reference"),
                ("subtotal", "Subtotal"), ("dpp", "DPP"), ("ppn", "PPN"), ("total", "Total"))
_MONEY_FIELDS = ("subtotal", "dpp", "ppn", "total")


def figure_comparison_table(r) -> pd.DataFrame:
    """Header figures of one result, PDF next to ERP, each with where it was read."""
    p = r.pdf_docs[0] if r.pdf_docs else None
    e = r.erp_docs[0] if r.erp_docs else None

    def src(d, k):
        f = d.fields.get(k) if d else None
        return f.src.label().split(" › ")[-1] if f and f.src else ""

    rows = []
    for k, lab in AUDIT_FIELDS:
        pv = getattr(p, k, None) if p else None
        ev = getattr(e, k, None) if e else None
        fmt = (lambda v: f"{v:,}") if k in _MONEY_FIELDS else (lambda v: str(v or ""))
        rows.append({"Field": lab, "PDF": fmt(pv) if pv is not None else "", "PDF source": src(p, k),
                     "ERP": fmt(ev) if ev is not None else "", "ERP source": src(e, k),
                     "Match": "" if pv is None or ev is None
                     else ("✓" if pv == ev or (k == "doc_no" and p is not None and e is not None and p.key == e.key)
                           else "✗")})
    return pd.DataFrame(rows)


def line_pairs_table(r) -> pd.DataFrame:
    """Every line of one result, paired by item code."""
    rows = []
    for lp in r.line_pairs:
        a, b = lp.pdf, lp.erp
        rows.append({"PDF #": a.line_no if a else None, "ERP #": b.line_no if b else None, "SKU": lp.sku,
                     "Qty PDF/ERP": f"{fmt_num(a.qty) if a else '—'} / {fmt_num(b.qty) if b else '—'}",
                     "Price PDF/ERP": f"{fmt_num(a.unit_price) if a else '—'} / {fmt_num(b.unit_price) if b else '—'}",
                     "Disc PDF/ERP": f"{fmt_num(a.disc_pct) if a else '—'} / {fmt_num(b.disc_pct) if b else '—'}",
                     "Amount PDF": a.amount if a else None, "Amount ERP": b.amount if b else None,
                     "Diff": lp.amount_diff, "Issue": ", ".join(lp.diffs) or "✓"})
    return pd.DataFrame(rows)


def summary_sheet_rows(br) -> list:
    t = br.totals
    rows = [["Transaction reconciliation report"], [],
            ["Supplier", br.supplier_label], ["Period", br.period_label],
            ["PDF file(s)", ", ".join(br.pdf_files)], ["ERP export", br.erp_file], [],
            ["PDF batch total", t.pdf_total], ["ERP batch total", t.erp_total],
            ["Difference (PDF − ERP)", t.difference], ["Unexplained", t.unexplained], [],
            ["Status", "Documents", "Σ Difference"]]
    for s in STATUSES:
        rows.append([s, t.status_counts[s], t.status_amounts[s]])
    rows += [[], ["Auto-approvable", t.auto_approvable], ["Review required", t.review_required], [],
             ["Runtime (s)", br.timings.get("total_s")], ["Pages", br.timings.get("pages")]]
    if br.llm_usage:
        rows += [["LLM calls", br.llm_usage.get("calls")], ["LLM tokens (in/out)",
                 f"{br.llm_usage.get('prompt_tokens')}/{br.llm_usage.get('completion_tokens')}"],
                 ["LLM cost (USD, est.)", br.llm_usage.get("cost_usd")]]
    rows += [[], ["Summary for AP manager / CFO", f"({br.summary_source})"]]
    for line in br.summary.split("\n"):
        rows.append([line])
    return rows


def write_reports(br, out_dir: Path, decisions: dict | None = None) -> dict:
    """decisions: doc key -> review.DecisionState."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    docs = documents_table(br, decisions).drop(columns=["_key"])
    lines = line_discrepancy_table(br)
    bridge = bridge_table(br)
    audit = audit_table(br).drop(columns=["Page", "BBox"], errors="ignore")
    paths = {
        "excel": out_dir / "reconciliation_report.xlsx",
        "documents_csv": out_dir / "document_report.csv",
        "lines_csv": out_dir / "line_discrepancies.csv",
        "audit_csv": out_dir / "audit_trail.csv",
        "summary_txt": out_dir / "customer_summary.txt",
    }
    docs.to_csv(paths["documents_csv"], index=False)
    lines.to_csv(paths["lines_csv"], index=False)
    audit.to_csv(paths["audit_csv"], index=False)
    paths["summary_txt"].write_text(br.summary + "\n")

    with pd.ExcelWriter(paths["excel"], engine="openpyxl") as xw:
        srows = summary_sheet_rows(br)
        width = max(len(r) for r in srows)
        pd.DataFrame([r + [None] * (width - len(r)) for r in srows]).to_excel(xw, sheet_name="Summary", index=False, header=False)
        docs.to_excel(xw, sheet_name="Documents", index=False)
        lines.to_excel(xw, sheet_name="Line_Discrepancies", index=False)
        control_totals_table(br).to_excel(xw, sheet_name="Control_Totals", index=False)
        bridge.to_excel(xw, sheet_name="Bridge_by_Document", index=False)
        checks_table(br).to_excel(xw, sheet_name="Arithmetic_Checks", index=False)
        audit.to_excel(xw, sheet_name="Audit_Trail", index=False)
        extraction_table(br).to_excel(xw, sheet_name="Extraction_Log", index=False)
        erp_load_table(br).to_excel(xw, sheet_name="ERP_Load", index=False)
        reason_code_table().to_excel(xw, sheet_name="Reason_Codes", index=False)
    _style(paths["excel"])
    return paths


def _style(path: Path):
    wb = load_workbook(path)
    money_cols = {"PDF total", "ERP total", "Difference (PDF-ERP)", "Difference", "Amount (IDR)", "PDF amount",
                  "ERP amount", "Amount difference", "Rule result", "Stated", "Delta", "PDF unit price", "ERP unit price"}
    money_cols |= {label for _, label in BRIDGE_COMPONENTS}
    wrap_cols = {"Explanation", "Suggested action", "Reason codes", "Notes", "Detail", "Meaning", "Headline", "Source",
                 "Decision state"}
    for ws in wb.worksheets:
        if ws.title == "Summary":
            ws.column_dimensions["A"].width = 34
            ws.column_dimensions["B"].width = 22
            ws.column_dimensions["C"].width = 18
            ws["A1"].font = Font(bold=True, size=14)
            for row in ws.iter_rows():
                for c in row:
                    if isinstance(c.value, int) and abs(c.value) >= 1000:
                        c.number_format = "#,##0;-#,##0"
            continue
        hdr = [c.value for c in ws[1]]
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="305496")
            c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "B2"
        for i, h in enumerate(hdr, 1):
            col = get_column_letter(i)
            width = (60 if h in ("Explanation", "Suggested action") else 40 if h in wrap_cols
                     else max(12, min(28, len(str(h)) + 4)))
            ws.column_dimensions[col].width = width
            for c in ws[col][1:]:
                if h in money_cols:
                    c.number_format = "#,##0;-#,##0"
                if h in wrap_cols:
                    c.alignment = Alignment(wrap_text=True, vertical="top")
                if h == "Status" and c.value in STATUS_FILL:
                    c.fill = PatternFill("solid", fgColor=STATUS_FILL[c.value])
        if ws.title in ("Control_Totals", "Bridge_by_Document"):
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
        ws.auto_filter.ref = ws.dimensions
    wb.save(path)
