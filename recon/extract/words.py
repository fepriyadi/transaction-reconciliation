"""Positioned words from a PDF page: the text layer, or tesseract OCR for scans.

OCR coordinates are mapped back to PDF points and de-skewed so table rows line
up again; each word keeps its original bbox for provenance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import pymupdf

OCR_DPI = 300


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 100.0
    orig: Optional[tuple] = None   # bbox on the page before deskew (for provenance)

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def bbox(self) -> tuple:
        return self.orig or (self.x0, self.y0, self.x1, self.y1)


def compact(s: str) -> str:
    """Lower-case alphanumerics only, so labels compare regardless of OCR spacing/punctuation."""
    return re.sub(r"[^a-z0-9%]", "", s.lower())


def union_bbox(words: list[Word]) -> Optional[tuple]:
    if not words:
        return None
    bs = [w.bbox for w in words]
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def text_layer_words(page) -> list[Word]:
    return [Word(w[4], w[0], w[1], w[2], w[3]) for w in page.get_text("words")]


OCR_PREFILTERS = (None, 3, 5)   # raw first; median filters only if the raw read doesn't add up


def ocr_words(page, dpi: int = OCR_DPI, median: Optional[int] = None) -> list[Word]:
    """OCR a page with tesseract; coordinates are mapped back to PDF points and
    de-skewed (scans are rarely straight) so table rows line up again."""
    import io

    import pytesseract
    from PIL import Image, ImageFilter

    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    if median:
        img = img.filter(ImageFilter.MedianFilter(median))   # removes scanner speckle
    data = pytesseract.image_to_data(img, config="--psm 6", output_type=pytesseract.Output.DICT)
    k = 72.0 / dpi
    out, line_ids = [], []
    for i, txt in enumerate(data["text"]):
        txt = (txt or "").strip()
        if not txt:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        out.append(Word(txt, x * k, y * k, (x + w) * k, (y + h) * k, float(data["conf"][i])))
        line_ids.append((data["block_num"][i], data["par_num"][i], data["line_num"][i]))
    return deskew(out, line_ids)


def deskew(words: list[Word], line_ids: list) -> list[Word]:
    """Estimate skew as the median slope of OCR text lines and straighten y."""
    from statistics import median
    lines: dict = {}
    for w, lid in zip(words, line_ids):
        lines.setdefault(lid, []).append(w)
    slopes = []
    for ws in lines.values():
        if len(ws) < 3 or ws[-1].xc - ws[0].xc < 100:
            continue
        xs, ys = [w.xc for w in ws], [w.yc for w in ws]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        if den:
            slopes.append(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den)
    if not slopes:
        return words
    m = median(slopes)
    if abs(m) < 0.002:
        return words
    out = []
    for w in words:
        dy = m * w.xc
        out.append(Word(w.text, w.x0, w.y0 - dy, w.x1, w.y1 - dy, w.conf, orig=(w.x0, w.y0, w.x1, w.y1)))
    return out


def page_png(page, dpi: int = 150) -> bytes:
    return page.get_pixmap(dpi=dpi).tobytes("png")


def group_rows(words: list[Word], tol: float = 3.5) -> list[list[Word]]:
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.yc, w.x0)):
        if rows and abs(rows[-1][0].yc - w.yc) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w.x0) for r in rows]


def row_text(row: list[Word]) -> str:
    return " ".join(w.text for w in row)


def page_text(words: list[Word]) -> str:
    return "\n".join(row_text(r) for r in group_rows(words))
