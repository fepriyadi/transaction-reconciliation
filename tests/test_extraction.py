"""Parser tests on synthetic positioned words (no PDF needed)."""
from fractions import Fraction

from recon.config import RightField, load_profiles
from recon.extract.assemble import assemble
from recon.extract.llm_reader import parse_page_from_llm
from recon.extract.profile_parser import match_right_grammar, parse_page_with_profile
from recon.extract.words import Word, deskew
from recon.llm import unsupported_numbers

SPEC = [RightField(name="qty", type="number"), RightField(name="uom", type="alpha"),
        RightField(name="unit_price", type="number"), RightField(name="disc_pct", type="number", optional=True),
        RightField(name="amount", type="number")]


def words(texts, y=100.0, x=50.0):
    out = []
    for t in texts:
        out.append(Word(t, x, y, x + 6 * len(t), y + 9))
        x += 6 * len(t) + 6
    return out


def test_row_grammar_with_discount():
    m, n = match_right_grammar(words(["1", "LKB-48", "Lakban", "48", "mm", "99", "roll", "13.900", "10", "1.238.490"]), SPEC)
    assert m["qty"].text == "99" and m["disc_pct"].text == "10" and m["amount"].text == "1.238.490" and n == 5


def test_row_grammar_without_discount_and_numeric_description():
    m, n = match_right_grammar(words(["4", "AMP-110", "Amplop", "isi", "100", "60", "box", "23.750", "1.425.000"]), SPEC)
    assert m["qty"].text == "60" and "disc_pct" not in m and m["unit_price"].text == "23.750" and n == 4


def test_row_grammar_reads_percent_discount():
    spec = [f.model_copy(update={"type": "percent"}) if f.name == "disc_pct" else f for f in SPEC]
    m, n = match_right_grammar(words(["1", "LAP-14I", "Laptop", "2", "unit", "12.500.000", "5%", "23.750.000"]), spec)
    assert m["disc_pct"].text == "5%" and m["unit_price"].text == "12.500.000" and n == 5


def test_llm_percent_discount_is_read_not_zeroed():
    pp = parse_page_from_llm({"doc_no": "SDS/1", "lines": [
        {"line_no": 1, "sku": "LAP-14I", "qty": "2", "unit_price": "12.500.000", "disc_pct": "5%", "amount": "23.750.000"},
        {"line_no": 2, "sku": "SSD-1TB", "qty": "4", "unit_price": "1.450.000", "disc_pct": "2,5%", "amount": "5.655.000"},
        {"line_no": 3, "sku": "MON-24F", "qty": "1", "unit_price": "1.850.000", "disc_pct": None, "amount": "1.850.000"}]},
        1, "t.pdf", "llm_text")
    assert [l.disc_pct for l in pp.lines] == [5, Fraction(5, 2), 0] and not pp.notes


def test_llm_unreadable_discount_is_flagged_not_zeroed():
    pp = parse_page_from_llm({"doc_no": "SDS/1", "lines": [
        {"line_no": 1, "sku": "LAP-14I", "qty": "2", "unit_price": "12.500.000", "disc_pct": "5 pct", "amount": "23.750.000"}]},
        1, "t.pdf", "llm_text")
    assert pp.lines == [] and pp.notes[0].startswith("EXTRACTION_INCOMPLETE")


def test_deskew_straightens_rows():
    # one row drifting 8pt over 370pt, as on the scanned September page
    ws = [Word(t, x, 100 + x * 0.021, x + 20, 109 + x * 0.021) for t, x in
          [("1", 100), ("MAP", 120), ("11", 320), ("pak", 336), ("36.000", 390), ("396.000", 470)]]
    out = deskew(ws, [(1, 1, 1)] * len(ws))
    assert max(w.yc for w in out) - min(w.yc for w in out) < 1.0
    assert out[0].bbox == ws[0].bbox     # provenance keeps the original page coordinates


def _page(config_dir, rows):
    ws = []
    for y, texts, x in rows:
        ws += words(texts, y=y, x=x)
    prof = load_profiles(config_dir)[0]
    return parse_page_with_profile(ws, prof, 1, "t.pdf", "text_layer")


def test_profile_parse_synthetic_page(config_dir):
    pp = _page(config_dir, [
        (40, ["PT", "PRIMA", "KERTAS", "INDONESIA"], 50), (60, ["FAKTUR", "/", "INVOICE"], 400),
        (120, ["No.", "Faktur", "/", "Invoice", "No.", "PKI/INV/2026/10/0201"], 310),
        (138, ["Tanggal", "/", "Date", "02-10-2026"], 310),
        (156, ["No.", "PO", "PO-90001"], 310),
        (210, ["#", "Kode", "/", "SKU", "Uraian", "Qty", "Sat.", "Harga", "Disc", "%", "Jumlah"], 55),
        (240, ["1", "LKB-48", "Lakban", "coklat", "99", "roll", "13.900", "10", "1.238.490"], 57),
        (258, ["2", "KRD-ARS", "Kardus", "arsip", "13", "pak", "112.500", "1.462.500"], 57),
        (290, ["Subtotal", "2.700.990"], 330),
        (308, ["DPP", "Nilai", "Lain", "(11/12)", "2.475.908"], 330),
        (326, ["PPN", "12%", "297.109"], 330),
        (344, ["TOTAL", "(Rp)", "2.998.099"], 330),
    ])
    assert pp.doc_no == "PKI/INV/2026/10/0201" and pp.header["po_no"].value == "PO-90001"
    assert pp.doc_type == "INVOICE"
    assert [(l.sku, l.qty, l.disc_pct, l.amount) for l in pp.lines] == [
        ("LKB-48", 99, 10, 1238490), ("KRD-ARS", 13, 0, 1462500)]
    assert pp.totals["dpp"].value == 2475908 and pp.totals_labels["dpp"].startswith("DPP Nilai")
    assert pp.lines[0].fields["amount"].src.page == 1 and pp.lines[0].fields["amount"].src.bbox


def test_profile_parse_reads_percent_discount(config_dir):
    pp = _page(config_dir, [
        (40, ["PT", "PRIMA", "KERTAS", "INDONESIA"], 50), (60, ["FAKTUR", "/", "INVOICE"], 400),
        (120, ["No.", "Faktur", "/", "Invoice", "No.", "PKI/INV/2026/10/0202"], 310),
        (210, ["#", "Kode", "/", "SKU", "Uraian", "Qty", "Sat.", "Harga", "Disc", "%", "Jumlah"], 55),
        (240, ["1", "LKB-48", "Lakban", "coklat", "99", "roll", "13.900", "10%", "1.238.490"], 57),
        (290, ["Subtotal", "1.238.490"], 330),
    ])
    assert [(l.sku, l.disc_pct, l.amount) for l in pp.lines] == [("LKB-48", 10, 1238490)] and not pp.notes


def test_multi_page_documents_are_stitched_and_cn_sign_flipped(config_dir):
    p1 = parse_page_from_llm({"doc_type": "CREDIT_NOTE", "doc_no": "CN/1", "doc_date": "15-09-2026",
                              "lines": [{"line_no": 1, "sku": "A", "qty": "15", "unit_price": "44.000", "amount": "660.000"}]},
                             1, "t.pdf", "llm_text")
    p2 = parse_page_from_llm({"doc_no": None, "lines": [{"line_no": 2, "sku": "B", "qty": "6", "unit_price": "36.000",
                                                          "amount": "216.000"}],
                              "subtotal": "876.000", "dpp": "803.000", "ppn": "96.360", "total": "972.360"},
                             2, "t.pdf", "llm_text")
    docs, logs = assemble([p1, p2], "t.pdf")
    assert len(docs) == 1 and docs[0].pages == [1, 2] and [l.role for l in logs] == ["start", "continuation"]
    d = docs[0]
    assert d.total == -972360 and d.lines[0].qty == -15 and d.lines[0].amount == -660000


def test_unassigned_page_is_logged_not_dropped():
    empty = parse_page_from_llm({}, 1, "t.pdf", "llm_text")
    docs, logs = assemble([empty], "t.pdf")
    assert docs == [] and logs[0].role == "unassigned"


def test_llm_summary_guard_rejects_invented_numbers():
    facts = {"difference": "-Rp 3,256,673", "dup": "Rp 17,238,178", "docs": 15}
    assert unsupported_numbers("The difference of -Rp 3,256,673 is explained; Rp 17,238,178 duplicate.", facts) == set()
    assert unsupported_numbers("About Rp 17.2 million is at risk.", facts) == {"17.2~scaled"}
    assert unsupported_numbers("About Rp 17 million is at risk.", facts) == {"17~scaled"}
    assert unsupported_numbers("We saved Rp 20,000,000.", facts) == {"20000000"}


def test_header_value_glued_to_its_label_by_ocr_is_still_found(config_dir):
    pp = _page(config_dir, [
        (40, ["PT", "PRIMA", "KERTAS", "INDONESIA"], 50), (60, ["FAKTUR", "/", "INVOICE"], 400),
        (120, ["No.", "Faktur", "/", "Invoice", "No.PKI/INV/2026/10/0203"], 310),
        (138, ["Tanggal/Date02-10-2026"], 310),
    ])
    assert pp.doc_no == "PKI/INV/2026/10/0203" and pp.header["doc_date"].value == "02-10-2026"
