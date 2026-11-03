"""Profile-driven page parse: labels select header rows, a row grammar reads table lines.

The layout spec comes from a validated SupplierProfile (config/suppliers/*.json),
so supporting a new layout is a config change, not a code change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from ..config import RightField, SupplierProfile, TableSpec
from ..models import Field, Line, SourceRef
from ..normalize import parse_int_amount, parse_number, parse_percent
from .page import PageParse
from .words import Word, compact, group_rows, row_text, union_bbox


def detect_profile(page_text: str, profiles: list[SupplierProfile]) -> Optional[SupplierProfile]:
    up = page_text.upper()
    for p in profiles:
        if any(k.upper() in up for k in p.detect):
            return p
    return None


def looks_like_continuation(page_text: str, prev_profile: Optional[SupplierProfile]) -> bool:
    if not prev_profile:
        return False
    comp = compact(page_text)
    return all(compact(k) in comp for k in prev_profile.layout.table.header_keywords)


def page_profile(page_text: str, profiles: list[SupplierProfile],
                 prev_profile: Optional[SupplierProfile]) -> Optional[SupplierProfile]:
    """The page's own profile, or the previous page's when this is a continuation page."""
    return detect_profile(page_text, profiles) or (prev_profile if looks_like_continuation(page_text, prev_profile) else None)


def match_right_grammar(tokens: list[Word], spec: list[RightField]):
    """Match `spec` (left-to-right column list) against the right end of `tokens`.

    Optional columns are tried taken first, then skipped (tiny backtracking).
    Returns (assignment dict name->Word, n_tokens_consumed) or None.
    """
    def ok(tok: Word, typ: str) -> bool:
        if typ == "number":
            return parse_number(tok.text) is not None
        if typ == "percent":
            return parse_percent(tok.text) is not None
        if typ == "alpha":
            return bool(re.fullmatch(r"[A-Za-z][A-Za-z.]*", tok.text.strip(".,")))
        return True

    def noise(tok: Word) -> bool:
        t = tok.text.strip()
        return len(t) <= 1 and not t.isdigit()

    def rec(ti: int, si: int, skips: int):
        if si < 0:
            return {}
        f = spec[si]
        if ti >= 0 and ok(tokens[ti], f.type):
            r = rec(ti - 1, si - 1, skips)
            if r is not None:
                r[f.name] = tokens[ti]
                return r
        if f.optional:
            r = rec(ti, si - 1, skips)
            if r is not None:
                return r
        if ti >= 0 and skips > 0 and noise(tokens[ti]):   # OCR speck between columns
            r = rec(ti - 1, si, skips - 1)
            if r is not None:
                r.setdefault("_skipped", []).append(tokens[ti])
                return r
        return None

    res = rec(len(tokens) - 1, len(spec) - 1, 2)
    if res is None:
        return None
    skipped = res.pop("_skipped", [])
    return res, len(res) + len(skipped)


def parse_page_with_profile(words: list[Word], profile: SupplierProfile, page_no: int, file: str,
                            method: str, page_height: Optional[float] = None) -> PageParse:
    """`page_height`: the real page height in points (the profile's value is only a fallback)."""
    pp = PageParse(page=page_no, method=method, profile=profile.name)
    rows = group_rows(words)
    lay = profile.layout
    hint = profile.decimal_separator

    def ref(ws: list[Word]) -> SourceRef:
        return SourceRef(file=file, page=page_no, bbox=union_bbox(ws), method=method)

    def conf_of(ws: list[Word]):
        if ws:
            pp.min_conf = min(pp.min_conf, min(w.conf for w in ws))

    # ---- document type
    full = "\n".join(row_text(r) for r in rows).upper()
    for dtype, keys in lay.doc_types.items():
        if any(k.upper() in full for k in keys):
            pp.doc_type = dtype
            break
    pp.vendor = profile.vendor_name

    # ---- header fields: label selects the row, pattern finds the value to its right
    for fname, spec in lay.header_fields.items():
        pat = re.compile(spec.pattern)
        labels = [compact(l) for l in spec.labels]
        for r in rows:
            comp = compact(row_text(r))
            if not any(l in comp for l in labels):
                continue
            at = _label_at(r, spec.labels)
            hit = _search_words(r[at[0]:], pat, skip=at[1]) if at else _search_words(r, pat)
            if hit:
                value, ws = hit
                pp.header[fname] = Field(value, row_text(ws), ref(ws))
                conf_of(ws)
                break

    # ---- table
    tbl = lay.table
    spot = locate_table(words, profile, page_height, rows)
    if spot is not None:
        pp.has_table = True
        body, anchors = spot.body, spot.anchors
        groups: dict[int, list[Word]] = {id(a): [] for a in anchors}
        for w in body:
            if not anchors or any(w is a for a in anchors):
                continue
            nearest = min(anchors, key=lambda a: abs(a.yc - w.yc))
            if abs(nearest.yc - w.yc) <= tbl.row_reach:
                groups[id(nearest)].append(w)
            else:
                pp.notes.append(f"p.{page_no}: unassigned table text '{w.text}'")
        for a in anchors:
            ln = _parse_line(a, groups[id(a)], tbl, hint, ref, pp)
            if ln:
                pp.lines.append(ln)

    # ---- totals: label word starts the row segment, value is the rightmost amount
    for tname, tspec in lay.totals.items():
        lab_words = [compact(x) for x in tspec.labels[0].split()]
        for r in rows:
            idx = next((i for i, w in enumerate(r) if compact(w.text) == lab_words[0]), None)
            if idx is None:
                continue
            rest = r[idx + 1:]
            vals = [w for w in rest if parse_int_amount(w.text, hint) is not None and len(re.sub(r"\D", "", w.text)) >= 1]
            if not vals:
                continue
            v = vals[-1]
            label = " ".join(w.text for w in r[idx:] if w.x1 <= v.x0)
            pp.totals[tname] = Field(parse_int_amount(v.text, hint), v.text, ref([v]))
            pp.totals_labels[tname] = label
            conf_of([v])
            break
    return pp


@dataclass
class TableSpot:
    """Where a page's item table is, as the parser sees it (shared with the profile dry run's diagnosis)."""
    heading: list[Word]          # the column-heading row
    top: float                   # item rows start below this y
    stop_y: float                # ... and end above this y
    stop_why: str                # what ended the table there
    sku_x: float                 # line numbers must lie left of this x
    sku_guessed: bool            # sku_header is not a word of the heading row: sku_x is a guess
    body: list[Word]
    anchors: list[Word]          # line-number words, top to bottom


def locate_table(words: list[Word], profile: SupplierProfile, page_height: Optional[float] = None,
                 rows: Optional[list[list[Word]]] = None) -> Optional[TableSpot]:
    """The item table: the first row holding every header keyword, down to the first total label,
    stop marker or the footer margin. None when no row holds every keyword."""
    rows = group_rows(words) if rows is None else rows
    lay, tbl = profile.layout, profile.layout.table
    hrow = next((r for r in rows if all(compact(k) in compact(row_text(r)) for k in tbl.header_keywords)), None)
    if hrow is None:
        return None
    hy = hrow[0].yc
    top = max(w.y1 for w in words if abs(w.yc - hy) <= 10)
    total_labels_first = {compact(s.labels[0].split()[0]) for s in lay.totals.values()}
    stop_y = (page_height or profile.page_height) - lay.footer_margin
    stop_why = f"footer_margin {lay.footer_margin:g}"
    for r in rows:
        if r[0].yc <= top:
            continue
        marker = next((s for s in tbl.stop_markers if compact(s) in compact(row_text(r))), None)
        if compact(r[0].text) in total_labels_first or marker:
            if r[0].y0 < stop_y:
                stop_y = r[0].y0
                stop_why = (f"stop marker '{marker}'" if marker else f"total label '{r[0].text}'") + f" on '{row_text(r)}'"
            break
    body = [w for w in words if top < w.yc < stop_y]
    sku_x = min((w.x0 for w in hrow if compact(w.text) == compact(tbl.sku_header)), default=None)
    guessed = sku_x is None
    if sku_x is None:
        sku_x = min((w.x0 for w in body), default=0) + 20
    anchors = sorted((w for w in body if w.xc < sku_x - 2 and re.fullmatch(r"\d{1,3}\.?", w.text)), key=lambda w: w.yc)
    return TableSpot(hrow, top, stop_y, stop_why, sku_x, guessed, body, anchors)


def table_problem(words: list[Word], profile: SupplierProfile, page_height: Optional[float] = None) -> str:
    """Why the parser reads no item rows on this page, naming the profile key to change ('' if it does)."""
    tbl = profile.layout.table
    rows = group_rows(words)
    spot = locate_table(words, profile, page_height, rows)
    if spot is None:
        closest = max(rows, key=lambda r: sum(compact(k) in compact(row_text(r)) for k in tbl.header_keywords),
                      default=[])
        missing = [k for k in tbl.header_keywords if compact(k) not in compact(row_text(closest))]
        return (f"the table heading row was not found: no row has every header_keywords word; the closest, "
                f"'{row_text(closest)}', lacks {missing} (use only words printed on the heading row of every "
                f"document type)")
    if spot.sku_guessed:
        return (f"sku_header '{tbl.sku_header}' is not a word of the heading row '{row_text(spot.heading)}' "
                f"(use the single word over the item-code column)")
    if not spot.anchors:
        if not [w for w in spot.body if re.fullmatch(r"\d{1,3}\.?", w.text)]:
            return f"the table ends above its first item row, at {spot.stop_why}"
        return (f"no line numbers left of the sku_header column '{tbl.sku_header}': it must head the item-code "
                f"column, not the line-number column")
    return ""


def _label_at(row: list[Word], labels: list[str]) -> Optional[tuple[int, int]]:
    """Where the label starts in the row (index of its first word) and its length in compact characters.
    Labels are compared without spaces/punctuation so OCR spacing doesn't matter."""
    comps = [compact(l) for l in labels]
    for i in range(len(row)):
        acc = ""
        for w in row[i:]:
            acc += compact(w.text)
            hit = next((c for c in comps if acc.startswith(c)), None)
            if hit is not None:
                return i, len(hit)
            if not any(c.startswith(acc) for c in comps):
                break
    return None


def _search_words(ws: list[Word], pat: re.Pattern, skip: int = 0):
    """First match of `pat` in the words joined by single spaces, so a value may span words
    ('02 Oct 2026'), searched after the first `skip` compact characters: the label, so 'PO' in
    'PO Reference:' is never the value, while a value OCR glued to its label ('Date:02-10-2026')
    is still found. Returns (matched text, the words it covers) or None."""
    text, spans = "", []
    for w in ws:
        start = len(text) + (1 if text else 0)
        text = f"{text} {w.text}" if text else w.text
        spans.append((start, len(text), w))
    pos, seen = 0, 0
    while seen < skip and pos < len(text):
        seen += bool(compact(text[pos]))
        pos += 1
    m = pat.search(text, pos)
    if not m:
        return None
    return m.group(0), [w for s, e, w in spans if s < m.end() and e > m.start()]


def _clean(w: Word) -> Optional[Word]:
    """Strip OCR edge noise ('«pes' -> 'pes', '_77.188' -> '77.188'); drop pure specks ('=', '~~')."""
    t = re.sub(r"^[^A-Za-z0-9(]+|[^A-Za-z0-9.,%)]+$", "", w.text)
    if not re.search(r"[A-Za-z0-9]", t):
        return None
    return w if t == w.text else Word(t, w.x0, w.y0, w.x1, w.y1, w.conf, w.orig)


def _parse_line(anchor: Word, group: list[Word], tbl: TableSpec, hint: str, ref, pp: PageParse) -> Optional[Line]:
    cleaned = [c for c in (_clean(w) for w in group) if c is not None]     # for the figures only
    same_row = sorted([w for w in cleaned if abs(w.yc - anchor.yc) <= 3.5], key=lambda w: w.x0)
    m = match_right_grammar(same_row, tbl.right_fields)
    line_no = int(anchor.text.rstrip("."))
    if not m:
        pp.notes.append(f"p.{pp.page}: line {line_no} does not fit the row grammar: '{row_text(same_row)}'")
        return None
    assign, n = m
    left = same_row[: len(same_row) - n]
    sku_w = left[0] if left else None
    # description = original words left of the figures (keeps '/', drops specks between columns)
    fig_x = same_row[len(same_row) - n].x0
    sku_box = sku_w.bbox if sku_w else None
    desc_words = [w for w in group if w.bbox != sku_box and
                  not (abs(w.yc - anchor.yc) <= 3.5 and w.x0 >= fig_x - 1)]
    desc = " ".join(row_text(r) for r in group_rows(desc_words))
    # confidence is driven by the words that carry figures, not descriptions
    pp.min_conf = min([pp.min_conf] + [w.conf for k, w in assign.items() if k != "uom"])

    def num(name, default=Fraction(0), parse=parse_number):
        w = assign.get(name)
        return (parse(w.text, hint) if w else default), w

    qty, qw = num("qty")
    price, pw = num("unit_price")
    disc, dw = num("disc_pct", parse=parse_percent)
    amt_w = assign["amount"]
    amount = parse_int_amount(amt_w.text, hint)
    if amount is None:
        pp.notes.append(f"p.{pp.page}: line {line_no} amount unreadable '{amt_w.text}'")
        return None
    uw = assign.get("uom")
    fields = {
        "line_no": Field(line_no, anchor.text, ref([anchor])),
        "sku": Field(sku_w.text if sku_w else "", sku_w.text if sku_w else "", ref([sku_w]) if sku_w else None),
        "description": Field(desc, desc, ref(desc_words)),
        "qty": Field(qty, qw.text if qw else "", ref([qw]) if qw else None),
        "uom": Field(uw.text if uw else "", uw.text if uw else "", ref([uw]) if uw else None),
        "unit_price": Field(price, pw.text if pw else "", ref([pw]) if pw else None),
        "disc_pct": Field(disc, dw.text if dw else "(blank)", ref([dw]) if dw else None),
        "amount": Field(amount, amt_w.text, ref([amt_w])),
    }
    return Line(line_no, (sku_w.text if sku_w else "").strip("_.,"), desc, qty,
                (uw.text if uw else "").strip(".,"), price, disc, amount, fields)
