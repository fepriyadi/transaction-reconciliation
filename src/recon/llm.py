"""The LLM roles the pipeline depends on, and their Groq implementation. Used for exactly four things:

1. Second read of scanned pages (vision) — cross-checked against OCR.
2. Extraction of pages from suppliers with no layout profile yet (text/vision).
3. Wording the AP-manager/CFO summary from already-computed facts.
4. Drafting a layout profile for such a supplier — only saved after code has run it
   on the same pages and a person has confirmed it (recon.profile_draft).

Never used for arithmetic, matching, status or per-document explanations.
Every LLM output is treated as untrusted: extraction results go through the
same deterministic checks as OCR/text, and the summary is rejected if it
contains any number that is not in the computed facts.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol, Union

from pydantic import BaseModel, ConfigDict, ValidationError

from .config import LLMConfig

log = logging.getLogger(__name__)

EXTRACT_PROMPT = """You extract supplier invoices / credit notes into JSON. Copy values exactly as printed.
Do not calculate, correct or infer any number. If a value is not printed, use null.
Return ONLY a JSON object with keys:
 doc_type ("INVOICE" or "CREDIT_NOTE"), vendor, doc_no, doc_date (as printed), due_date, po_no,
 reference (original invoice for credit notes), lines (array of {line_no, sku, description, qty, uom,
 unit_price, disc_pct, amount} — numbers as printed strings, e.g. "1.238.490", disc_pct null if blank),
 subtotal, dpp, dpp_label (the exact label text printed next to the DPP figure), ppn, total.
If the page is a continuation page without a header, set doc_no null and still return the lines/totals."""

SUMMARY_PROMPT = """You write a short reconciliation summary that an accounts-payable manager can forward to the CFO.
Use ONLY the facts in the JSON. Copy every amount exactly as written in the facts (e.g. "Rp 17,238,178");
do not round, add, subtract or invent numbers, dates or counts. Plain business English, no jargon, no markdown headers.
Structure: one sentence on the outcome (is the difference fully explained?), then the 3-5 issues with money at stake
(largest first) and what is being done, then one sentence on what was approved. Max 170 words."""

PROFILE_PROMPT = """Write a supplier layout profile: JSON a deterministic parser uses to read this supplier's
invoices and credit notes from the words of each printed row. Input: sample pages (one line per printed row; lines
starting with "…" mark rows left out) and either another supplier's profile as a format example or your earlier draft.
Reply with the JSON object only, in the same format. The parser uses the keys like this:
- detect: text printed on every page of this supplier and on no other supplier's page (its name, upper case).
- decimal_separator: "," for 1.250.000, "." for 1,250,000. date_order: DMY or MDY.
- doc_types: keywords per type; list CREDIT_NOTE first (a credit note also mentions "invoice").
- header_fields (doc_no required; doc_date, due_date, po_no, reference = original invoice on a credit note):
  labels = fixed text printed left of the value on that row of every document, never a value itself and never ""
  (no label printed: use fixed text on that row, e.g. the start of the address);
  pattern = Python regex for the value. The row's words are joined by single spaces, so it may contain spaces;
  days and months may have 1 or 2 digits.
  It must match every sample's value (invoice and credit-note numbering) and nothing between label and value.
- table: header_keywords = 2-4 words printed on the column-heading row of EVERY document type (a word only
  the invoice or only the credit note prints means the other's table is never found); sku_header = the one word
  over the item-code column, never "#" (item rows start with a line number left of it); right_fields = the
  columns at the right end of an item row, left to right, named qty, uom, unit_price, disc_pct, amount (amount
  last), typed number, alpha or percent; disc_pct optional if sometimes blank; stop_markers = the first non-total
  row below the items.
- totals (subtotal, dpp, ppn, total): the first word of the first label is the first word printed before the amount."""


@dataclass
class LLMUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    cost_usd: float = 0.0
    log: list = field(default_factory=list)


# --------------------------------------------------------------------------- what the pipeline needs from a model
# Two narrow roles, so extraction and summary depend only on what they call.
# GroqLLM is the production implementation; tests use a fake (tests/fakes.py).

class PageReader(Protocol):
    """Reads a page into the EXTRACT_PROMPT shape: (untrusted reply, call info)."""
    @property
    def available(self) -> bool: ...
    def extract_page_image(self, png: bytes) -> tuple[dict, dict]: ...
    def extract_page_text(self, page_text: str) -> tuple[dict, dict]: ...


class SummaryWriter(Protocol):
    """Words the CFO summary from computed facts: (text, call info)."""
    text_model: str

    @property
    def available(self) -> bool: ...
    def write_summary(self, facts: dict) -> tuple[str, dict]: ...


class ProfileDrafter(Protocol):
    """Drafts a supplier layout profile (PROFILE_PROMPT shape) from a request text: (untrusted reply, call info)."""
    @property
    def available(self) -> bool: ...
    def draft_profile(self, request: str) -> tuple[dict, dict]: ...


class LLMClient(PageReader, SummaryWriter, ProfileDrafter, Protocol):
    """Every role plus the usage the run reports."""
    usage: LLMUsage


# --------------------------------------------------------------------------- extraction output schema

Scalar = Optional[Union[int, float, str]]     # numbers may come back as printed strings or as JSON numbers


class LLMLine(BaseModel):
    model_config = ConfigDict(extra="ignore")
    line_no: Scalar = None
    sku: Scalar = None
    description: Scalar = None
    qty: Scalar = None
    uom: Scalar = None
    unit_price: Scalar = None
    disc_pct: Scalar = None
    amount: Scalar = None


class LLMPage(BaseModel):
    """What EXTRACT_PROMPT asks for. LLM output is untrusted: anything that does not fit
    this shape is rejected here, and every value is still re-parsed and re-checked by code."""
    model_config = ConfigDict(extra="ignore")
    doc_type: Scalar = None
    vendor: Scalar = None
    doc_no: Scalar = None
    doc_date: Scalar = None
    due_date: Scalar = None
    po_no: Scalar = None
    reference: Scalar = None
    lines: Optional[list[LLMLine]] = None
    subtotal: Scalar = None
    dpp: Scalar = None
    dpp_label: Scalar = None
    ppn: Scalar = None
    total: Scalar = None

    @classmethod
    def from_untrusted(cls, data) -> tuple["LLMPage", str]:
        """(page, problem); an unusable reply becomes an empty page plus a note, never an exception."""
        if isinstance(data, LLMPage):
            return data, ""
        try:
            return cls.model_validate(data if isinstance(data, dict) else {}), ""
        except ValidationError as e:
            return cls(), f"LLM reply did not match the extraction schema ({e.error_count()} error(s)); page not read"


# --------------------------------------------------------------------------- client

def is_rate_limited(e: Exception) -> bool:
    """429, or Groq's 413 for a request over the tokens-per-minute limit: the model is busy, not the request wrong."""
    status = getattr(e, "status_code", None)
    return status == 429 or (status == 413 and "per minute" in str(e).lower())


class GroqLLM:
    def __init__(self, config: LLMConfig | None = None, api_key: str | None = None, text_model: str | None = None,
                 vision_model: str | None = None, fallback_text_model: str | None = None,
                 fallback_vision_model: str | None = None):
        cfg = config or LLMConfig()
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.text_model = text_model or os.getenv("GROQ_TEXT_MODEL") or cfg.text_model
        self.vision_model = vision_model or os.getenv("GROQ_VISION_MODEL") or cfg.vision_model
        # called only when the main model is rate-limited; "" turns the fallback off
        self.fallback_text_model = _first(fallback_text_model, os.getenv("GROQ_FALLBACK_TEXT_MODEL"), cfg.fallback_text_model)
        self.fallback_vision_model = _first(fallback_vision_model, os.getenv("GROQ_FALLBACK_VISION_MODEL"),
                                            cfg.fallback_vision_model)
        self.config = cfg
        self.usage = LLMUsage()
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _cli(self):
        if self._client is None:
            from groq import Groq
            self._client = Groq(api_key=self.api_key)
        return self._client

    def _chat(self, model: str, messages: list, json_mode: bool, purpose: str, fallback: str = "",
              **params) -> tuple[str, dict]:
        """One call; if `model` is rate-limited, the same call once on `fallback`. The call info names
        the model that answered (and `fallback_from`), so cost and the run log follow the real model."""
        try:
            return self._call(model, messages, json_mode, purpose, **params)
        except Exception as e:
            if not fallback or fallback == model or not is_rate_limited(e):
                raise
            log.warning("LLM %s: %s is rate-limited (%s); using %s", purpose, model, type(e).__name__, fallback)
            text, info = self._call(fallback, messages, json_mode, purpose, **params)
            info["fallback_from"] = model
            return text, info

    def _call(self, model: str, messages: list, json_mode: bool, purpose: str, **params) -> tuple[str, dict]:
        t0 = time.time()
        kw = dict(model=model, messages=messages, temperature=0, **params)
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        resp = self._cli().chat.completions.create(**kw)
        dt = time.time() - t0
        u = resp.usage
        pt, ct = (u.prompt_tokens, u.completion_tokens) if u else (0, 0)
        price = self.config.price(model)
        cost = (pt / 1e6 * price.input + ct / 1e6 * price.output) if price else 0.0
        self.usage.calls += 1
        self.usage.prompt_tokens += pt
        self.usage.completion_tokens += ct
        self.usage.seconds += dt
        self.usage.cost_usd += cost
        choice = resp.choices[0]
        info = {"purpose": purpose, "model": model, "prompt_tokens": pt, "completion_tokens": ct,
                "seconds": round(dt, 2), "cost_usd": round(cost, 6),
                "finish_reason": getattr(choice, "finish_reason", "") or ""}     # "length": cut off at the token limit
        self.usage.log.append(info)
        log.debug("LLM %s: %s, %d+%d tokens, %.2f s, $%.6f", purpose, model, pt, ct, dt, cost)
        return choice.message.content or "", info

    @staticmethod
    def _json(text: str) -> dict:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.S)
            try:
                return json.loads(m.group(0)) if m else {}
            except json.JSONDecodeError:
                return {}

    def extract_page_image(self, png: bytes) -> tuple[dict, dict]:
        b64 = base64.b64encode(png).decode()
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": EXTRACT_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}]
        text, info = self._chat(self.vision_model, msgs, True, "vision extraction", self.fallback_vision_model)
        return self._json(text), info

    def extract_page_text(self, page_text: str) -> tuple[dict, dict]:
        msgs = [{"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": page_text[:12000]}]
        text, info = self._chat(self.text_model, msgs, True, "text extraction", self.fallback_text_model)
        return self._json(text), info

    def write_summary(self, facts: dict) -> tuple[str, dict]:
        msgs = [{"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False, indent=1)}]
        return self._chat(self.text_model, msgs, False, "summary", self.fallback_text_model)

    def draft_profile(self, request: str) -> tuple[dict, dict]:
        msgs = [{"role": "system", "content": PROFILE_PROMPT},
                {"role": "user", "content": request[:24000]}]
        try:
            text, info = self._chat(self.text_model, msgs, True, "profile draft", self.fallback_text_model,
                                    max_completion_tokens=self.config.profile_max_completion_tokens)
        except Exception as e:
            # Groq's JSON mode refuses a reply that is not JSON (400 json_validate_failed): that is one
            # failed draft, which the next attempt can make up for, not a failed onboarding
            if "json_validate_failed" not in f"{e} {getattr(e, 'body', '')}":
                raise
            log.warning("LLM profile draft: the reply was not valid JSON (%s)", type(e).__name__)
            return {}, {"purpose": "profile draft", "model": self.text_model, "finish_reason": "invalid_json"}
        return self._json(text), info


def _first(*values: Optional[str]) -> str:
    """First value that is set; "" counts as set (it switches a fallback off)."""
    return next((v for v in values if v is not None), "")


_NUM = re.compile(r"\d[\d.,]*\d|\d")
_SCALE = re.compile(r"\s*(million|billion|thousand|juta|miliar|ribu|mn|bn|k)\b", re.I)


def numbers_in(text: str) -> set[str]:
    """Numbers mentioned in text, canonicalised: '17,238,178' -> '17238178'.
    Decimals stay as decimals ('17.2') and scaled figures ('17 million') are
    marked, so a rounded restatement of an amount never passes as a fact."""
    out = set()
    for m in _NUM.finditer(text):
        tok = m.group(0)
        if re.fullmatch(r"\d{1,3}([.,]\d{3})+", tok):
            val = re.sub(r"[.,]", "", tok)
        elif re.fullmatch(r"\d+[.,]\d+", tok):
            val = tok.replace(",", ".")
        else:
            val = tok
        if _SCALE.match(text, m.end()):
            val += "~scaled"
        out.add(val)
    return out


def unsupported_numbers(text: str, facts: dict) -> set[str]:
    """Numbers in `text` that do not appear anywhere in `facts` (hallucination guard)."""
    allowed = numbers_in(json.dumps(facts, ensure_ascii=False))
    allowed |= {str(i) for i in range(0, 32)}     # counts, ordinals, day numbers
    return {n for n in numbers_in(text) if n not in allowed}
