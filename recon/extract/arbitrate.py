"""Choosing between reads of a scanned page.

OCR is retried with median filters until the read adds up; an LLM vision read,
when available, is compared with the OCR read. A disagreement is resolved only
by the reads' own arithmetic (a reading decision, never a correction of the
document); otherwise it is flagged for a person.
"""
from __future__ import annotations

from typing import Optional

from ..config import SupplierProfile
from ..models import SourceRef
from .llm_reader import parse_page_from_llm
from .page import PageParse, page_self_consistent
from .profile_parser import page_profile, parse_page_with_profile
from .words import OCR_PREFILTERS, Word, ocr_words, page_png, page_text


def compare_parses(a: PageParse, b: PageParse) -> list[str]:
    """Field-level disagreements between two reads of the same page."""
    diffs = []
    for k in ("doc_no", "po_no", "doc_date", "reference"):
        va, vb = a.header.get(k), b.header.get(k)
        if (va.value if va else "") != (vb.value if vb else ""):
            diffs.append(f"{k}: '{va.value if va else ''}' vs '{vb.value if vb else ''}'")
    for k in ("subtotal", "dpp", "ppn", "total"):
        va, vb = a.totals.get(k), b.totals.get(k)
        if (va.value if va else None) != (vb.value if vb else None):
            diffs.append(f"{k}: {va.value if va else None} vs {vb.value if vb else None}")
    if len(a.lines) != len(b.lines):
        diffs.append(f"line count: {len(a.lines)} vs {len(b.lines)}")
    else:
        for la, lb in zip(a.lines, b.lines):
            for attr in ("qty", "unit_price", "disc_pct", "amount"):
                if getattr(la, attr) != getattr(lb, attr):
                    diffs.append(f"line {la.line_no} {attr}: {getattr(la, attr)} vs {getattr(lb, attr)}")
    return diffs


def best_ocr_read(page, profiles: list[SupplierProfile], prev_profile: Optional[SupplierProfile], page_no: int,
                  file: str) -> tuple[Optional[PageParse], list[Word], Optional[SupplierProfile], str]:
    """OCR the page, raw first, median-filtered only if the raw read doesn't add up.

    Returns (parse or None, words, profile or None, OCR error message)."""
    best = None
    words: list[Word] = []
    error = ""
    for median in OCR_PREFILTERS:
        try:
            words = ocr_words(page, median=median)
        except Exception as e:  # tesseract missing / failed
            words, error = [], f"OCR unavailable ({type(e).__name__}: {e})"
            break
        prof = page_profile(page_text(words), profiles, prev_profile)
        if not prof:
            continue
        cand = parse_page_with_profile(words, prof, page_no, file, "ocr", page.rect.height)
        score = (page_self_consistent(cand), bool(cand.doc_no), len(cand.lines), -len(cand.notes))
        if best is None or score > best[0]:
            best = (score, cand, words, prof)
        if score[0] and score[1]:
            break
    if best is None:
        return None, words, None, error
    _, pp, words, prof = best
    return pp, words, prof, error


def second_read_scanned(ocr_pp: PageParse, page, page_no: int, file: str, llm, profile: SupplierProfile) -> PageParse:
    """Scanned page: compare OCR read with an LLM vision read.

    Agreement -> high confidence. Disagreement -> keep the read whose own
    arithmetic is consistent (that's a reading decision, not a correction of the
    document); if both or neither are consistent, keep OCR and flag the conflict.
    """
    try:
        data, usage = llm.extract_page_image(page_png(page, 150))
    except Exception as e:  # network / quota: fall back to OCR only
        ocr_pp.notes.append(f"LLM vision read failed ({type(e).__name__}); OCR only")
        return ocr_pp
    vis = parse_page_from_llm(data, page_no, file, "llm_vision", profile.vendor_name)
    vis.llm_usage = usage
    diffs = compare_parses(ocr_pp, vis)
    ocr_ok, vis_ok = page_self_consistent(ocr_pp), page_self_consistent(vis)
    if not diffs:
        ocr_pp.method = "ocr+llm_vision"
        ocr_pp.llm_usage = usage
        all_fields = list(ocr_pp.header.values()) + list(ocr_pp.totals.values())
        all_fields += [f for l in ocr_pp.lines for f in l.fields.values()]
        for f in all_fields:
            if f.src:
                f.src = SourceRef(f.src.file, f.src.page, f.src.bbox, method="ocr+llm_vision")
        return ocr_pp
    if vis_ok and not ocr_ok:
        vis.notes = [f"OCR read inconsistent; vision read used. OCR vs vision: {'; '.join(diffs[:5])}"]
        vis.doc_type = vis.doc_type or ocr_pp.doc_type
        return vis
    ocr_pp.llm_usage = usage
    ocr_pp.notes.append("EXTRACTION_CONFLICT: OCR and vision reads disagree: " + "; ".join(diffs[:5]))
    return ocr_pp
