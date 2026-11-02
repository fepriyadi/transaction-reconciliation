import openpyxl

from recon.erp_loader import load_erp


def test_percent_discount_text_is_read_and_unreadable_one_is_flagged(data_dir, config_dir, tmp_path):
    wb = openpyxl.load_workbook(data_dir / "ERP_AP_Export_Sep2026.xlsx")
    wb["AP_Lines"]["H2"] = "10%"       # E-10451 line 1 (10 as a number in the original)
    wb["AP_Lines"]["H3"] = "five"      # E-10451 line 2
    path = tmp_path / "erp.xlsx"
    wb.save(path)
    doc = next(d for d in load_erp(path, config_dir).documents if d.entry_id == "E-10451")
    assert doc.lines[0].disc_pct == 10
    assert any(n.startswith("EXTRACTION_INCOMPLETE") and "H3" in n for n in doc.extraction_notes)
