"""ERP AP export (xlsx) -> Documents.

Reads every sheet. Each sheet is classified by its header row (using the
synonyms in config/erp_mapping.json), so renamed/reordered sheets or columns
need no code change. Every value keeps its sheet!cell reference, and values
stored as text ('15,619,088', '09/23/2026') are normalised and logged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Union

import openpyxl
from openpyxl.utils import get_column_letter

from .config import ErpMapping, load_erp_mapping
from .models import Document, Field, Line, SourceRef
from .normalize import is_blank, parse_date, parse_int_amount, parse_number, parse_percent


@dataclass
class ErpLoad:
    documents: list = field(default_factory=list)
    sheets: list = field(default_factory=list)          # (sheet, role, rows)
    metadata: dict = field(default_factory=dict)        # from info sheets
    notes: list = field(default_factory=list)           # normalisation log
    orphan_lines: list = field(default_factory=list)    # lines whose entry has no header


def _c(s) -> str:
    return re.sub(r"[^a-z0-9%]", "", str(s or "").lower())


def _map_header(row_vals: list, synonyms: dict) -> dict:
    """field -> column index, using exact (compacted) synonym matches."""
    out = {}
    comp = [_c(v) for v in row_vals]
    for fname, syns in synonyms.items():
        for s in syns:
            if _c(s) in comp:
                out[fname] = comp.index(_c(s))
                break
    return out


def load_erp(path: Path, mapping: Union[ErpMapping, Path]) -> ErpLoad:
    """`mapping`: the validated ERP mapping, or a config directory to load it from."""
    path = Path(path)
    cfg = mapping if isinstance(mapping, ErpMapping) else load_erp_mapping(Path(mapping))
    hint = cfg.decimal_separator
    date_order = cfg.text_date_order
    wb = openpyxl.load_workbook(path, data_only=True)
    res = ErpLoad()
    header_sheets, line_sheets = [], []

    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        role, hdr_row, cols = "info", 0, {}
        for i, r in enumerate(rows[:10]):
            heads = list(r)
            lc = _map_header(heads, cfg.line_fields)
            hc = _map_header(heads, cfg.header_fields)
            if {"entry_id", "sku", "qty", "amount"} <= lc.keys():
                role, hdr_row, cols = "lines", i, lc
                break
            if {"doc_no", "total"} <= hc.keys():
                is_cn = any(_c(m) in [_c(v) for v in heads] for m in cfg.credit_note_markers)
                role, hdr_row, cols = ("credit_notes" if is_cn else "invoices"), i, hc
                break
        res.sheets.append((ws.title, role, max(0, len(rows) - 1)))
        if role == "info":
            for r in rows:
                if r and r[0] is not None:
                    res.metadata[f"{ws.title}: {r[0]}"] = " ".join(str(v) for v in r[1:] if v is not None)
        elif role == "lines":
            line_sheets.append((ws, rows, hdr_row, cols))
        else:
            header_sheets.append((ws, rows, hdr_row, cols, role))

    by_entry: dict = {}
    for ws, rows, hdr_row, cols, role in header_sheets:
        for ri in range(hdr_row + 1, len(rows)):
            r = rows[ri]
            if not r or all(v is None for v in r):
                continue
            excel_row = ri + 1

            def cell(fname):
                if fname not in cols:
                    return None, None
                ci = cols[fname]
                ref = SourceRef(file=path.name, sheet=ws.title, row=excel_row,
                                cell=f"{get_column_letter(ci + 1)}{excel_row}", method="erp")
                return (r[ci] if ci < len(r) else None), ref

            text_notes = []

            def amount(fname):
                raw, ref = cell(fname)
                if raw is None:
                    return None, None
                v = parse_int_amount(raw, hint)
                if isinstance(raw, str):
                    text_notes.append(f"{ref.label()}: amount stored as text '{raw}' -> {v:,}")
                return v, Field(v, str(raw), ref)

            entry, eref = cell("entry_id")
            docno, dref = cell("doc_no")
            draw, daref = cell("doc_date")
            dval, amb = parse_date(draw, date_order)
            if isinstance(draw, str):
                text_notes.append(f"{daref.label()}: date stored as text '{draw}' -> {dval}"
                                  + (" (ambiguous day/month)" if amb else ""))
            vendor, vref = cell("vendor")
            po, pref = cell("po_no")
            refinv, rref = cell("reference")
            status, sref = cell("doc_status")
            doc = Document(side="ERP", doc_type="CREDIT_NOTE" if role == "credit_notes" else "INVOICE",
                           doc_no=str(docno or "").strip(), doc_date=dval, vendor=str(vendor or ""),
                           po_no=str(po or ""), reference=str(refinv or ""), entry_id=str(entry or ""),
                           status_in_source=str(status or ""), method="erp", confidence=1.0)
            doc.fields.update({
                "entry_id": Field(doc.entry_id, str(entry), eref),
                "doc_no": Field(doc.doc_no, str(docno), dref),
                "doc_date": Field(dval, str(draw), daref),
                "vendor": Field(doc.vendor, str(vendor), vref),
            })
            if pref:
                doc.fields["po_no"] = Field(doc.po_no, str(po), pref)
            if rref:
                doc.fields["reference"] = Field(doc.reference, str(refinv), rref)
            if sref:
                doc.fields["doc_status"] = Field(doc.status_in_source, str(status), sref)
            if amb:
                doc.fields["doc_date_ambiguous"] = Field(True, str(draw), daref)
            for k in ("subtotal", "dpp", "ppn", "total"):
                v, f = amount(k)
                setattr(doc, k, v)
                if f:
                    doc.fields[k] = f
            if doc.total is not None and doc.total < 0:
                doc.doc_type = "CREDIT_NOTE"
            doc.extraction_notes = text_notes
            res.notes.extend(text_notes)
            res.documents.append(doc)
            by_entry.setdefault(doc.entry_id, []).append(doc)

    for ws, rows, hdr_row, cols in line_sheets:
        for ri in range(hdr_row + 1, len(rows)):
            r = rows[ri]
            if not r or all(v is None for v in r):
                continue
            excel_row = ri + 1

            def get(fname):
                ci = cols.get(fname)
                if ci is None or ci >= len(r):
                    return None, None
                return r[ci], SourceRef(file=path.name, sheet=ws.title, row=excel_row,
                                        cell=f"{get_column_letter(ci + 1)}{excel_row}", method="erp")

            entry, _ = get("entry_id")
            vals, flds, unreadable = {}, {}, []
            for k in ("line_no", "sku", "description", "qty", "uom", "unit_price", "disc_pct", "amount"):
                raw, ref = get(k)
                if k == "disc_pct":
                    v = Fraction(0) if is_blank(raw) else parse_percent(raw, hint)
                    if v is None:
                        unreadable.append(f"EXTRACTION_INCOMPLETE: {ref.label()}: discount unreadable '{raw}'")
                elif k in ("qty", "unit_price"):
                    v = parse_number(raw, hint) if raw is not None else Fraction(0)
                elif k == "amount":
                    v = parse_int_amount(raw, hint)
                elif k == "line_no":
                    v = int(raw) if raw is not None else None
                else:
                    v = str(raw or "")
                vals[k] = v
                flds[k] = Field(v, "" if raw is None else str(raw), ref)
            ln = Line(vals["line_no"], vals["sku"], vals["description"], vals["qty"], vals["uom"],
                      vals["unit_price"], vals["disc_pct"] or Fraction(0), vals["amount"], flds)
            owners = by_entry.get(str(entry))
            if owners:
                owners[0].lines.append(ln)
                owners[0].extraction_notes.extend(unreadable)
            else:
                res.orphan_lines.append((str(entry), ln))
                res.notes.append(f"{ws.title}!row {excel_row}: line for unknown entry {entry}")
    return res
