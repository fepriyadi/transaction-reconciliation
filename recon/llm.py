"""Groq wrapper. Used for exactly three things:

1. Second read of scanned pages (vision) — cross-checked against OCR.
2. Extraction of pages from suppliers with no layout profile yet (text/vision).
3. Wording the AP-manager/CFO summary from already-computed facts.

Never used for arithmetic, matching, status or per-document explanations.
Every LLM output is treated as untrusted: extraction results go through the
same deterministic checks as OCR/text, and the summary is rejected if it
contains any number that is not in the computed facts.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, ValidationError

from .config import LLMConfig

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


@dataclass
class LLMUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    cost_usd: float = 0.0
    log: list = field(default_factory=list)


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

class GroqLLM:
    def __init__(self, config: LLMConfig | None = None, api_key: str | None = None, text_model: str | None = None,
                 vision_model: str | None = None):
        cfg = config or LLMConfig()
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.text_model = text_model or os.getenv("GROQ_TEXT_MODEL") or cfg.text_model
        self.vision_model = vision_model or os.getenv("GROQ_VISION_MODEL") or cfg.vision_model
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

    def _chat(self, model: str, messages: list, json_mode: bool, purpose: str) -> tuple[str, dict]:
        t0 = time.time()
        kw = dict(model=model, messages=messages, temperature=0)
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
        info = {"purpose": purpose, "model": model, "prompt_tokens": pt, "completion_tokens": ct,
                "seconds": round(dt, 2), "cost_usd": round(cost, 6)}
        self.usage.log.append(info)
        return resp.choices[0].message.content or "", info

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
        text, info = self._chat(self.vision_model, msgs, True, "vision extraction")
        return self._json(text), info

    def extract_page_text(self, page_text: str) -> tuple[dict, dict]:
        msgs = [{"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": page_text[:12000]}]
        text, info = self._chat(self.text_model, msgs, True, "text extraction")
        return self._json(text), info

    def write_summary(self, facts: dict) -> tuple[str, dict]:
        msgs = [{"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False, indent=1)}]
        return self._chat(self.text_model, msgs, False, "summary")


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
