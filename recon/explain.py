"""Plain-language explanation, suggested action and headline for one result.

Templates over computed facts, so the same input always yields the same,
verifiable sentence. Which codes are arithmetic errors, which statuses are
approvable etc. comes from the reason-code registry.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .matching import DocGroup, LinePair
from .models import CheckResult, Document
from .normalize import fmt_idr, fmt_money, fmt_num, fmt_signed
from .reason_codes import APPROVABLE_STATUSES, EXISTENCE_STATUSES, code, in_group
from .rules import RuleConfig, rule_ppn

if TYPE_CHECKING:
    from .reconcile import DocResult

BRIDGE_SHORT = {
    "pdf_subtotal_error": "the invoice's subtotal error",
    "erp_subtotal_error": "the ERP subtotal error",
    "tax_on_subtotal_diff": "PPN on that subtotal difference",
    "pdf_tax_calc": "the invoice's PPN calculation",
    "erp_tax_calc": "the ERP's PPN calculation",
    "pdf_total_error": "the invoice's total calculation",
    "erp_total_error": "the ERP's total calculation",
}


def _kind(d: Document) -> str:
    return "credit note" if d.doc_type == "CREDIT_NOTE" else "invoice"


def describe_line_pair(lp: LinePair) -> str:
    p, e = lp.pdf, lp.erp
    if p is not None and e is None:
        return (f"Invoice line {p.line_no} {p.sku} ({p.description}) for {fmt_idr(p.amount)} "
                f"is not in the ERP entry.")
    if e is not None and p is None:
        return f"ERP line {e.line_no} {e.sku} ({e.description}) for {fmt_idr(e.amount)} is not on the invoice."
    parts = []
    uom = (p.uom or e.uom or "").upper()
    if "qty" in lp.diffs:
        parts.append(f"quantity {fmt_num(p.qty)} {uom} on the invoice vs {fmt_num(e.qty)} {uom} in the ERP")
    if "unit_price" in lp.diffs:
        parts.append(f"unit price {fmt_money(p.unit_price)} on the invoice vs {fmt_money(e.unit_price)} in the ERP")
    if "disc_pct" in lp.diffs:
        parts.append(f"discount {fmt_num(p.disc_pct)}% on the invoice vs {fmt_num(e.disc_pct)}% in the ERP")
    if "uom" in lp.diffs:
        parts.append(f"unit {p.uom} vs {e.uom}")
    if "amount" in lp.diffs:
        parts.append("same quantity, price and discount but a different line amount")
    return (f"Line {p.line_no} {p.sku} ({p.description}): {', '.join(parts)}; "
            f"line amount {fmt_idr(p.amount)} vs {fmt_idr(e.amount)} ({fmt_signed(lp.amount_diff)}).")


def _describe_check(c: CheckResult, d: Document, cfg: RuleConfig) -> str:
    who = f"ERP entry {d.entry_id}" if d.side == "ERP" else f"The {_kind(d)}"
    if c.rule == "R1":
        return (f"{who}, {c.target}: quantity × price × (1 − discount) = {fmt_idr(c.expected)}, "
                f"but it shows {fmt_idr(c.stated)}.")
    if c.rule == "R2":
        hi = "higher" if c.delta > 0 else "lower"
        return (f"{who} states a subtotal of {fmt_idr(c.stated)}, but its lines add up to {fmt_idr(c.expected)} — "
                f"{fmt_idr(abs(c.delta))} {hi}. DPP, PPN and total were calculated from that wrong subtotal.")
    if c.rule == "R3":
        label = d.fields.get("dpp_label")
        lab = f" (labelled '{label.value}')" if label and label.value else ""
        msg = (f"{who} shows DPP{lab} of {fmt_idr(c.stated)}; the rule is subtotal × 11/12 = {fmt_idr(c.expected)}.")
        if d.subtotal is not None and c.stated == d.subtotal:
            over = (d.ppn or 0) - rule_ppn(d.subtotal, cfg)
            msg += (f" It charged 12% PPN on the full subtotal instead of the 11/12 base, so PPN is "
                    f"{fmt_idr(abs(over))} too {'high' if over > 0 else 'low'}.")
        return msg
    if c.target == "PPN":
        return f"{who} shows PPN of {fmt_idr(c.stated)}; DPP × 12% = {fmt_idr(c.expected)}."
    return f"{who} shows a total of {fmt_idr(c.stated)}; subtotal + PPN = {fmt_idr(c.expected)}."


def explain(r: DocResult, g: DocGroup, cfg: RuleConfig) -> tuple[str, str, str]:
    S: list[str] = []          # explanation sentences
    A: list[str] = []          # actions, most important first
    p = r.pdf_docs[0] if r.pdf_docs else None
    e = r.erp_docs[0] if r.erp_docs else None
    codes = set(r.reasons)
    headline = ""

    if r.status == "DUPLICATE_IN_ERP":
        ids = ", ".join(d.entry_id for d in r.erp_docs)
        same = all(d.total == e.total and len(d.lines) == len(e.lines) for d in r.erp_docs)
        extra = sum(d.total or 0 for d in r.erp_docs[1:])
        S.append(f"{r.doc_no} is booked {len(r.erp_docs)} times in the ERP ({ids})"
                 + (", with identical lines and totals" if same else "") + ". "
                 f"Paying every entry would overpay by {fmt_idr(extra)}.")
        A.append(f"Reverse duplicate ERP entr{'y' if len(r.erp_docs) == 2 else 'ies'} "
                 f"{', '.join(d.entry_id for d in r.erp_docs[1:])} before the next payment run.")
        headline = f"Booked twice in ERP — {fmt_idr(extra)} double-payment risk"
    if r.status == "DUPLICATE_IN_PDF":
        pages = "; ".join("p." + ",".join(map(str, d.pages)) for d in r.pdf_docs)
        S.append(f"{r.doc_no} appears {len(r.pdf_docs)} times in the supplier PDF ({pages}).")
        A.append("Process the document once and tell the supplier it was sent twice.")
        headline = "Same document sent twice by supplier"

    if r.status == "MISSING_IN_ERP":
        po = f", {p.po_no}" if p.po_no else ""
        dated = f" dated {p.doc_date:%d %b %Y}" if p.doc_date else ""
        S.append(f"The {_kind(p)} {p.doc_no}{dated}{po} for {fmt_idr(abs(p.total or 0))} "
                 f"(PDF p.{','.join(map(str, p.pages))}) has no entry in the ERP.")
        if p.doc_type == "CREDIT_NOTE":
            A.append("Book the credit note so the amount owed is reduced.")
        else:
            A.append(f"Check that the goods{(' for ' + p.po_no) if p.po_no else ''} were received, then book the invoice. "
                     "Do not pay until it is booked and approved.")
        headline = f"Not booked in ERP ({fmt_idr(abs(p.total or 0))})"
    if r.status == "MISSING_IN_PDF":
        st = f", status {e.status_in_source}" if e.status_in_source else ""
        S.append(f"ERP entry {e.entry_id} ({e.doc_no}, {fmt_idr(abs(e.total or 0))}{st}) has no matching "
                 f"supplier document in this PDF batch.")
        keep = "Keep it parked" if "ERP_PARKED" in codes else "Block it for payment"
        A.append(f"Ask the supplier for a copy of {e.doc_no} or confirm it was never issued. {keep} until the document arrives.")
        headline = f"In ERP but no supplier invoice ({fmt_idr(abs(e.total or 0))})"

    if p and e:
        if "MATCHED_BY_SECONDARY_KEY" in codes:
            S.append(f"The document numbers differ (invoice {p.doc_no}, ERP {e.doc_no} on {e.entry_id}); "
                     f"they were paired because they have the {g.secondary_basis}.")
            A.append("Confirm with the supplier which document number is right, and correct the ERP reference.")
        if "DOC_NO_FORMAT_DIFF" in codes:
            S.append(f"The ERP stores the number as '{e.doc_no}'; it is the same number written differently.")
        if "PO_MISMATCH" in codes:
            S.append(f"The invoice quotes {p.po_no} but ERP entry {e.entry_id} is booked against {e.po_no}"
                     + (" (the digits look transposed)" if sorted(p.po_no) == sorted(e.po_no) else "") + ".")
            A.append("Confirm the correct PO with purchasing and fix the PO reference before payment.")
        if "CN_REFERENCE_MISMATCH" in codes:
            S.append(f"The credit note refers to {p.reference}; the ERP links it to {e.reference}.")
            A.append("Confirm which invoice the credit note settles and correct the ERP link.")
        if "DOC_TYPE_MISMATCH" in codes:
            S.append(f"The PDF document is a {_kind(p)} but the ERP books it as a {_kind(e)}.")
            A.append("Correct the document type in the ERP.")
        if "DATE_MISMATCH" in codes:
            S.append(f"Dates differ: {p.doc_date:%d %b %Y} on the PDF vs {e.doc_date:%d %b %Y} in the ERP.")

        for lp in r.line_discrepancies:
            S.append(describe_line_pair(lp))
        b = r.bridge
        if b.line_diff:
            S.append(f"Lines differ by {fmt_signed(b.line_diff)}; with PPN on that ({fmt_signed(b.tax_on_subtotal_diff)}) "
                     f"the {_kind(p)} total is {fmt_signed((p.total or 0) - (e.total or 0))} against the ERP.")
            A.extend(_line_actions(r))
            headline = headline or _line_headline(r)
        elif (p.total or 0) != (e.total or 0) and r.status != "MATCHED_WITHIN_TOLERANCE":
            parts = [(v, BRIDGE_SHORT[k]) for k, v in b.items() if v and k in BRIDGE_SHORT]
            if parts:
                d = (p.total or 0) - (e.total or 0)
                if len(parts) == 1:
                    S.append(f"Against ERP entry {e.entry_id} the {_kind(p)} is {fmt_signed(d)}, all from {parts[0][1]}.")
                else:
                    S.append(f"Against ERP entry {e.entry_id} the {_kind(p)} is {fmt_signed(d)}: "
                             + " and ".join(f"{fmt_signed(v)} from {lab}" for v, lab in parts) + ".")

    # document's own arithmetic
    hard_any = False
    for d in r.pdf_docs + r.erp_docs:
        chk = r.checks_for(d)
        for c in chk.hard:
            S.append(_describe_check(c, d, cfg))
            hard_any = True
        if not chk.hard:
            for c in chk.rounding:
                S.append(f"{'ERP entry ' + d.entry_id if d.side == 'ERP' else 'The invoice'} shows {c.target} "
                         f"{fmt_idr(c.stated)}; the rule gives {fmt_idr(c.expected)} — a {fmt_idr(abs(c.delta))} "
                         f"rounding difference.")
    # an unread figure makes that side's arithmetic flags unreliable: don't blame the supplier or the ERP for it
    pdf_unread = [n for d in r.pdf_docs for n in d.unread_notes]
    erp_unread = [n for d in r.erp_docs for n in d.unread_notes]
    pdf_errors = in_group(r.reasons, "pdf_arithmetic")
    if pdf_errors and not pdf_unread:
        ok_erp = e is not None and not r.checks_for(e).hard
        A.insert(0, f"Do not pay from this {_kind(p)}: ask the supplier for a corrected {_kind(p)} (and tax invoice)."
                 + (f" The ERP figure ({fmt_idr(e.total)}) follows the rules." if ok_erp else ""))
        diff = f" ({fmt_signed(r.difference)})" if r.difference else ""
        headline = headline or f"Supplier's figures don't add up: {code(pdf_errors[0]).short}{diff}"
    if in_group(r.reasons, "erp_arithmetic") and not erp_unread:
        A.append("Correct the ERP entry figures before payment.")
        headline = headline or "ERP entry figures do not add up"
    if pdf_unread or erp_unread:
        S.extend("Not read: " + n.split(": ", 1)[1] + "." for n in pdf_unread + erp_unread)
        A.insert(0, "Read the unreadable figure from the source, correct the extraction (or supplier profile) and re-run. "
                    "Don't raise arithmetic differences with the supplier or change the ERP until then: "
                    "they may come from the unread figure.")
        if r.status not in EXISTENCE_STATUSES:
            headline = "A figure could not be read: check the source before acting"

    if p and e and "TOTAL_DIFF" in codes and not r.line_discrepancies and not hard_any:
        S.append(f"All lines agree but totals differ: PPN {fmt_idr(p.ppn)} on the invoice vs {fmt_idr(e.ppn)} in the ERP.")
        A.append("Check how PPN was calculated on both sides.")

    if r.status == "MATCHED_WITHIN_TOLERANCE":
        S.insert(0, f"All {len(r.line_pairs)} lines agree. Totals differ by {fmt_idr(abs(r.difference))}, "
                    f"within the IDR {cfg.total_tolerance} rounding tolerance.")
        A.append("Approve for payment; rounding difference only, no correction needed.")
        headline = f"Rounding difference of {fmt_idr(abs(r.difference))}"
    if r.status == "MATCHED":
        what = "credit note" if p.doc_type == "CREDIT_NOTE" else "invoice"
        S.insert(0, f"The {what} agrees with ERP entry {e.entry_id} on all {len(r.line_pairs)} lines, subtotal, "
                    f"PPN and total ({fmt_idr(p.total)}).")
        A.append("Approve for payment." if what == "invoice" else "Apply the credit note against the next payment.")
        headline = "Agrees on every line and total"

    if "CREDIT_NOTE" in codes and p:
        ref = f" and refers to invoice {p.reference}" if p.reference else ""
        S.append(f"Credit note: it reduces the amount owed by {fmt_idr(abs(p.total or 0))}{ref}.")
    if "EXTRACTED_BY_OCR" in codes:
        pages = ",".join(str(pg) for d in r.pdf_docs for pg in d.pages)
        S.append(f"PDF p.{pages} is a scanned image; its figures were read by OCR"
                 + (" and confirmed by a second (vision model) read" if any("llm_vision" in d.method for d in r.pdf_docs) else "")
                 + (" and passed every arithmetic check." if not hard_any else "."))
    if "EXTRACTED_BY_LLM" in codes:
        S.append("Figures were read by an LLM; verify them against the PDF page before approving.")
    if "EXTRACTION_CONFLICT" in codes:
        A.insert(0, "Check the highlighted figures against the PDF page: two reads of the scan disagree.")
    if "EXTRACTION_LOW_CONFIDENCE" in codes and r.status in APPROVABLE_STATUSES:
        A.insert(0, "Verify the figures against the PDF page before approving (low extraction confidence).")
    if "ERP_VALUE_STORED_AS_TEXT" in codes:
        cells = [n.split(" › ")[-1] for d in r.erp_docs for n in d.normalisation_notes]
        S.append("ERP formatting normalised: " + "; ".join(cells) + ".")
    if "MULTI_PAGE_DOC" in codes and p:
        S.append(f"The {_kind(p)} runs over PDF pages {p.pages[0]}–{p.pages[-1]}; lines were matched by item code, not position.")

    if "NEW_SUPPLIER" in codes:
        S.append(r.new_supplier_note)
        if r.status in APPROVABLE_STATUSES:
            A.insert(0, "New supplier: check the figures against the PDF page before approving.")

    if r.status == "NEEDS_REVIEW" and not headline:
        headline = "Key references disagree" if codes & {"PO_MISMATCH", "CN_REFERENCE_MISMATCH", "MATCHED_BY_SECONDARY_KEY"} \
            else "Needs a reviewer's decision"
    return " ".join(S), " ".join(dict.fromkeys(A)), headline


def _line_actions(r: DocResult) -> list[str]:
    acts = []
    lds = r.line_discrepancies
    total_diff = r.bridge.line_diff + r.bridge.tax_on_subtotal_diff
    if any("qty" in lp.diffs for lp in lds):
        skus = ", ".join(lp.sku for lp in lds if "qty" in lp.diffs)
        acts.append(f"Check the goods receipt for {skus}. If the ERP quantity is what was received, ask the supplier "
                    f"for a credit note of {fmt_idr(abs(total_diff))} (incl. PPN); if more was received, update the ERP entry.")
    if any("unit_price" in lp.diffs or "disc_pct" in lp.diffs for lp in lds):
        skus = ", ".join(lp.sku for lp in lds if {"unit_price", "disc_pct"} & set(lp.diffs))
        acts.append(f"Check the agreed price/discount for {skus} on the PO. If the ERP is right, ask the supplier for a "
                    f"corrected invoice or credit note of {fmt_idr(abs(total_diff))} (incl. PPN).")
    for lp in lds:
        if "extra_in_pdf" in lp.diffs:
            acts.append(f"Confirm whether '{lp.pdf.description}' ({fmt_idr(lp.pdf.amount)}) was agreed on the PO. "
                        "If yes, add it to the ERP entry; if not, dispute it with the supplier.")
        if "missing_from_pdf" in lp.diffs:
            acts.append(f"Confirm whether {lp.erp.sku} was delivered and invoiced; correct the ERP entry if not.")
    acts.append("Pay only the undisputed amount until resolved.")
    return acts


def _line_headline(r: DocResult) -> str:
    lds = r.line_discrepancies
    kinds = []
    for key, phrase in (("qty", "Quantity difference"), ("unit_price", "Price difference"),
                        ("disc_pct", "Discount difference"), ("uom", "Unit difference"),
                        ("amount", "Line amount difference"), ("extra_in_pdf", "Extra line on invoice"),
                        ("missing_from_pdf", "Line missing from invoice")):
        if any(key in lp.diffs for lp in lds):
            kinds.append(phrase)
    return " + ".join(kinds) + f" ({fmt_signed(r.difference)})"
