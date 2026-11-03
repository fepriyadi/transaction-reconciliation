"""A PDF page as a PNG with figure boxes drawn on it (the audit view)."""
from __future__ import annotations

import io

import pymupdf
from PIL import Image, ImageDraw

BOX_RGB = {"red": (220, 38, 38), "blue": (37, 99, 235), "green": (22, 163, 74)}

Box = tuple[float, float, float, float, str]       # x0, y0, x1, y1 in PDF points, colour name


def render_page(pdf_path: str, page_no: int, boxes: tuple[Box, ...], dpi: int = 110) -> bytes:
    """Page `page_no` (1-based) with each box outlined and lightly filled in its colour."""
    doc = pymupdf.open(pdf_path)
    pix = doc[page_no - 1].get_pixmap(dpi=dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    k = dpi / 72
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(over)
    for x0, y0, x1, y1, color in boxes:
        rgb = BOX_RGB[color]
        dr.rectangle([x0 * k - 2, y0 * k - 2, x1 * k + 2, y1 * k + 2], outline=rgb + (255,), width=2, fill=rgb + (40,))
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
