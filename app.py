"""Streamlit UI for the AP team: run a batch, work the review queue, audit any figure.

    recon ui              (same as: streamlit run app.py)
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from recon.config import Config, ConfigError
from recon.llm import GroqLLM
from recon.normalize import fmt_idr, fmt_signed, norm_vendor
from recon.pipeline import DEFAULT_CONFIG, run_batch
from recon.profile_draft import (generate_profile, grouped, llm_documents, recheck, register_supplier, save_profile,
                                 unknown_layout_pages)
from recon.reason_codes import STATUSES, status_rank
from recon.render import render_page
from recon.report import (STATUS_FILL, audit_table, checks_table, control_totals_table, document_boxes, documents_table,
                          erp_load_table, extraction_table, figure_boxes, figure_comparison_table,
                          line_discrepancy_table, line_pairs_table, reason_code_table, write_reports)
from recon.review import DECISIONS, DecisionLog, batch_id
from recon.uploads import save_upload

load_dotenv()
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "output"

st.set_page_config(page_title="AP Reconciliation", page_icon="🧾", layout="wide")


# --------------------------------------------------------------------------- helpers

def style_status(df: pd.DataFrame):
    def color(v):
        return f"background-color: #{STATUS_FILL[v]}; color: #111" if v in STATUS_FILL else ""
    money = [c for c in df.columns if c in ("PDF total", "ERP total", "Difference (PDF-ERP)")]
    sty = df.style.map(color, subset=["Status"]) if "Status" in df.columns else df.style
    return sty.format({c: lambda v: "" if pd.isna(v) else f"{int(v):,}" for c in money})


@st.cache_data(show_spinner=False)
def cached_page(pdf_path: str, page_no: int, boxes: tuple) -> bytes:
    return render_page(pdf_path, page_no, boxes)


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
            pdf_paths = [save_upload(f.name, f.getvalue(), tmp) for f in ups]
            erp_path = save_upload(upx.name, upx.getvalue(), tmp)

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
        fallback_text = st.text_input("Fallback text model (when rate-limited)", conf.llm.fallback_text_model)
        fallback_vision = st.text_input("Fallback vision model (when rate-limited)", conf.llm.fallback_vision_model)
    run = st.button("Run reconciliation", type="primary", disabled=not (pdf_paths and erp_path), width="stretch")
    st.caption("Arithmetic, matching, statuses and explanations are deterministic code. "
               "The LLM only reads pages and words the summary; its output is re-checked by code.")

rerun = st.session_state.pop("rerun_batch", False) and bool(pdf_paths and erp_path)     # after a profile is saved
if run or rerun:
    st.session_state.pop("profile_draft", None)
    llm = None
    if key and (use_llm_extract or use_llm_summary):
        llm = GroqLLM(conf.llm, api_key=key, text_model=text_model, vision_model=vision_model,
                      fallback_text_model=fallback_text, fallback_vision_model=fallback_vision)
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
if saved := st.session_state.pop("profile_saved", None):
    st.success(f"Profile saved to `{saved}`, and the batch was read again with it.")
if registered := st.session_state.pop("supplier_registered", None):
    st.info(registered)
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


def profile_onboarding(unknown: dict):
    """Pages no supplier profile read: the LLM drafts a profile, code dry-runs it, a person saves it."""
    st.markdown("#### New layout → supplier profile")
    fname = st.selectbox("PDF", list(unknown)) if len(unknown) > 1 else next(iter(unknown))
    pages = unknown[fname]
    path = st.session_state.pdf_map.get(fname)
    refs = llm_documents(br.pdf_docs, fname, pages)
    st.caption(f"{len(pages)} page(s) of {fname} were read without a supplier profile, so their figures have no box on "
               f"the page. A drafted profile is run through the parser on these pages, compared with the LLM's read "
               f"({len(refs)} document(s)) and with R1–R4, and is saved only when you confirm.")
    names = sorted({d.vendor for d in br.erp.documents if d.vendor})
    read_as = {norm_vendor(d.vendor) for d in refs if d.vendor}
    erp_names = st.multiselect("Supplier name(s) in the ERP export", names,
                               default=[n for n in names if norm_vendor(n) in read_as])
    if st.button("Generate profile", disabled=not (key and erp_names and path),
                 help=None if key else "Needs a GROQ_API_KEY (sidebar)."):
        llm = GroqLLM(conf.llm, api_key=key, text_model=text_model, vision_model=vision_model,
                      fallback_text_model=fallback_text, fallback_vision_model=fallback_vision)
        with st.spinner("Drafting the profile and running it on the pages"):
            try:
                st.session_state.profile_draft = generate_profile(llm, path, pages, list(conf.profiles), refs, erp_names,
                                                                  conf.rule_config)
                st.session_state.draft_no = st.session_state.get("draft_no", 0) + 1
            except Exception as e:
                st.error(f"Drafting failed: {type(e).__name__}: {e}")
    draft = st.session_state.get("profile_draft")
    if draft is None or draft.file != fname:
        return

    chk = draft.check
    st.caption("LLM calls: " + "; ".join(f"#{a['attempt']} {a['model']}"
                                         + (f" (fallback, {a['fallback_from']} rate-limited)" if a.get("fallback_from") else "")
                                         + ("" if a.get("valid", True) else ": not a valid profile")
                                         + f": {a['blocking']} blocking, {a['disagreements']} disagreeing, "
                                         f"{a.get('prompt_tokens', 0):,} in + {a.get('completion_tokens', 0):,} out tokens, "
                                         f"${a['cost_usd']:.4f}" for a in draft.attempts)
               + (" · edited by you" if draft.edited else ""))
    if chk.repairs:
        st.caption("Corrected by code before checking: " + "; ".join(chk.repairs))
    if chk.savable and not chk.needs_look:
        st.success(f"The draft reads all {len(pages)} page(s) and agrees with the LLM on every figure "
                   f"({len(chk.rows)} compared).")
    if chk.blocking:
        blocking = grouped(chk.blocking)          # the same mistake on every document is one line
        st.error("**Cannot be saved yet**\n\n" + "\n".join(f"- {m}" for m in blocking[:15])
                 + (f"\n- … and {len(blocking) - 15} more" if len(blocking) > 15 else ""))
    look = grouped(chk.disagreements + chk.to_check)
    if look:
        st.warning("**Look at these on the page before saving**\n\n" + "\n".join(f"- {m}" for m in look[:15])
                   + (f"\n- … and {len(look) - 15} more" if len(look) > 15 else ""))

    left, right = st.columns([1, 1])
    with left:
        rows = pd.DataFrame(chk.rows)
        if not rows.empty:
            if st.toggle("Only disagreements", value=bool(chk.disagreements)):
                rows = rows[rows["Agree"] == "NO"]
            st.dataframe(rows, hide_index=True, width="stretch", height=420)
    with right:
        boxes = document_boxes(chk.docs, chk.red)
        pg = st.radio("Page", pages, horizontal=True, key="draft_page") if len(pages) > 1 else pages[0]
        st.caption("Boxes = figures the draft profile reads. Red = the draft and the LLM read disagree.")
        st.image(cached_page(path, pg, tuple(boxes.get(pg, []))), width="stretch")

    with st.expander("Profile JSON (edit and re-check if needed)", expanded=bool(chk.blocking)):
        text = st.text_area("Profile", draft.text, height=420, key=f"draft_json_{st.session_state.get('draft_no', 0)}",
                            label_visibility="collapsed")
        if st.button("Re-check edited profile"):
            st.session_state.profile_draft = recheck(draft, text, path, list(conf.profiles), refs, conf.rule_config)
            st.rerun()

    with st.form("save_profile"):
        days = conf.rules.new_supplier_review_days
        st.caption(f"Saving also adds the supplier to `config/supplier_register.json` with today as its go-live date "
                   f"(unless it is already there): its documents are reviewed by a person for {days} days.")
        who = st.text_input("Your name", value=st.session_state.get("reviewer", ""))
        looked = st.checkbox("I compared the flagged figures with the page") if chk.needs_look else True
        confirm = st.form_submit_button("Confirm and save profile", type="primary", disabled=not chk.savable)
    if confirm:
        try:
            saved = save_profile(draft, conf.dir, who, looked)
        except (ValueError, FileExistsError, ConfigError) as e:
            st.error(f"Not saved: {e}")
        else:
            st.session_state.profile_saved = str(saved.relative_to(ROOT) if saved.is_relative_to(ROOT) else saved)
            prof, today = chk.profile, datetime.now().date()
            assert prof is not None             # save_profile refuses a draft without one
            try:        # the profile stands without it: an unregistered supplier is reviewed anyway
                added = register_supplier(conf.dir, [prof.vendor_name, *prof.erp_vendor_names], today)
            except (OSError, ValueError) as e:
                st.session_state.supplier_registered = f"Not added to the supplier register ({e}): add it by hand."
            else:
                until = today + timedelta(days=conf.rules.new_supplier_review_days)
                st.session_state.supplier_registered = (
                    f"{', '.join(added)} added to the supplier register, live from {today:%d %b %Y}: documents dated "
                    f"before {until:%d %b %Y} are reviewed by a person." if added else
                    f"{prof.vendor_name} was already in the supplier register; its go-live date is unchanged.")
            st.session_state.rerun_batch = True
            st.rerun()


# --------------------------------------------------------------------------- tabs

def remember_tab(labels: list[str]):
    st.session_state.tab_no = labels.index(st.session_state.main_tab)


# The tab is kept by position: the queue count in its label changes the widget's identity, which would reset it
# to Overview after a button press (Generate profile, Save decision) unless the default carries the last choice.
tab_labels = ["Overview", "Documents", f"Review queue ({sum(1 for r in br.results if open_for_review(r))})",
              "Audit a document", "Extraction log", "Download"]
tab_over, tab_docs, tab_queue, tab_audit, tab_extract, tab_dl = st.tabs(
    tab_labels, default=tab_labels[st.session_state.get("tab_no", 0)], key="main_tab",
    on_change=remember_tab, args=(tab_labels,))

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
        st.dataframe(figure_comparison_table(r), hide_index=True, width="stretch")
        if r.line_pairs:
            st.markdown("#### Lines (matched by item code)")
            money = {"Amount PDF": "{:,.0f}", "Amount ERP": "{:,.0f}", "Diff": "{:,.0f}"}
            st.dataframe(line_pairs_table(r).style.format(money, na_rep=""), hide_index=True, width="stretch")
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
                st.image(cached_page(path, pg, tuple(boxes.get(pg, []))), width="stretch")
        else:
            st.info("No supplier document — ERP entry only.")
    st.markdown("#### Audit trail — every figure and where it came from")
    st.dataframe(audit_table(br, r.key).drop(columns=["BBox"]), hide_index=True, width="stretch", height=320)

with tab_extract:
    st.dataframe(extraction_table(br), hide_index=True, width="stretch")
    if unknown := unknown_layout_pages(br.page_logs):
        profile_onboarding(unknown)
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
