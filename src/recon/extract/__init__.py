"""PDF -> structured documents.

Pipeline per page:
  1. Get positioned words: text layer if present, otherwise OCR (tesseract).   words.py
  2. Detect the supplier profile (config/suppliers/*.json) from page text;
     continuation pages inherit the previous page's profile.                   profile_parser.py
  3. Parse with the profile's layout spec (labels + row grammar) — plain code.  profile_parser.py
  4. Scanned pages: if an LLM is configured, a vision read is compared
     against the OCR read. Pages with no known profile go to the LLM.          arbitrate.py, llm_reader.py
  5. Pages are stitched into documents (multi-page invoices) and every page
     is accounted for in the extraction log — nothing is dropped silently.     assemble.py

Every extracted figure carries a SourceRef (page + bounding box).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pymupdf

from ..config import DEFAULT_LAYOUT_MARGINS, SupplierProfile
from ..llm import PageReader
from ..models import Document
from .arbitrate import best_ocr_read, second_read_scanned
from .assemble import PageLog, assemble
from .llm_reader import parse_page_from_llm
from .page import PageParse
from .profile_parser import page_profile, parse_page_with_profile
from .words import Word, page_png, page_text, text_layer_words

__all__ = ["extract_pdf", "PageLog", "PageParse"]

log = logging.getLogger(__name__)


def is_running_text(w: Word, page_height: float, profile: Optional[SupplierProfile]) -> bool:
    """Running header/footer (batch stamps etc.): not page content, so it doesn't count as a text layer."""
    top, bottom = ((profile.layout.running_header_margin, profile.layout.running_footer_margin)
                   if profile else DEFAULT_LAYOUT_MARGINS)
    return w.y0 < top or w.y0 > page_height - bottom


def extract_pdf(path: Path, profiles: list[SupplierProfile], llm: Optional[PageReader] = None, min_text_words: int = 25,
                progress=None) -> tuple[list[Document], list[PageLog]]:
    """Extract all documents from one supplier PDF."""
    path = Path(path)
    file = path.name
    pdf = pymupdf.open(path)
    parses: list[PageParse] = []
    prev_profile: Optional[SupplierProfile] = None
    for i in range(pdf.page_count):
        page = pdf[i]
        page_no = i + 1
        if progress:
            progress(page_no, len(pdf))
        height = page.rect.height
        words = text_layer_words(page)
        body_words = [w for w in words if not is_running_text(w, height, prev_profile)]
        method, ocr_error, pp = "text_layer", "", None
        if len(body_words) < min_text_words and page.get_images():
            method = "ocr"
            pp, words, profile, ocr_error = best_ocr_read(page, profiles, prev_profile, page_no, file)
        else:
            profile = page_profile(page_text(words), profiles, prev_profile)

        if profile:
            if pp is None:
                pp = parse_page_with_profile(words, profile, page_no, file, method, height)
            if method == "ocr" and llm is not None and llm.available:
                pp = second_read_scanned(pp, page, page_no, file, llm, profile)
        elif llm is not None and llm.available:
            pp = _read_unknown_layout(llm, page, words, page_no, file, method)
        else:
            pp = PageParse(page=page_no, method=method, profile="(none)")
            pp.notes.append("no supplier profile matched and no LLM configured: page not extracted")
        if ocr_error:
            pp.notes.append(ocr_error)
        parses.append(pp)
        prev_profile = profile or prev_profile

    return assemble(parses, file, prev_profile.date_order if prev_profile else "DMY")


def _read_unknown_layout(llm: PageReader, page, words: list[Word], page_no: int, file: str, method: str) -> PageParse:
    """A page no supplier profile matches is read by the LLM. A failed call (network,
    quota) leaves the page unread and accounted for, like a run without an LLM."""
    try:
        data, usage = (llm.extract_page_image(page_png(page, 150)) if method == "ocr"
                       else llm.extract_page_text(page_text(words)))
    except Exception as e:
        log.warning("%s p.%d: LLM read failed (%s: %s); page not extracted", file, page_no, type(e).__name__, e)
        pp = PageParse(page=page_no, method=method, profile="(none)")
        pp.notes.append(f"no supplier profile matched and the LLM read failed ({type(e).__name__}): page not extracted")
        return pp
    pp = parse_page_from_llm(data, page_no, file, "llm_vision" if method == "ocr" else "llm_text")
    pp.llm_usage = usage
    pp.notes.append("no supplier profile matched: extracted by LLM, verify before approving")
    return pp
