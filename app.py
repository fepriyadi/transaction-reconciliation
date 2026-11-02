"""Streamlit UI for the AP team: run a batch, work the review queue, audit any figure.

    streamlit run app.py
"""
from __future__ import annotations

import hashlib
import io
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import pymupdf
import streamlit as st
from dotenv import load_dotenv
from PIL import Image, ImageDraw

from recon.config import Config, ConfigError
from recon.llm import GroqLLM
from recon.normalize import fmt_idr, fmt_num, fmt_signed
from recon.pipeline import DEFAULT_CONFIG, run_batch
from recon.reason_codes import STATUSES, status_rank
from recon.report import (STATUS_FILL, audit_table, checks_table, control_totals_table, documents_table,
                          extraction_table, erp_load_table, figure_boxes, line_discrepancy_table, reason_code_table,
                          write_reports)
from recon.review import DECISIONS, DecisionLog, batch_id

load_dotenv()
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "output"

st.set_page_config(page_title="AP Reconciliation", page_icon="🧾", layout="wide")


# --------------------------------------------------------------------------- helpers

def save_upload(f, folder: Path) -> Path:
    """Content-addressed, so two different files with the same name never overwrite each other."""
    data = f.getbuffer()
    p = folder / hashlib.sha256(data).hexdigest()[:12] / f.name
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return p


def style_status(df: pd.DataFrame):
    def color(v):
        return f"background-color: #{STATUS_FILL[v]}; color: #111" if v in STATUS_FILL else ""
    money = [c for c in df.columns if c in ("PDF total", "ERP total", "Difference (PDF-ERP)")]
    sty = df.style.map(color, subset=["Status"]) if "Status" in df.columns else df.style
    return sty.format({c: lambda v: "" if pd.isna(v) else f"{int(v):,}" for c in money})


@st.cache_data(show_spinner=False)
def render_page(pdf_path: str, page_no: int, boxes: tuple, dpi: int = 110) -> bytes:
    """Render a PDF page with highlighted bounding boxes ((x0,y0,x1,y1,color),...)."""
    doc = pymupdf.open(pdf_path)
    pix = doc[page_no - 1].get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    k = dpi / 72
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(over)
    for x0, y0, x1, y1, color in boxes:
        rgb = {"red": (220, 38, 38), "blue": (37, 99, 235), "green": (22, 163, 74)}[color]
        dr.rectangle([x0 * k - 2, y0 * k - 2, x1 * k + 2, y1 * k + 2], outline=rgb + (255,), width=2, fill=rgb + (40,))
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- sidebar

with st.sidebar:
    st.header("Batch")
    src = st.radio("Input", ["Sample: September 2026", "Upload new batch"], label_visibility="collapsed")
    pdf_paths, erp_path = [], None
    if src.startswith("Sample"):
        pdf_paths = sorted(DATA.glob("Supplier_Invoices_*.pdf"))
        xl = sorted(DATA.glob("ERP_*.xlsx"))
        erp_path = xl[0] if xl else None
        st.caption("\n".join(f"• {p.name}" for p in pdf_paths + ([erp_path] if erp_path else [])))
    else:
        ups = st.file_uploader("Supplier invoice PDF(s)", type="pdf", accept_multiple_files=True)
        upx = st.file_uploader("ERP AP export (.xlsx)", type=["xlsx"])
        if ups and upx:
            tmp = OUT / "uploads" / datetime.now().strftime("%Y%m%d")
            pdf_paths = [save_upload(f, tmp) for f in ups]
            erp_path = save_upload(upx, tmp)

    try:
        conf = Config.load(DEFAULT_CONFIG)
    except ConfigError as e:
        st.error(f"Configuration is invalid, fix it before running: {e}")
        st.stop()

    st.header("LLM (Groq)")
    key = st.text_input("GROQ_API_KEY", value=os.getenv("GROQ_API_KEY", ""), type="password",
                        help="Optional. Without a key the run is fully deterministic (OCR for scanned pages).")
    use_llm_extract = st.toggle("Vision second-read of scanned pages / unknown layouts", value=bool(key))
    use_llm_summary = st.toggle("Word the CFO summary with the LLM", value=bool(key))
    with st.expander("Models"):
        text_model = st.text_input("Text model", conf.llm.text_model)
        vision_model = st.text_input("Vision model", conf.llm.vision_model)
    run = st.button("Run reconciliation", type="primary", disabled=not (pdf_paths and erp_path), width="stretch")
    st.caption("Arithmetic, matching, statuses and explanations are deterministic code. "
               "The LLM only reads pages and words the summary; its output is re-checked by code.")

if run:
    llm = None
    if key and (use_llm_extract or use_llm_summary):
        llm = GroqLLM(conf.llm, api_key=key, text_model=text_model, vision_model=vision_model)
    bar = st.progress(0.0, "Starting")
    try:
        br = run_batch(pdf_paths, erp_path, conf, llm=llm, llm_extract=use_llm_extract,
                       llm_summary=use_llm_summary, progress=lambda m, f: bar.progress(min(f, 1.0), m))
    except Exception as e:
        bar.empty()
        st.error(f"Run failed: {type(e).__name__}: {e}")
        st.stop()
    bar.empty()
    st.session_state.br = br
    st.session_state.bid = batch_id(list(pdf_paths) + [erp_path])
    st.session_state.pdf_map = {Path(p).name: str(p) for p in pdf_paths}
    st.session_state.run_no = st.session_state.get("run_no", 0) + 1

br = st.session_state.get("br")
st.title("Supplier invoice ↔ ERP reconciliation")
if br is None:
    st.info("Choose a batch in the sidebar and press **Run reconciliation**.")
    st.stop()

bid = st.session_state.bid
log = DecisionLog(OUT / bid)
decisions = log.state(br.results)          # doc key -> latest decision, flagged stale if the facts changed
t = br.totals


def open_for_review(r) -> bool:
    ds = decisions.get(r.key)
    return r.review_required and not (ds and ds.current)


# --------------------------------------------------------------------------- tabs

tab_over, tab_docs, tab_queue, tab_audit, tab_extract, tab_dl = st.tabs(
    ["Overview", "Documents", f"Review queue ({sum(1 for r in br.results if open_for_review(r))})",
     "Audit a document", "Extraction log", "Download"])

with tab_over:
    st.subheader(f"{br.supplier_label} · {br.period_label}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Supplier PDF total", fmt_idr(t.pdf_total), f"{t.pdf_documents} documents", delta_color="off")
    c2.metric("ERP total", fmt_idr(t.erp_total), f"{t.erp_entries} entries", delta_color="off")
    c3.metric("Difference (PDF − ERP)", fmt_signed(t.difference))
    c4.metric("Unexplained", fmt_idr(t.unexplained),
              "fully explained" if t.unexplained == 0 else "OPEN", delta_color="normal" if t.unexplained == 0 else "inverse")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Summary for the AP manager / CFO")
        st.write(br.summary.replace("\n", "  \n"))
        st.caption(f"Source: {br.summary_source}")
    with right:
        st.markdown("#### Status")
        sdf = pd.DataFrame([{"Status": s, "Documents": t.status_counts[s], "Σ Difference": t.status_amounts[s]}
                            for s in STATUSES if t.status_counts[s]])
        st.dataframe(sdf.style.map(lambda v: f"background-color: #{STATUS_FILL.get(v, 'FFFFFF')}; color:#111",
                                   subset=["Status"]).format({"Σ Difference": "{:,}"}), hide_index=True, width="stretch")
        st.markdown(f"**{t.auto_approvable}** can be approved as is · **{t.review_required}** need a decision")

    st.markdown("#### Batch control totals — where the difference comes from")
    ct = control_totals_table(br)
    st.dataframe(ct.style.format({"Amount (IDR)": "{:,}"}), hide_index=True, width="stretch")
    with st.expander("Run statistics"):
        st.write({"runtime_seconds": br.timings, "llm": {k: v for k, v in br.llm_usage.items() if k != "log"} or "not used"})
        if br.llm_usage.get("log"):
            st.dataframe(pd.DataFrame(br.llm_usage["log"]), hide_index=True)

with tab_docs:
    df = documents_table(br, decisions)
    pick = st.multiselect("Filter status", STATUSES, default=[s for s in STATUSES if t.status_counts[s]])
    view = df[df["Status"].isin(pick)].drop(columns=["_key"])
    cols = ["Document no.", "Type", "PDF source", "ERP entry", "PDF total", "ERP total", "Difference (PDF-ERP)",
            "Status", "Headline", "Reason codes", "Confidence", "Reviewer decision", "Decision state"]
    st.dataframe(style_status(view[cols]), hide_index=True, width="stretch", height=600)
    st.markdown("#### Line-level discrepancies")
    ld = line_discrepancy_table(br)
    if ld.empty:
        st.write("None.")
    else:
        st.dataframe(ld.drop(columns=["Explanation"]), hide_index=True, width="stretch")

with tab_queue:
    queue = [r for r in br.results if r.review_required]
    done = sum(1 for r in queue if not open_for_review(r))
    st.progress(done / len(queue) if queue else 1.0, f"{done} of {len(queue)} decided")
    reviewer = st.text_input("Your name", value=st.session_state.get("reviewer", ""), key="reviewer")
    for r in sorted(queue, key=lambda r: (not open_for_review(r), status_rank(r.status), -abs(r.difference or 0))):
        ds = decisions.get(r.key)
        icon = "🟡" if ds is None else ("✅" if ds.current else "⚠️")
        with st.expander(f"{icon} {r.doc_no} — {r.status} — {r.headline}", expanded=False):
            a, b, c = st.columns(3)
            a.metric("PDF total", fmt_idr(r.pdf_total))
            b.metric("ERP total", fmt_idr(r.erp_total))
            c.metric("Difference", fmt_signed(r.difference))
            st.markdown(f"**What happened.** {r.explanation}")
            st.markdown(f"**Suggested action.** {r.action}")
            st.caption("Reason codes: " + ", ".join(f"`{c}`" for c in r.reasons) + f" · confidence {r.confidence}")
            if r.line_discrepancies:
                ld = line_discrepancy_table(br)
                st.dataframe(ld[ld["Document no."] == r.doc_no].drop(columns=["Document no.", "Status", "Explanation"]),
                             hide_index=True, width="stretch")
            if ds:
                dec = ds.decision
                said = f"{dec.decision} by {dec.reviewer} at {dec.at}" + (f" — {dec.note}" if dec.note else "")
                if ds.current:
                    st.success(said)
                else:
                    st.warning(f"Earlier decision no longer applies ({ds.stale}): {said}. Decide again.")
            with st.form(f"f_{r.key}"):
                choice = st.radio("Decision", DECISIONS, horizontal=True, index=None)
                note = st.text_input("Note")
                if st.form_submit_button("Save decision"):
                    try:
                        log.record(r, choice or "", reviewer or "", note)
                    except ValueError:
                        st.warning("Pick a decision and enter your name.")
                    else:
                        st.rerun()

with tab_audit:
    labels = [f"{r.doc_no} — {r.status}" for r in br.results]
    i = st.selectbox("Document", range(len(labels)), format_func=lambda k: labels[k])
    r = br.results[i]
    st.markdown(f"### {r.doc_no}  ·  {r.status}")
    st.markdown(f"**{r.headline}** — {r.explanation}")
    st.markdown(f"**Suggested action:** {r.action}")

    left, right = st.columns([1, 1])
    with left:
        st.markdown("#### Figures side by side")
        p = r.pdf_docs[0] if r.pdf_docs else None
        e = r.erp_docs[0] if r.erp_docs else None

        def src(d, k):
            f = d.fields.get(k) if d else None
            return f.src.label().split(" › ")[-1] if f and f.src else ""
        rows = []
        for k, lab in (("doc_no", "Document no."), ("doc_date", "Date"), ("po_no", "PO"), ("reference", "Reference"),
                       ("subtotal", "Subtotal"), ("dpp", "DPP"), ("ppn", "PPN"), ("total", "Total")):
            pv = getattr(p, k, None) if p else None
            ev = getattr(e, k, None) if e else None
            fmt = (lambda v: f"{v:,}") if k in ("subtotal", "dpp", "ppn", "total") else (lambda v: str(v or ""))
            rows.append({"Field": lab, "PDF": fmt(pv) if pv is not None else "", "PDF source": src(p, k),
                         "ERP": fmt(ev) if ev is not None else "", "ERP source": src(e, k),
                         "Match": "" if pv is None or ev is None else ("✓" if (pv == ev or k == "doc_no" and p.key == e.key) else "✗")})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if r.line_pairs:
            st.markdown("#### Lines (matched by item code)")
            lrows = []
            for lp in r.line_pairs:
                a, b = lp.pdf, lp.erp
                lrows.append({"PDF #": a.line_no if a else None, "ERP #": b.line_no if b else None, "SKU": lp.sku,
                              "Qty PDF/ERP": f"{fmt_num(a.qty) if a else '—'} / {fmt_num(b.qty) if b else '—'}",
                              "Price PDF/ERP": f"{fmt_num(a.unit_price) if a else '—'} / {fmt_num(b.unit_price) if b else '—'}",
                              "Disc PDF/ERP": f"{fmt_num(a.disc_pct) if a else '—'} / {fmt_num(b.disc_pct) if b else '—'}",
                              "Amount PDF": a.amount if a else None, "Amount ERP": b.amount if b else None,
                              "Diff": lp.amount_diff, "Issue": ", ".join(lp.diffs) or "✓"})
            st.dataframe(pd.DataFrame(lrows).style.format({"Amount PDF": "{:,.0f}", "Amount ERP": "{:,.0f}", "Diff": "{:,.0f}"},
                                                          na_rep=""), hide_index=True, width="stretch")
        chk = checks_table(br)
        chk = chk[chk["Document no."] == r.doc_no]
        if not chk.empty:
            st.markdown("#### Arithmetic checks (R1–R4) on each side")
            st.dataframe(chk.drop(columns=["Document no."]).style.map(
                lambda v: "color:#b91c1c;font-weight:600" if v == "FAIL" else ("color:#a16207" if v == "ROUNDING" else ""),
                subset=["Result"]), hide_index=True, width="stretch")
    with right:
        if r.pdf_docs:
            boxes = figure_boxes(r)
            d0 = r.pdf_docs[0]
            fname = d0.fields.get("file").value if d0.fields.get("file") else ""
            path = st.session_state.pdf_map.get(fname)
            pages = [pg for d in r.pdf_docs for pg in d.pages]
            pg = st.radio("PDF page", pages, horizontal=True) if len(pages) > 1 else pages[0]
            st.caption("Blue = extracted figure, red = figure behind a discrepancy. Every figure in the audit "
                       "table below carries its page and box.")
            if path:
                st.image(render_page(path, pg, tuple(boxes.get(pg, []))), width="stretch")
        else:
            st.info("No supplier document — ERP entry only.")
    st.markdown("#### Audit trail — every figure and where it came from")
    st.dataframe(audit_table(br, r.key).drop(columns=["BBox"]), hide_index=True, width="stretch", height=320)

with tab_extract:
    st.dataframe(extraction_table(br), hide_index=True, width="stretch")
    st.markdown("#### ERP export as read")
    st.dataframe(erp_load_table(br), hide_index=True, width="stretch")
    with st.expander("Reason codes (the status policy)"):
        st.dataframe(reason_code_table(), hide_index=True, width="stretch")

with tab_dl:
    out_dir = OUT / bid
    sig = (bid, st.session_state.run_no, log.version)     # rewrite only after a new run or a new decision
    if st.session_state.get("reports_sig") != sig:
        st.session_state.reports = write_reports(br, out_dir, decisions)
        st.session_state.reports_sig = sig
    paths = st.session_state.reports
    st.write(f"Reports (including {len(decisions)} reviewer decisions) written to `{out_dir}`.")
    for k, label, mime in (("excel", "Reconciliation report (Excel, all sheets)",
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                           ("documents_csv", "Document report (CSV)", "text/csv"),
                           ("lines_csv", "Line discrepancies (CSV)", "text/csv"),
                           ("audit_csv", "Audit trail (CSV)", "text/csv"),
                           ("summary_txt", "Customer summary (text)", "text/plain")):
        st.download_button(label, paths[k].read_bytes(), file_name=paths[k].name, mime=mime, key=f"dl_{k}")
