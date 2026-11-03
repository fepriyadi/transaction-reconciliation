"""A layout profile for a supplier no profile reads yet: drafted by the LLM, judged by code, saved by a person.

    unknown_layout_pages(page logs)   which pages of which PDF no profile read
    generate_profile(...)             LLM draft -> dry run -> one revision with the problems found -> Draft
    dry_run(data, ...)                the draft through the real extractor (extract_pdf) on the same PDF,
                                      compared with the LLM's own read of those pages and with R1-R4
    save_profile(draft, ...)          config/suppliers/<supplier>.json, only once a person confirms
    register_supplier(...)            ... and its go-live date in config/supplier_register.json (new-supplier review)

The dry run is the point. A profile that passes the schema can still read the wrong
words, and once saved its figures are trusted like any text-layer read (confidence
0.99). So nothing is saved while a page goes unread or a date is not understood, and
nothing where the two reads disagree until a person has looked at the page.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Optional

import pymupdf
from pydantic import ValidationError

from .config import ConfigError, Layout, RegisterFile, SupplierProfile, load_profiles
from .extract import PageLog, extract_pdf
from .extract.profile_parser import detect_profile, locate_table, table_problem
from .extract.words import ocr_words, page_text, text_layer_words
from .llm import ProfileDrafter
from .models import Document
from .normalize import fmt_num, norm_doc_no, norm_sku, norm_vendor
from .rules import RuleConfig, check_document, hard_failures

UNREAD = ("(llm)", "(none)")                   # PageLog.profile of a page no supplier profile read
HEADER = ("doc_type", "doc_no", "doc_date", "po_no", "reference", "subtotal", "dpp", "ppn", "total")
LINE = ("sku", "qty", "unit_price", "disc_pct", "amount")
_KEYS = ("doc_no", "po_no", "reference")       # compared as document numbers: separators don't matter
_ASSEMBLY_PROBLEMS = ("missing ", "no line items", "line numbers not continuous")


def unknown_layout_pages(page_logs: list[PageLog]) -> dict[str, list[int]]:
    """file -> pages no supplier profile read (read by the LLM, or not read at all)."""
    out: dict[str, list[int]] = {}
    for lg in page_logs:
        if lg.profile in UNREAD:
            out.setdefault(lg.file, []).append(lg.page)
    return out


def llm_documents(pdf_docs: list[Document], file: str, pages: list[int]) -> list[Document]:
    """The batch's documents of `file` that lie on `pages`: the LLM's read the draft is compared with."""
    return [d for d in pdf_docs if (f := d.fields.get("file")) and f.value == file and d.pages
            and set(d.pages) <= set(pages)]


# --------------------------------------------------------------------------- dry run

@dataclass
class DryRun:
    """What a draft profile reads on the pages being onboarded, and what stands between it and saving."""
    profile: Optional[SupplierProfile]
    docs: list = field(default_factory=list)            # documents the draft reads on those pages
    rows: list = field(default_factory=list)            # field-by-field comparison with the LLM read
    blocking: list = field(default_factory=list)        # cannot be saved while any of these stands
    disagreements: list = field(default_factory=list)   # the two reads differ: a person looks at the page
    to_check: list = field(default_factory=list)        # other things a person confirms having looked at
    red: set = field(default_factory=set)               # (id(document or line), field) behind a disagreement
    repairs: list = field(default_factory=list)         # slips in the reply code corrected before judging it

    @property
    def savable(self) -> bool:
        return self.profile is not None and not self.blocking

    @property
    def needs_look(self) -> bool:
        return bool(self.disagreements or self.to_check)


def dry_run(data, pdf_path, pages: list[int], profiles: list[SupplierProfile], llm_docs: list[Document],
            rules: RuleConfig = RuleConfig()) -> DryRun:
    """Judge a draft (the LLM's reply or the reviewer's edit) on `pages` of `pdf_path`.

    The whole PDF goes through extract_pdf with the existing profiles first, exactly as the
    next run will read it; no LLM is involved, so every figure is the profile's own read."""
    if not isinstance(data, dict):
        return DryRun(None, blocking=["not a valid profile: the draft is not a JSON object"])
    try:
        prof = SupplierProfile.model_validate({k: v for k, v in data.items() if k != "file" and not str(k).startswith("_")})
    except ValidationError as err:
        return DryRun(None, blocking=[f"not a valid profile: {'.'.join(map(str, x['loc'])) or 'profile'}: {x['msg']}"
                                      for x in err.errors()])
    run = DryRun(prof)
    for fname, spec in prof.layout.header_fields.items():
        for label in spec.labels:
            if re.fullmatch(spec.pattern, label.strip()):
                run.blocking.append(f"header_fields.{fname}: label '{label}' is itself a {fname} value, so only the "
                                    f"document printing it is found: use fixed text printed on that row of every document")
    if not prof.erp_vendor_names:
        run.blocking.append("no supplier name from the ERP export chosen: its entries would all be out of scope")
    for p in profiles:
        if p.name == prof.name or p.file == profile_path(".", prof).name:
            run.blocking.append(f"a profile for this supplier already exists ({p.file})")
    if not any(set(norm_vendor(k).split()) & set(norm_vendor(prof.vendor_name).split()) for k in prof.detect):
        run.to_check.append(f"detect text {prof.detect} does not contain the supplier's name: "
                            "make sure no other supplier prints it")

    pdf = pymupdf.open(pdf_path)
    for i in range(pdf.page_count):
        if i + 1 not in pages and detect_profile(page_text(text_layer_words(pdf[i])), [prof]):
            run.blocking.append(f"detect text {prof.detect} is also printed on p.{i + 1}, which another profile reads")

    if why := separator_problem([_words_of(pdf, pg, ocr=False)[0] for pg in pages], prof.decimal_separator):
        run.blocking.append(why)

    docs, logs = extract_pdf(Path(pdf_path), [*profiles, prof])
    for lg in logs:
        if lg.page not in pages:
            continue
        if lg.profile != prof.name:
            run.blocking.append(f"p.{lg.page}: not recognised, none of {prof.detect} is printed on it")
        run.blocking += [f"p.{lg.page}: {n}" for n in lg.notes]
    run.docs = [d for d in docs if set(d.pages) & set(pages)]

    ref = {d.key: d for d in llm_docs}
    for d in run.docs:
        for n in d.extraction_notes:
            if not n.startswith(_ASSEMBLY_PROBLEMS):
                continue
            if n == "no line items extracted":
                words, height = _words_of(pdf, d.pages[0])
                if why := table_problem(words, prof, height):
                    n = f"{n}: {why}"     # say which key to change, not only that the rows are missing
            run.blocking.append(f"{d.doc_no}: {n}")
        if d.lines:                       # rows read, but by a guessed column: the next layout change breaks it
            words, height = _words_of(pdf, d.pages[0], ocr=False)
            spot = locate_table(words, prof, height) if words else None
            if spot is not None and spot.sku_guessed:
                run.blocking.append(f"p.{d.pages[0]}: {table_problem(words, prof, height)}")
        raw_date = d.fields.get("doc_date")
        if raw_date and d.doc_date is None:
            run.blocking.append(f"{d.doc_no}: date '{raw_date.raw}' is not understood")
        theirs = ref.pop(d.key, None)
        if theirs is None:
            run.to_check.append(f"{d.doc_no}: the LLM did not read this document, nothing to compare with")
        _compare(run, d, theirs)
        # a check the draft's read fails but the LLM's read of the same figures passes: two independent
        # signals against the draft, so it blocks; one both reads fail is the supplier's own error
        llm_fails = {(c.rule, c.target) for c in hard_failures(check_document(theirs, rules))} if theirs else None
        for c in hard_failures(check_document(d, rules)):
            if llm_fails is not None and (c.rule, c.target) not in llm_fails:
                run.blocking.append(f"{d.doc_no}: {c.rule} {c.target} does not add up as the draft reads it "
                                    f"({fmt_num(c.stated)}, the rules give {fmt_num(c.expected)}) but does as the LLM "
                                    f"read it: the draft misreads it")
            else:
                run.to_check.append(f"{d.doc_no}: {c.rule} {c.target} printed {fmt_num(c.stated)}, the rules give "
                                    f"{fmt_num(c.expected)} (a supplier error, or a misread: check the box)")
    for left in ref.values():
        run.blocking.append(f"{left.doc_no} (p.{','.join(map(str, left.pages))}): read by the LLM but not by the draft")
    _against_llm_read(run, prof, llm_docs, pdf, pages)
    return run


def _against_llm_read(run: DryRun, prof: SupplierProfile, llm_docs: list[Document], pdf, pages: list[int]):
    """Name the key behind a miss, using what the LLM read on the same pages, so one revision can fix
    every layer at once: a pattern that does not match a printed value (the credit-note numbering), and
    an item table not found on a page whose document the draft did not even find."""
    patterns = {str(k): spec.pattern for k, spec in prof.layout.header_fields.items()}
    missed: dict[str, list[str]] = {}
    for e in llm_docs:
        for k, pattern in patterns.items():
            raw = str(f.raw).strip() if (f := e.fields.get(k)) is not None else ""
            if raw and not re.search(pattern, raw):
                missed.setdefault(k, []).append(raw)
    for name, vals in missed.items():
        shown = ", ".join(f"'{v}'" for v in dict.fromkeys(vals[:3]))
        run.blocking.append(f"header_fields.{name}: the pattern {patterns[name]!r} does not match "
                            f"{shown} as printed{f' ({len(vals)} documents)' if len(vals) > 1 else ''}: it must match "
                            f"every document's value")
    found = {d.pages[0] for d in run.docs}
    for pg in sorted({e.pages[0] for e in llm_docs if e.lines and e.pages} - found):
        if pg in pages:
            words, height = _words_of(pdf, pg, ocr=False)
            if words and (why := table_problem(words, prof, height)):
                run.blocking.append(f"p.{pg}: {why}")


_GROUPED_DOT = re.compile(r"(?<![\d.,])\d{1,3}(?:\.\d{3})+(?![\d.,]*\d)")      # 12.500.000
_GROUPED_COMMA = re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?![\d.,]*\d)")    # 12,500,000


def separator_problem(pages_words: list, decimal_separator: str) -> str:
    """The draft's decimal_separator against how the pages group thousands. A wrong one still reads
    1.850.000 right (two dots can only be thousands) but turns 575.000 into 575, so check it directly."""
    text = " ".join(w.text for words in pages_words for w in words)
    dots, commas = len(_GROUPED_DOT.findall(text)), len(_GROUPED_COMMA.findall(text))
    if decimal_separator == "." and dots >= 3 and dots > 3 * commas:
        return (f'decimal_separator is "." but the pages group thousands with dots ({dots} amounts like 12.500.000): '
                f'use ","')
    if decimal_separator == "," and commas >= 3 and commas > 3 * dots:
        return (f'decimal_separator is "," but the pages group thousands with commas ({commas} amounts like '
                f'12,500,000): use "."')
    return ""


def _words_of(pdf, page_no: int, ocr: bool = True):
    """(words, page height) as extract_pdf reads them; a scanned page is only OCR'd when `ocr`."""
    page = pdf[page_no - 1]
    words = text_layer_words(page)
    if len(words) < 25 and page.get_images():
        words = ocr_words(page) if ocr else []
    return words, page.rect.height


_TOP = set(SupplierProfile.model_fields) - set(Layout.model_fields)
_LAYOUT = set(Layout.model_fields) - set(SupplierProfile.model_fields)


def repair_draft(data: dict) -> tuple[dict, list[str]]:
    """Correct slips in an LLM reply that have only one possible meaning, and say what was corrected:
    a key one level too deep or too high, a multi-word sku_header ('ITEM CODE': the parser compares single
    words, and a column's first word is the one at its left edge), empty labels. Everything else, the
    dry run judges as it stands."""
    data = copy.deepcopy(data)
    fixed: list[str] = []
    layout = data.get("layout") if isinstance(data.get("layout"), dict) else None
    if layout is not None:
        for k in [k for k in layout if k in _TOP and k not in data]:
            data[k] = layout.pop(k)
            fixed.append(f"moved {k} out of layout")
        for k in [k for k in data if k in _LAYOUT and k not in layout]:
            layout[k] = data.pop(k)
            fixed.append(f"moved {k} into layout")
        table = layout.get("table")
        sku = table.get("sku_header") if isinstance(table, dict) else None
        if isinstance(sku, str) and len(sku.split()) > 1:
            word = next((w for w in sku.split() if re.search(r"[A-Za-z0-9]", w)), sku)
            table["sku_header"] = word
            fixed.append(f"sku_header '{sku}' -> '{word}' (one word of the heading row)")
        for fname, spec in (layout.get("header_fields") or {}).items():
            labels = spec.get("labels") if isinstance(spec, dict) else None
            if isinstance(labels, list) and any(not str(x).strip() for x in labels) and any(str(x).strip() for x in labels):
                spec["labels"] = [x for x in labels if str(x).strip()]
                fixed.append(f"dropped an empty {fname} label")
    return data, fixed


def _compare(run: DryRun, d: Document, e: Optional[Document]):
    def row(label, k, pv, lv, owner):
        if pv in (None, "") and lv in (None, ""):
            return
        agree = None if e is None else _same(k, pv, lv)
        run.rows.append({"Document no.": d.doc_no, "Field": label, "Draft profile": _show(pv),
                         "LLM read": "" if e is None else _show(lv),
                         "Agree": "—" if agree is None else ("yes" if agree else "NO")})
        if agree is False:
            run.red.add((id(owner), k))
            thousand = (isinstance(pv, (int, Fraction)) and isinstance(lv, (int, Fraction)) and pv and lv
                        and 1000 in (lv / pv, pv / lv))
            run.disagreements.append(f"{d.doc_no}: {label} — draft reads {_show(pv) or '(nothing)'}, "
                                     f"LLM read {_show(lv) or '(nothing)'}"
                                     + (" (a factor of 1,000: check decimal_separator)" if thousand else ""))

    for k in HEADER:
        row(k, k, getattr(d, k), getattr(e, k) if e else None, d)
    mine = {ln.line_no: ln for ln in d.lines}
    theirs = {ln.line_no: ln for ln in e.lines} if e else {}
    for no in sorted(set(mine) | set(theirs), key=lambda n: (n is None, n)):
        p, q = mine.get(no), theirs.get(no)
        if p is None or (q is None and e is not None):
            row(f"line {no}", "line", "present" if p else None, "present" if q else None, p)
            continue
        for k in LINE:
            row(f"line {no} {k}", k, getattr(p, k), getattr(q, k) if q else None, p)


def _same(k: str, a, b) -> bool:
    if k in _KEYS:
        return norm_doc_no(a or "") == norm_doc_no(b or "")
    if k == "sku":
        return norm_sku(a) == norm_sku(b)
    return a == b


def _show(v) -> str:
    return "" if v is None else fmt_num(v) if not isinstance(v, str) and not hasattr(v, "isoformat") else str(v)


# --------------------------------------------------------------------------- drafting

@dataclass
class Draft:
    file: str
    pages: list
    data: dict                                        # the profile as drafted, or as the reviewer edited it
    check: DryRun
    attempts: list = field(default_factory=list)      # one entry per LLM call: model, problems left, cost
    edited: bool = False

    @property
    def text(self) -> str:
        """The draft as the reviewer sees and edits it (schema order once it validates)."""
        body = self.check.profile.model_dump(mode="json", exclude={"file"}) if self.check.profile else self.data
        return json.dumps(body, indent=2, ensure_ascii=False)


def generate_profile(llm: ProfileDrafter, pdf_path, pages: list[int], profiles: list[SupplierProfile],
                     llm_docs: list[Document], erp_vendor_names: list[str], rules: RuleConfig = RuleConfig(),
                     attempts: int = 3) -> Draft:
    """Ask for a draft and dry-run it; when it leaves a page unread or disagrees with the LLM read,
    send the problems back for a revision. The best draft is kept (a revision can be worse).
    The ERP names are the reviewer's choice, never the model's.

    The revision carries the earlier draft in place of the example profile: the model already
    has its own draft in the right format, so the example would only be paid for twice."""
    samples = sample_text(pdf_path, sample_pages(pages, llm_docs))
    best: Optional[tuple[dict, DryRun]] = None
    log = []
    for i in range(attempts):
        # a revision is asked of the best draft so far and answered with the keys it changes only, merged
        # here: keys that were right can't be broken by a rewrite, and one bad revision doesn't compound
        prev = None if best is None or "layout" not in best[0] else best      # None: nothing usable to revise yet
        request = (example_text(profiles) + samples) if prev is None else (samples + revision_request(*prev))
        reply, info = llm.draft_profile(request)
        reply = {k: v for k, v in reply.items() if k != "file"} if isinstance(reply, dict) else {}
        data, fixed = repair_draft(reply if prev is None else merge_draft(prev[0], reply))
        data["erp_vendor_names"] = list(erp_vendor_names)
        run = dry_run(data, pdf_path, pages, profiles, llm_docs, rules)
        run.repairs = fixed
        cut_off = info.get("finish_reason") == "length"
        if info.get("finish_reason") == "invalid_json":
            run.blocking.insert(0, "the model's reply was not valid JSON")
        if cut_off:
            run.blocking.insert(0, "the reply was cut off at the completion-token limit "
                                   "(profile_max_completion_tokens in config/llm.json)")
        log.append({"attempt": i + 1, "model": info.get("model", ""), "fallback_from": info.get("fallback_from", ""),
                    "valid": run.profile is not None, "blocking": len(run.blocking), "disagreements": len(run.disagreements),
                    "prompt_tokens": info.get("prompt_tokens", 0), "completion_tokens": info.get("completion_tokens", 0),
                    "cost_usd": info.get("cost_usd", 0.0)})
        if best is None or _worse(best[1], run):
            best = (data, run)
        if cut_off or not (run.blocking or run.disagreements):
            break       # a revision under the same token limit would be cut off again
    assert best is not None
    return Draft(Path(pdf_path).name, list(pages), best[0], best[1], log)


def merge_draft(base: dict, change: dict) -> dict:
    """`base` with `change` applied: objects merge key by key, anything else (lists included) replaces,
    None removes the key."""
    out = copy.deepcopy(base)
    for k, v in change.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_draft(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _worse(a: DryRun, b: DryRun) -> bool:
    """Is `a` a worse draft than `b`? An invalid profile is worst, then blocking problems, then disagreements."""
    return (a.profile is None, len(a.blocking), len(a.disagreements)) > (b.profile is None, len(b.blocking), len(b.disagreements))


def recheck(draft: Draft, text: str, pdf_path, profiles: list[SupplierProfile], llm_docs: list[Document],
            rules: RuleConfig = RuleConfig()) -> Draft:
    """The reviewer's edit of the draft, dry-run again."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return Draft(draft.file, draft.pages, draft.data, DryRun(None, blocking=[f"not valid JSON: {e}"]),
                     draft.attempts, edited=True)
    return Draft(draft.file, draft.pages, data, dry_run(data, pdf_path, draft.pages, profiles, llm_docs, rules),
                 draft.attempts, edited=True)


def draft_request(pdf_path, pages: list[int], profiles: list[SupplierProfile], llm_docs: list[Document]) -> str:
    """The first request: the example profile and the sample pages."""
    return example_text(profiles) + sample_text(pdf_path, sample_pages(pages, llm_docs))


def example_text(profiles: list[SupplierProfile]) -> str:
    """An existing profile as a format example, compact: no indentation, no keys left at their default,
    one label per header field (the alternatives show nothing new about the format)."""
    if not profiles:
        return ""
    full = profiles[0].model_dump(mode="json", exclude={"file", "erp_vendor_names"})
    short = profiles[0].model_dump(mode="json", exclude_defaults=True, exclude={"file", "erp_vendor_names"})
    # decimal_separator and date_order stay even at their default: without them the model guesses where they go
    example = {k: v for k, v in full.items() if k in short or k in ("decimal_separator", "date_order")}
    example["layout"] = short["layout"]
    for spec in example["layout"]["header_fields"].values():
        spec["labels"] = spec["labels"][:1]
    return "Example profile (another supplier):\n" + _compact(example) + "\n\n"


def sample_pages(pages: list[int], llm_docs: list[Document]) -> list[int]:
    """The fewest pages that show every variation a pattern must cover: the first page of each
    document type (credit-note numbering differs) and one continuation page if a table runs on.
    Without an LLM read to tell the types apart, the first page alone."""
    picks = [d.pages[0] for t in ("INVOICE", "CREDIT_NOTE")
             if (d := next((x for x in llm_docs if x.doc_type == t and x.pages), None))]
    if cont := next((d for d in llm_docs if len(d.pages) > 1), None):
        picks.append(cont.pages[1])
    return sorted(set(picks)) or pages[:1]


ITEM_ROWS_SHOWN = 3


def sample_text(pdf_path, pages: list[int]) -> str:
    """The sample pages, one line per printed row, without what the model has already seen: rows
    repeated from an earlier sample page (letterhead, bank details) and item rows past the first
    few (they all have the same shape). Each cut is marked, so the model knows rows are missing."""
    pdf = pymupdf.open(pdf_path)
    seen: set[str] = set()
    parts = []
    for pg in pages:
        page = pdf[pg - 1]
        words = text_layer_words(page)
        if len(words) < 25 and page.get_images():
            words = ocr_words(page)
        rows, repeated, items, cut = [], 0, 0, 0
        for line in page_text(words).splitlines():
            if line in seen:
                repeated += 1
                continue
            seen.add(line)
            if _ITEM_ROW.match(line):
                items += 1
                if items > ITEM_ROWS_SHOWN:
                    cut += 1
                    continue
            elif cut:
                rows.append(f"… {cut} more item row(s) of the same shape")
                cut = 0
            rows.append(line)
        if cut:
            rows.append(f"… {cut} more item row(s) of the same shape")
        if repeated:
            rows.append(f"… {repeated} row(s) identical to an earlier sample page left out")
        parts.append(f"Sample page {pg}:\n" + "\n".join(rows))
    return "\n\n".join(parts)


_ITEM_ROW = re.compile(r"^\d{1,3}\.? \S+ .*\d")      # a line number, then text ending in figures


def revision_request(data: dict, run: DryRun, limit: int = 10) -> str:
    """The earlier draft (without the reviewer's ERP names, which code sets anyway) and what the
    parser found wrong with it, one line per kind of problem."""
    return ("\n\nYour earlier draft:\n" + _compact({k: v for k, v in data.items() if k != "erp_vendor_names"})
            + "\n\nProblems the parser found with it on these pages:\n- "
            + "\n- ".join(grouped(run.blocking + run.disagreements)[:limit])
            + '\n\nReply with ONLY the keys you change, nested as in the draft, e.g. '
              '{"layout":{"header_fields":{"doc_no":{"pattern":"..."}}}}. Keys you leave out stay as they are; '
              'a list you send replaces the whole list; null removes a key.')


def grouped(problems: list[str]) -> list[str]:
    """'SDS/1: date '02' is not understood' x11 -> one line with a count: the same mistake on every
    document is one fix, and repeating it would only cost tokens."""
    groups: dict[str, list[str]] = {}
    for m in problems:
        groups.setdefault(re.sub(r"\d+", "#", m.split(": ", 1)[-1]), []).append(m)
    return [ms[0] + (f" (and {len(ms) - 1} more like it)" if len(ms) > 1 else "") for ms in groups.values()]


def _compact(data) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- saving

def profile_path(config_dir, profile: SupplierProfile) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", norm_vendor(profile.vendor_name).lower()).strip("_") or "supplier"
    return Path(config_dir) / "suppliers" / f"{slug}.json"


def save_profile(draft: Draft, config_dir, reviewer: str, looked: bool = False, now: Optional[datetime] = None) -> Path:
    """Write the confirmed draft. Refuses a draft with a blocking problem, one with disagreements the
    reviewer has not confirmed looking at, an existing file, and anything that breaks the config load."""
    run = draft.check
    if not run.savable or run.profile is None:
        raise ValueError("the draft has blocking problems: " + "; ".join(run.blocking[:3]))
    if run.needs_look and not looked:
        raise ValueError("the reviewer has not confirmed looking at the flagged figures")
    if not reviewer.strip():
        raise ValueError("enter the reviewer's name")
    path = profile_path(config_dir, run.profile)
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    model = next((a["model"] for a in draft.attempts if a.get("model")), "the LLM")
    pages = f"p.{draft.pages[0]}-{draft.pages[-1]}" if len(draft.pages) > 1 else f"p.{draft.pages[0]}"
    note = (f"Drafted by {model} from {draft.file} {pages}{', edited by the reviewer' if draft.edited else ''}; "
            f"dry-run against the LLM read and R1-R4 ({len(run.disagreements)} disagreement(s) looked at); "
            f"confirmed by {reviewer.strip()} on {(now or datetime.now()):%Y-%m-%d %H:%M}.")
    body = {"_comment": note, **run.profile.model_dump(mode="json", exclude={"file"})}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    try:
        load_profiles(Path(config_dir))
    except ConfigError:
        path.unlink()
        raise
    return path


def register_supplier(config_dir, names: list[str], since: date) -> list[str]:
    """Add the supplier to config/supplier_register.json with go-live date `since`, so its documents are
    reviewed for the new-supplier period. A name already registered keeps its date: its period started
    then. Returns the names added (compared as norm_vendor, as the register looks them up)."""
    path = Path(config_dir) / "supplier_register.json"
    body = json.loads(path.read_text()) if path.exists() else {}
    have = body.setdefault("suppliers", {})
    known = {norm_vendor(n) for n in have}
    added = []
    for name in names:
        if name.strip() and norm_vendor(name) not in known:
            have[name.strip()] = {"onboarded": since.isoformat()}
            known.add(norm_vendor(name))
            added.append(name.strip())
    if not added:
        return []
    RegisterFile.model_validate({k: v for k, v in body.items() if not k.startswith("_")})
    # one line per supplier, as the file is written by hand
    head = "".join(f"  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)},\n"
                   for k, v in body.items() if k != "suppliers")
    rows = ",\n".join(f"    {json.dumps(n, ensure_ascii=False)}: {json.dumps(v)}" for n, v in have.items())
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text("{\n" + head + '  "suppliers": {\n' + rows + "\n  }\n}\n")
    tmp.replace(path)
    return added
