"""Drafting a profile for a new layout: the dry run that judges a draft, the one revision, and saving it
(fake LLM, a synthetic PDF in the PT Sagara Digital Supply layout)."""
import copy
import json
import shutil
from datetime import date, datetime
from pathlib import Path

import pymupdf
import pytest

from recon.config import LLMConfig, load_profiles, load_register
from recon.extract import extract_pdf
from recon.llm import PROFILE_PROMPT, GroqLLM
from recon.profile_draft import (dry_run, example_text, generate_profile, recheck, register_supplier, sample_pages,
                                 sample_text, save_profile,
                                 unknown_layout_pages)
from recon.report import document_boxes
from tests.fakes import FakeGroqClient, FakeLLM

ERP_NAME = "PT SAGARA DIGITAL SUPPLY"

SAGARA = {
    "name": "PT Sagara Digital Supply", "vendor_name": "PT SAGARA DIGITAL SUPPLY", "erp_vendor_names": [ERP_NAME],
    "detect": ["SAGARA DIGITAL SUPPLY"], "decimal_separator": ",", "date_order": "DMY",
    "layout": {
        "doc_types": {"CREDIT_NOTE": ["CREDIT NOTE"], "INVOICE": ["TAX INVOICE", "INVOICE"]},
        "header_fields": {
            "doc_no": {"labels": ["Jl. Teknologi Raya"], "pattern": r"SDS/(CN/)?\d{4}/\d{2}/\d{3}"},
            "doc_date": {"labels": ["Date:"], "pattern": r"\d{1,2} [A-Za-z]{3} \d{4}"},
            "due_date": {"labels": ["Due:"], "pattern": r"\d{1,2} [A-Za-z]{3} \d{4}"},
            "po_no": {"labels": ["PO Reference:"], "pattern": r"[A-Z]{2,5}-[A-Z]{2,4}-\d{4}-\d{3}"},
            "reference": {"labels": ["Reference:"], "pattern": r"SDS/\d{4}/\d{2}/\d{3}"}},
        "table": {"header_keywords": ["ITEM", "QTY", "DISC"], "sku_header": "ITEM",
                  "stop_markers": ["PAYMENT INSTRUCTIONS", "CREDIT NOTE INFORMATION"],
                  "right_fields": [{"name": "qty", "type": "number"}, {"name": "uom", "type": "alpha"},
                                   {"name": "unit_price", "type": "number"},
                                   {"name": "disc_pct", "type": "percent", "optional": True},
                                   {"name": "amount", "type": "number"}]},
        "totals": {"subtotal": {"labels": ["Subtotal"]}, "dpp": {"labels": ["DPP"]}, "ppn": {"labels": ["PPN"]},
                   "total": {"labels": ["TOTAL"]}},
        "footer_margin": 45}}

PAGES = [
    [(50, 72, "PT SAGARA DIGITAL SUPPLY"), (456, 72, "TAX INVOICE"),
     (50, 86, "Jl. Teknologi Raya No. 88, Bandung"), (487, 86, "SDS/2026/10/031"),
     (485, 95, "Date: 02 Oct 2026"), (485, 104, "Due: 01 Nov 2026"),
     (348, 157, "PO Reference: NRG-IT-2610-031"),
     (62, 238, "#"), (84, 238, "ITEM CODE DESCRIPTION QTY UNIT UNIT PRICE DISC. LINE TOTAL"),
     (62, 259, "1"), (84, 259, "LAP-14I Laptop Business 2 unit 12.500.000 5% 23.750.000"),
     (62, 279, "2"), (84, 279, "MON-24F Monitor 24-inch 2 unit 1.850.000 0% 3.700.000"),
     (43, 331, "PAYMENT INSTRUCTIONS"), (390, 345, "Subtotal Rp 27.450.000"),
     (340, 360, "DPP Nilai Lain (11/12) Rp 25.162.500"), (384, 375, "PPN 12% Rp 3.019.500"),
     (269, 394, "TOTAL DUE (IDR) Rp 30.469.500")],
    [(50, 72, "PT SAGARA DIGITAL SUPPLY"), (450, 72, "CREDIT NOTE"),
     (50, 86, "Jl. Teknologi Raya No. 88, Bandung"), (474, 86, "SDS/CN/2026/10/005"),
     (485, 95, "Date: 28 Oct 2026"), (448, 104, "Reference: SDS/2026/10/031"),
     (348, 166, "Reference invoice: SDS/2026/10/031"),
     (62, 238, "#"), (84, 238, "ITEM CODE DESCRIPTION QTY UNIT UNIT PRICE DISC. CREDIT AMOUNT"),
     (62, 261, "1"), (84, 261, "MON-24F Monitor 24-inch 1 unit 1.850.000 0% 1.850.000"),
     (43, 294, "CREDIT NOTE INFORMATION"), (390, 310, "Subtotal Rp 1.850.000"),
     (340, 325, "DPP Nilai Lain (11/12) Rp 1.695.833"), (384, 340, "PPN 12% Rp 203.500"),
     (269, 357, "TOTAL CREDIT (IDR) Rp 2.053.500")],
    # another supplier's page in the same PDF, read by the existing profile
    [(50, 72, "PT PRIMA KERTAS INDONESIA"), (400, 72, "FAKTUR / INVOICE"), (50, 120, "TAX INVOICE copy for the file")],
]

LLM_READ = {   # what the LLM path reads on p.1 and p.2 (call 1 and 2)
    1: {"doc_type": "INVOICE", "vendor": "PT Sagara Digital Supply", "doc_no": "SDS/2026/10/031",
        "doc_date": "02 Oct 2026", "due_date": "01 Nov 2026", "po_no": "NRG-IT-2610-031",
        "lines": [{"line_no": 1, "sku": "LAP-14I", "qty": "2", "uom": "unit", "unit_price": "12.500.000",
                   "disc_pct": "5%", "amount": "23.750.000"},
                  {"line_no": 2, "sku": "MON-24F", "qty": "2", "uom": "unit", "unit_price": "1.850.000",
                   "disc_pct": "0%", "amount": "3.700.000"}],
        "subtotal": "27.450.000", "dpp": "25.162.500", "ppn": "3.019.500", "total": "30.469.500"},
    2: {"doc_type": "CREDIT_NOTE", "vendor": "PT Sagara Digital Supply", "doc_no": "SDS/CN/2026/10/005",
        "doc_date": "28 Oct 2026", "reference": "SDS/2026/10/031",
        "lines": [{"line_no": 1, "sku": "MON-24F", "qty": "1", "uom": "unit", "unit_price": "1.850.000",
                   "disc_pct": "0%", "amount": "1.850.000"}],
        "subtotal": "1.850.000", "dpp": "1.695.833", "ppn": "203.500", "total": "2.053.500"},
}


@pytest.fixture
def sagara_pdf(tmp_path):
    path = tmp_path / "Supplier_Invoices_PT_Sagara.pdf"
    doc = pymupdf.open()
    for rows in PAGES:
        page = doc.new_page()
        for x, y, text in rows:
            page.insert_text((x, y), text, fontsize=8)
    doc.save(path)
    return path


@pytest.fixture(scope="module")
def config_dir(tmp_path_factory):
    """A copy of config/ with only the Prima Kertas profile: a Sagara profile saved from the UI would make the layout known."""
    real = Path(__file__).resolve().parent.parent / "config"
    conf = tmp_path_factory.mktemp("profile_draft") / "config"
    shutil.copytree(real, conf, ignore=lambda d, names: [n for n in names
                                                         if Path(d).name == "suppliers" and n != "prima_kertas.json"])
    return conf


@pytest.fixture
def profiles(config_dir):
    return load_profiles(config_dir)


def llm_batch(pdf, profiles, read=LLM_READ):
    """The batch as it stands before onboarding: p.1-2 read by the LLM, p.3 by the existing profile."""
    return extract_pdf(pdf, profiles, FakeLLM(page=lambda n: read[n]))


def judge(pdf, profiles, data=SAGARA, read=LLM_READ):
    docs, _ = llm_batch(pdf, profiles, read)
    return dry_run(data, pdf, [1, 2], profiles, [d for d in docs if set(d.pages) <= {1, 2}])


def test_unknown_layout_pages_are_the_ones_no_profile_read(sagara_pdf, profiles):
    _, logs = llm_batch(sagara_pdf, profiles)
    assert unknown_layout_pages(logs) == {sagara_pdf.name: [1, 2]}


def test_good_draft_reads_every_page_and_agrees_with_the_llm(sagara_pdf, profiles):
    run = judge(sagara_pdf, profiles)
    assert run.savable and not run.needs_look, (run.blocking, run.disagreements, run.to_check)
    inv, cn = run.docs
    assert (inv.doc_no, inv.doc_date, inv.po_no, inv.total) == ("SDS/2026/10/031", date(2026, 10, 2), "NRG-IT-2610-031", 30469500)
    assert (cn.doc_type, cn.reference, cn.total) == ("CREDIT_NOTE", "SDS/2026/10/031", -2053500)
    assert {r["Agree"] for r in run.rows} == {"yes"}
    # every figure now has a box on its page: the audit view's blue boxes
    boxes = document_boxes(run.docs, run.red)
    assert set(boxes) == {1, 2} and all(b[4] == "blue" for b in boxes[1] + boxes[2])
    assert inv.fields["doc_date"].raw == "02 Oct 2026"          # one value read across three words


def test_disagreement_is_marked_red_and_needs_a_look_before_saving(sagara_pdf, profiles, tmp_path):
    read = copy.deepcopy(LLM_READ)
    read[1]["total"] = "30.469.000"
    read[1]["lines"][1]["qty"] = "3"
    run = judge(sagara_pdf, profiles, read=read)
    assert run.savable and run.needs_look and not run.blocking
    assert run.disagreements == ["SDS/2026/10/031: total — draft reads 30,469,500, LLM read 30,469,000",
                                 "SDS/2026/10/031: line 2 qty — draft reads 2, LLM read 3"]
    inv = run.docs[0]
    assert {(id(inv), "total"), (id(inv.lines[1]), "qty")} <= run.red
    reds = [b for b in document_boxes(run.docs, run.red)[1] if b[4] == "red"]
    assert len(reds) == 2
    draft = generate_profile(FakeLLM(profile=SAGARA), sagara_pdf, [1, 2], profiles,
                             [d for d in llm_batch(sagara_pdf, profiles, read)[0] if set(d.pages) <= {1, 2}], [ERP_NAME])
    with pytest.raises(ValueError, match="not confirmed looking"):
        save_profile(draft, tmp_path, "Rina")


def test_date_not_understood_blocks_and_wrong_type_order_disagrees(sagara_pdf, profiles):
    bad = copy.deepcopy(SAGARA)
    bad["layout"]["header_fields"]["doc_date"]["pattern"] = r"\d{2}"
    bad["layout"]["doc_types"] = {"INVOICE": ["INVOICE"], "CREDIT_NOTE": ["CREDIT NOTE"]}   # the CN page says "invoice" too
    run = judge(sagara_pdf, profiles, data=bad)
    assert "SDS/2026/10/031: date '02' is not understood" in run.blocking and not run.savable
    assert "SDS/CN/2026/10/005: doc_type — draft reads INVOICE, LLM read CREDIT_NOTE" in run.disagreements


def test_invalid_draft_is_blocked_not_raised(sagara_pdf, profiles):
    no_doc_no = copy.deepcopy(SAGARA)
    del no_doc_no["layout"]["header_fields"]["doc_no"]
    run = judge(sagara_pdf, profiles, data=no_doc_no)
    assert not run.savable and run.blocking[0].startswith("not a valid profile: layout:")
    assert judge(sagara_pdf, profiles, data=["not", "a", "profile"]).blocking == [
        "not a valid profile: the draft is not a JSON object"]


def test_detect_text_another_suppliers_page_prints_is_blocked(sagara_pdf, profiles):
    generic = {**SAGARA, "detect": ["TAX INVOICE"]}
    run = judge(sagara_pdf, profiles, data=generic)
    assert "detect text ['TAX INVOICE'] is also printed on p.3, which another profile reads" in run.blocking


def test_missing_erp_name_and_existing_supplier_are_blocked(sagara_pdf, profiles):
    assert any("no supplier name from the ERP export" in m
               for m in judge(sagara_pdf, profiles, data={**SAGARA, "erp_vendor_names": []}).blocking)
    prima = {**SAGARA, "name": profiles[0].name}
    assert any("already exists (prima_kertas.json)" in m for m in judge(sagara_pdf, profiles, data=prima).blocking)


def test_generate_revises_once_with_the_problems_and_keeps_the_reviewers_erp_names(sagara_pdf, profiles):
    bad = copy.deepcopy(SAGARA)
    bad["layout"]["header_fields"]["doc_date"]["pattern"] = r"\d{2}"
    good = {**SAGARA, "erp_vendor_names": ["SOMETHING THE MODEL MADE UP"], "file": "x.json"}
    llm = FakeLLM(profile=lambda n: bad if n == 1 else good)
    docs, _ = llm_batch(sagara_pdf, profiles)
    draft = generate_profile(llm, sagara_pdf, [1, 2], profiles, docs, [ERP_NAME])
    assert [a["blocking"] for a in draft.attempts] == [2, 0] and draft.check.savable
    assert draft.check.profile.erp_vendor_names == [ERP_NAME]
    first, second = llm.profile_requests
    assert "Example profile (another supplier)" in first and "PRIMA KERTAS INDONESIA" in first
    assert "Sample page 1:" in first and "Sample page 2:" in first and "SDS/CN/2026/10/005" in first   # the credit note too
    assert "Example profile" not in second and "Your earlier draft" in second     # the draft shows the format now
    assert "date '02' is not understood (and 1 more like it)" in second            # one line per kind of problem
    assert "erp_vendor_names" not in second


def test_a_worse_revision_does_not_replace_the_better_draft(sagara_pdf, profiles):
    read = copy.deepcopy(LLM_READ)
    read[1]["total"] = "30.469.000"            # the LLM misread: the good draft still "disagrees" once
    worse = copy.deepcopy(SAGARA)
    worse["layout"]["header_fields"]["doc_date"]["pattern"] = r"\d{2}"
    docs = [d for d in llm_batch(sagara_pdf, profiles, read)[0] if set(d.pages) <= {1, 2}]
    draft = generate_profile(FakeLLM(profile=lambda n: SAGARA if n == 1 else worse), sagara_pdf, [1, 2], profiles,
                             docs, [ERP_NAME], attempts=2)
    assert len(draft.attempts) == 2 and draft.attempts[1]["blocking"] == 2
    assert draft.check.savable and draft.data["layout"]["header_fields"]["doc_date"]["pattern"] != r"\d{2}"


def test_good_first_draft_is_not_sent_back(sagara_pdf, profiles):
    llm = FakeLLM(profile=SAGARA)
    draft = generate_profile(llm, sagara_pdf, [1, 2], profiles, llm_batch(sagara_pdf, profiles)[0], [ERP_NAME])
    assert len(draft.attempts) == 1 and llm.calls.count("profile draft") == 1


def test_reviewer_edit_is_rechecked(sagara_pdf, profiles):
    docs = llm_batch(sagara_pdf, profiles)[0]
    draft = generate_profile(FakeLLM(profile=SAGARA), sagara_pdf, [1, 2], profiles, docs, [ERP_NAME])
    broken = recheck(draft, "{not json", sagara_pdf, profiles, docs)
    assert broken.edited and broken.check.blocking[0].startswith("not valid JSON")
    edited = json.loads(draft.text)
    edited["layout"]["header_fields"]["doc_date"]["pattern"] = r"\d{2}"
    again = recheck(draft, json.dumps(edited), sagara_pdf, profiles, docs)
    assert again.edited and not again.check.savable


def test_confirmed_draft_is_saved_and_the_next_run_reads_the_layout_with_boxes(sagara_pdf, profiles, config_dir, tmp_path):
    conf = tmp_path / "config"
    shutil.copytree(config_dir, conf)
    docs = llm_batch(sagara_pdf, profiles)[0]
    draft = generate_profile(FakeLLM(profile=SAGARA, text_model="fake-text"), sagara_pdf, [1, 2], profiles, docs, [ERP_NAME])
    with pytest.raises(ValueError, match="reviewer's name"):
        save_profile(draft, conf, "  ")
    path = save_profile(draft, conf, "Rina", now=datetime(2026, 11, 2, 9, 30))
    assert path == conf / "suppliers" / "sagara_digital_supply.json"
    body = json.loads(path.read_text())
    assert body["_comment"].startswith("Drafted by fake-text from Supplier_Invoices_PT_Sagara.pdf p.1-2")
    assert body["_comment"].endswith("confirmed by Rina on 2026-11-02 09:30.")
    assert "file" not in body and body["erp_vendor_names"] == [ERP_NAME]

    reread, logs = extract_pdf(sagara_pdf, load_profiles(conf))       # no LLM: the profile alone
    assert [lg.profile for lg in logs] == ["PT Sagara Digital Supply", "PT Sagara Digital Supply", "PT Prima Kertas Indonesia"]
    assert reread[0].fields["total"].src.bbox and reread[0].confidence == 0.99
    with pytest.raises(FileExistsError):
        save_profile(draft, conf, "Rina")


def test_saved_supplier_is_registered_once_and_held_for_review(config_dir, tmp_path):
    conf = tmp_path / "config"
    shutil.copytree(config_dir, conf)
    reg_file = conf / "supplier_register.json"       # start from a register without Sagara, whatever the live one holds
    data = json.loads(reg_file.read_text())
    data["suppliers"].pop(ERP_NAME, None)
    reg_file.write_text(json.dumps(data))
    before = load_register(conf)
    assert before.review_reason(ERP_NAME, date(2026, 11, 10)).endswith("treated as a new supplier.")

    assert register_supplier(conf, ["PT Sagara Digital Supply", ERP_NAME], date(2026, 11, 3)) == ["PT Sagara Digital Supply"]
    reg = load_register(conf)                    # the file still loads, the old entry and the comment are kept
    assert "_comment" in json.loads((conf / "supplier_register.json").read_text())
    assert reg.review_reason("PT PRIMA KERTAS INDONESIA", date(2026, 11, 3)) == ""
    assert "went live on 03 Nov 2026" in reg.review_reason(ERP_NAME, date(2026, 11, 10))
    assert reg.review_reason(ERP_NAME, date(2026, 12, 4)) == ""

    # registered already: the date its new-supplier period started stays
    assert register_supplier(conf, [ERP_NAME], date(2027, 1, 1)) == []
    assert "went live on 03 Nov 2026" in load_register(conf).review_reason(ERP_NAME, date(2026, 11, 10))


def test_blocked_draft_cannot_be_saved(sagara_pdf, profiles, tmp_path):
    docs = llm_batch(sagara_pdf, profiles)[0]
    draft = generate_profile(FakeLLM(profile={"name": "x"}), sagara_pdf, [1, 2], profiles, docs, [ERP_NAME], attempts=1)
    with pytest.raises(ValueError, match="blocking problems"):
        save_profile(draft, tmp_path, "Rina", looked=True)
    assert not (tmp_path / "suppliers").exists()


def test_groq_profile_draft_call():
    llm = GroqLLM(LLMConfig(), api_key="test-key")
    llm._client = FakeGroqClient('{"name": "PT X"}')
    data, info = llm.draft_profile("Sample page 1:\nPT X INVOICE")
    req = llm._client.requests[0]
    assert data == {"name": "PT X"} and info["purpose"] == "profile draft"
    assert req["messages"][0] == {"role": "system", "content": PROFILE_PROMPT}
    assert req["response_format"] == {"type": "json_object"}


# --------------------------------------------------------------------------- what the request costs

def test_samples_are_one_page_per_document_type(sagara_pdf, profiles):
    docs = llm_batch(sagara_pdf, profiles)[0]
    assert sample_pages([1, 2], docs) == [1, 2]                     # an invoice and the credit note
    assert sample_pages([1, 2], [d for d in docs if d.doc_type == "INVOICE"] * 5) == [1]
    assert sample_pages([4, 5, 6], []) == [4]                       # no LLM read: the first page alone


def test_sample_text_leaves_out_repeated_rows_and_extra_item_rows(tmp_path):
    path = tmp_path / "long.pdf"
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page()
        page.insert_text((50, 72), "PT SAGARA DIGITAL SUPPLY", fontsize=8)
        page.insert_text((50, 90), "# ITEM QTY UNIT PRICE DISC TOTAL", fontsize=8)
        for i in range(1, 8):
            page.insert_text((50, 90 + 14 * i), f"{i} SKU-{i} Item number {i} 2 unit 1.000 0% 2.000", fontsize=8)
        page.insert_text((50, 220), "Subtotal Rp 14.000", fontsize=8)
    doc.save(path)
    text = sample_text(path, [1, 2])
    first, second = text.split("Sample page 2:")
    assert "3 SKU-3" in first and "4 SKU-4" not in first
    assert "… 4 more item row(s) of the same shape\nSubtotal" in first
    assert "PT SAGARA" not in second and "row(s) identical to an earlier sample page left out" in second


def test_example_profile_is_compact(profiles):
    text = example_text(profiles)
    assert "\n " not in text and '"page_height"' not in text and '"optional":false' not in text
    assert '"labels":["No. Faktur / Invoice No."]' in text           # one label per field
    assert example_text([]) == ""


def test_reply_cut_off_at_the_token_limit_is_not_sent_back(sagara_pdf, profiles):
    class CutOff:
        available = True

        def draft_profile(self, request):
            return {}, {"model": "m", "finish_reason": "length"}

    draft = generate_profile(CutOff(), sagara_pdf, [1, 2], profiles, llm_batch(sagara_pdf, profiles)[0], [ERP_NAME])
    assert len(draft.attempts) == 1
    assert draft.check.blocking[0].startswith("the reply was cut off at the completion-token limit")


def test_schema_is_checked_before_the_pdf_is_read(sagara_pdf, profiles, monkeypatch):
    def no_extraction(*a, **k):
        raise AssertionError("an invalid draft must not reach the extractor")
    monkeypatch.setattr("recon.profile_draft.extract_pdf", no_extraction)
    run = dry_run({**SAGARA, "layout": {"doc_types": {}}}, sagara_pdf, [1, 2], profiles, [])
    assert not run.savable and all(m.startswith("not a valid profile") for m in run.blocking)


def test_groq_profile_draft_has_a_completion_budget():
    llm = GroqLLM(LLMConfig(), api_key="test-key", text_model="t", fallback_text_model="")
    llm._client = FakeGroqClient('{"name": "PT X"}')
    _, info = llm.draft_profile("request")
    assert llm._client.requests[0]["max_completion_tokens"] == LLMConfig().profile_max_completion_tokens == 4096
    assert "finish_reason" in info
    llm.extract_page_text("page")
    assert "max_completion_tokens" not in llm._client.requests[1]          # only the draft is capped


# --------------------------------------------------------------------------- "no line items": which key to change

def _table(**table):
    d = copy.deepcopy(SAGARA)
    d["layout"]["table"].update(table)
    return d


def test_heading_keywords_of_both_document_types_are_named(sagara_pdf, profiles):
    # the draft that blocked in the app: each heading row lacks the other type's last column
    run = judge(sagara_pdf, profiles, data=_table(header_keywords=["ITEM", "QTY", "DISC.", "LINE TOTAL", "CREDIT AMOUNT"]))
    inv, cn = run.blocking
    assert inv.startswith("SDS/2026/10/031: no line items extracted: the table heading row was not found")
    assert "lacks ['CREDIT AMOUNT']" in inv and "lacks ['LINE TOTAL']" in cn


def test_stop_marker_printed_in_the_item_rows_is_named(sagara_pdf, profiles):
    run = judge(sagara_pdf, profiles, data=_table(stop_markers=["unit"]))
    assert run.blocking[0].endswith("the table ends above its first item row, at stop marker 'unit' on "
                                    "'1 LAP-14I Laptop Business 2 unit 12.500.000 5% 23.750.000'")


def test_sku_header_not_printed_blocks_even_when_the_guess_reads_the_rows(sagara_pdf, profiles):
    run = judge(sagara_pdf, profiles, data=_table(sku_header="SKU"))
    assert run.docs[0].lines                                       # the fallback guess happened to work here
    assert "p.1: sku_header 'SKU' is not a word of the heading row" in run.blocking[0]


def test_line_number_column_as_sku_header_is_named(sagara_pdf, profiles):
    from recon.config import SupplierProfile
    from recon.extract.profile_parser import table_problem
    from recon.extract.words import text_layer_words
    prof = SupplierProfile.model_validate(SAGARA)
    hash_col = prof.model_copy(update={"layout": prof.layout.model_copy(update={
        "table": prof.layout.table.model_copy(update={"sku_header": "#"})})})     # past the schema, as an old file could be
    words = text_layer_words(pymupdf.open(sagara_pdf)[0])
    assert table_problem(words, prof) == ""
    assert table_problem(words, hash_col).startswith("no line numbers left of the sku_header column '#'")


def test_a_pattern_missing_the_credit_note_numbering_names_the_value(sagara_pdf, profiles):
    d = copy.deepcopy(SAGARA)
    d["layout"]["header_fields"]["doc_no"]["pattern"] = r"[A-Z]{3}/\d{4}/\d{2}/\d{3}"            # no room for /CN/
    d["layout"]["table"]["header_keywords"] = ["ITEM", "QTY", "LINE TOTAL"]                       # invoice-only column
    run = judge(sagara_pdf, profiles, data=d)
    assert any(m.startswith("header_fields.doc_no: the pattern") and "'SDS/CN/2026/10/005' as printed" in m
               for m in run.blocking)
    # the credit note's table problem shows in the same round, though the draft never found that document
    assert any(m.startswith("p.2: the table heading row was not found") and "lacks ['LINE TOTAL']" in m
               for m in run.blocking)


def test_the_value_is_searched_after_its_label(sagara_pdf, profiles):
    # the live draft's loose pattern: from the label's start it read 'PO' (of 'PO Reference:') as the PO number
    d = copy.deepcopy(SAGARA)
    d["layout"]["header_fields"]["po_no"]["pattern"] = "[A-Z0-9-]+"
    run = judge(sagara_pdf, profiles, data=d)
    assert run.docs[0].po_no == "NRG-IT-2610-031" and run.savable and not run.disagreements


def test_a_label_that_is_itself_a_value_is_named(sagara_pdf, profiles):
    d = copy.deepcopy(SAGARA)
    d["layout"]["header_fields"]["doc_no"]["labels"] = ["SDS/2026/10/031"]
    run = judge(sagara_pdf, profiles, data=d)
    assert run.blocking[0].startswith("header_fields.doc_no: label 'SDS/2026/10/031' is itself a doc_no value")


def test_a_wrong_decimal_separator_is_blocked_with_the_fix(sagara_pdf, profiles):
    run = judge(sagara_pdf, profiles, data={**SAGARA, "decimal_separator": "."})
    assert run.blocking[0].startswith('decimal_separator is "." but the pages group thousands with dots')
    assert run.blocking[0].endswith('use ","')


def test_draft_failing_a_check_the_llm_read_passes_blocks(sagara_pdf, profiles):
    d = copy.deepcopy(SAGARA)
    f = d["layout"]["table"]["right_fields"]
    f[2], f[4] = f[4], f[2]                       # unit_price and amount swapped: every line fails R1 as read
    run = judge(sagara_pdf, profiles, data=d)
    assert any(m.startswith("SDS/2026/10/031: R1 line 1 LAP-14I does not add up as the draft reads it")
               and m.endswith("but does as the LLM read it: the draft misreads it") for m in run.blocking)
    assert not run.savable


def test_a_check_both_reads_fail_is_the_suppliers_error_and_only_needs_a_look(sagara_pdf, profiles):
    read = copy.deepcopy(LLM_READ)
    read[1]["subtotal"] = "27.450.001"            # both readers agree with the rules except the printed subtotal
    run = judge(sagara_pdf, profiles, read=read)
    assert run.savable and not any("R2" in m for m in run.blocking)


def test_a_revision_sends_only_its_changes_and_keeps_the_rest(sagara_pdf, profiles):
    bad = copy.deepcopy(SAGARA)
    bad["layout"]["header_fields"]["doc_date"]["pattern"] = r"\d{2}"
    fix = {"layout": {"header_fields": {"doc_date": {"pattern": r"\d{1,2} [A-Za-z]{3} \d{4}"}}}}      # a patch, not a profile
    llm = FakeLLM(profile=lambda n: bad if n == 1 else fix)
    draft = generate_profile(llm, sagara_pdf, [1, 2], profiles, llm_batch(sagara_pdf, profiles)[0], [ERP_NAME])
    assert draft.check.savable and len(draft.attempts) == 2
    assert draft.data["layout"]["table"] == SAGARA["layout"]["table"]          # untouched keys kept from the draft
    assert "Reply with ONLY the keys you change" in llm.profile_requests[1]


def test_merge_draft():
    from recon.profile_draft import merge_draft
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    assert merge_draft(base, {"a": {"c": [3]}, "d": None, "e": 2}) == {"a": {"b": 1, "c": [3]}, "e": 2}
    assert base == {"a": {"b": 1, "c": [1, 2]}, "d": 1}                        # the base is not changed


def test_an_invalid_revision_is_logged_as_invalid_and_loses(sagara_pdf, profiles):
    read = copy.deepcopy(LLM_READ)
    read[1]["total"] = "30.469.000"                                             # keeps the good draft revising
    docs = [d for d in llm_batch(sagara_pdf, profiles, read)[0] if set(d.pages) <= {1, 2}]
    llm = FakeLLM(profile=lambda n: SAGARA if n == 1 else {"layout": {"table": {"sku_header": "#"}}})
    draft = generate_profile(llm, sagara_pdf, [1, 2], profiles, docs, [ERP_NAME], attempts=2)
    assert [a["valid"] for a in draft.attempts] == [True, False]
    assert draft.check.savable and draft.data["layout"]["table"]["sku_header"] == "ITEM"


def test_a_reply_groq_rejects_as_invalid_json_is_one_failed_attempt(sagara_pdf, profiles):
    import groq
    import httpx
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    bad_json = groq.BadRequestError("Failed to validate JSON", response=httpx.Response(400, request=req),
                                    body={"error": {"code": "json_validate_failed"}})
    llm = GroqLLM(LLMConfig(), api_key="test-key", text_model="t", fallback_text_model="")
    llm._client = FakeGroqClient("{}", fail={"t": bad_json})
    reply, info = llm.draft_profile("request")
    assert reply == {} and info["finish_reason"] == "invalid_json"

    drafts = FakeLLM(profile=lambda n: {} if n == 1 else SAGARA)
    first = drafts.draft_profile
    drafts.draft_profile = lambda r: (first(r)[0], {"model": "m", "finish_reason": "invalid_json"}) \
        if not drafts.profile_requests else first(r)
    draft = generate_profile(drafts, sagara_pdf, [1, 2], profiles, llm_batch(sagara_pdf, profiles)[0], [ERP_NAME])
    assert draft.check.savable and len(draft.attempts) == 2
    assert "Example profile" in drafts.profile_requests[1]      # nothing to revise: asked afresh
