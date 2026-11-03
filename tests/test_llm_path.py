"""The LLM path with a fake model (no key, no network): what the pipeline does with
good, malformed and failing replies, and how GroqLLM accounts for cost."""
import groq
import httpx
import pymupdf
import pytest

from recon.config import LLMConfig, load_profiles
from recon.extract import extract_pdf
from recon.extract.arbitrate import second_read_scanned
from recon.extract.llm_reader import parse_page_from_llm
from recon.extract.page import PageParse
from recon.llm import GroqLLM
from recon.reconcile import extraction_failure_results
from recon.summary import deterministic_summary, summarise
from tests.fakes import FakeGroqClient, FakeLLM

PAGE = {"doc_type": "INVOICE", "vendor": "PT CONTOH JAYA", "doc_no": "CJ/INV/001", "doc_date": "05/10/2026",
        "lines": [{"line_no": 1, "sku": "A-1", "description": "Kertas HVS", "qty": "10", "uom": "RIM",
                   "unit_price": "50000", "disc_pct": None, "amount": "500000"}],
        "subtotal": "500000", "dpp": "458333", "ppn": "55000", "total": "555000"}


@pytest.fixture
def unknown_layout_pdf(tmp_path):
    """One text-layer page from a supplier no profile matches."""
    path = tmp_path / "contoh.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    text = ["PT CONTOH JAYA", "INVOICE CJ/INV/001 tanggal 05/10/2026",
            "Kepada PT Pembeli Sejahtera, Jalan Merdeka nomor 1, Jakarta",
            "1 A-1 Kertas HVS ukuran A4 sepuluh rim 10 RIM 50000 500000",
            "Subtotal 500000 DPP 458333 PPN 55000 Total 555000",
            "Pembayaran paling lambat tiga puluh hari setelah tanggal invoice ke rekening perusahaan"]
    for i, line in enumerate(text):
        page.insert_text((72, 100 + 18 * i), line)
    doc.save(path)
    return path


@pytest.fixture
def profiles(config_dir):
    return load_profiles(config_dir)


# --------------------------------------------------------------------------- unknown layout -> LLM

def test_unknown_layout_page_is_read_by_the_llm(unknown_layout_pdf, profiles):
    llm = FakeLLM(page=PAGE)
    docs, logs = extract_pdf(unknown_layout_pdf, profiles, llm=llm)
    assert llm.calls == ["text extraction"]
    assert [(d.doc_no, d.method, d.total) for d in docs] == [("CJ/INV/001", "llm_text", 555000)]
    assert any("extracted by LLM" in n for n in docs[0].extraction_notes)


def test_failed_llm_read_leaves_the_page_unread_not_the_batch_dead(unknown_layout_pdf, profiles):
    docs, logs = extract_pdf(unknown_layout_pdf, profiles, llm=FakeLLM(fail=TimeoutError("slow")))
    assert docs == []
    assert [lg.role for lg in logs] == ["unassigned"]
    assert any("LLM read failed (TimeoutError)" in n for n in logs[0].notes)
    [r] = extraction_failure_results(logs)
    assert (r.status, r.reasons, r.review_required) == ("NEEDS_REVIEW", ["EXTRACTION_FAILED"], True)


def test_unavailable_llm_is_never_called(unknown_layout_pdf, profiles):
    llm = FakeLLM(page=PAGE, available=False)
    docs, logs = extract_pdf(unknown_layout_pdf, profiles, llm=llm)
    assert llm.calls == [] and docs == []
    assert any("no LLM configured" in n for n in logs[0].notes)


def test_reply_outside_the_schema_is_noted_not_read(unknown_layout_pdf, profiles):
    docs, logs = extract_pdf(unknown_layout_pdf, profiles, llm=FakeLLM(page={"doc_no": "CJ/INV/001", "lines": "two"}))
    assert docs == []
    assert any("did not match the extraction schema" in n for n in logs[0].notes)


# --------------------------------------------------------------------------- scanned page: OCR vs vision

def _scanned(unknown_layout_pdf):
    return pymupdf.open(unknown_layout_pdf)[0]


def test_vision_read_agreeing_with_ocr_raises_the_method(unknown_layout_pdf, profiles):
    ocr = parse_page_from_llm(PAGE, 1, "t.pdf", "ocr")
    pp = second_read_scanned(ocr, _scanned(unknown_layout_pdf), 1, "t.pdf", FakeLLM(page=PAGE), profiles[0])
    assert pp is ocr and pp.method == "ocr+llm_vision"


def test_vision_read_disagreeing_with_ocr_is_flagged(unknown_layout_pdf, profiles):
    ocr = parse_page_from_llm(PAGE, 1, "t.pdf", "ocr")
    misread = {**PAGE, "total": "565000"}
    pp = second_read_scanned(ocr, _scanned(unknown_layout_pdf), 1, "t.pdf", FakeLLM(page=misread), profiles[0])
    assert pp is ocr and pp.method == "ocr"
    assert any(n.startswith("EXTRACTION_CONFLICT") and "total: 555000 vs 565000" in n for n in pp.notes)


def test_vision_read_failure_falls_back_to_ocr(unknown_layout_pdf, profiles):
    ocr = PageParse(page=1, method="ocr")
    pp = second_read_scanned(ocr, _scanned(unknown_layout_pdf), 1, "t.pdf", FakeLLM(fail=ConnectionError()), profiles[0])
    assert pp is ocr and pp.notes == ["LLM vision read failed (ConnectionError); OCR only"]


# --------------------------------------------------------------------------- summary

def test_summary_using_only_facts_is_accepted(september_batch):
    llm = FakeLLM(summary=lambda f: f"{f['supplier']}: the difference of {f['difference']} is fully explained.")
    text, source = summarise(september_batch, llm)
    assert text.endswith("-Rp 3,256,673 is fully explained.")
    assert source == "LLM (fake-text), numbers verified against facts"


def test_summary_with_an_invented_number_is_rejected(september_batch):
    text, source = summarise(september_batch, FakeLLM(summary="About Rp 3.3 million is still open."))
    assert text == deterministic_summary(september_batch)
    assert source.startswith("deterministic (LLM summary rejected: unsupported numbers")


def test_failing_or_missing_llm_gives_the_deterministic_summary(september_batch):
    expected = deterministic_summary(september_batch)
    assert summarise(september_batch, FakeLLM(fail=TimeoutError())) == (expected, "deterministic (LLM unavailable: TimeoutError)")
    assert summarise(september_batch, FakeLLM(summary="x", available=False)) == (expected, "deterministic")
    assert summarise(september_batch, None) == (expected, "deterministic")


# --------------------------------------------------------------------------- GroqLLM, SDK faked

def test_groq_cost_and_json_recovery():
    cfg = LLMConfig(text_model="t", vision_model="v", fallback_text_model="", fallback_vision_model="",
                    prices_usd_per_million_tokens={"t": {"input": 0.15, "output": 0.60}, "v": {"input": 0.2, "output": 0.8}})
    llm = GroqLLM(cfg, api_key="test-key", text_model="t", vision_model="v")
    llm._client = FakeGroqClient('Here you go: {"doc_no": "A/1"} hope that helps', prompt_tokens=2000, completion_tokens=500)
    data, info = llm.extract_page_text("page text")
    assert data == {"doc_no": "A/1"}
    assert info["purpose"] == "text extraction" and info["model"] == "t"
    assert llm.usage.cost_usd == pytest.approx(2000 / 1e6 * 0.15 + 500 / 1e6 * 0.60)
    assert llm._client.requests[0]["response_format"] == {"type": "json_object"}


def test_groq_unparseable_reply_reads_as_empty():
    llm = GroqLLM(LLMConfig(), api_key="test-key")
    llm._client = FakeGroqClient("no json here {broken")
    data, _ = llm.extract_page_text("page text")
    assert data == {}
    assert parse_page_from_llm(data, 1, "t.pdf", "llm_text").doc_no == ""


# --------------------------------------------------------------------------- fallback model when rate-limited

def _groq_error(cls, status: int, message: str):
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return cls(message, response=httpx.Response(status, request=req), body=None)


RATE_LIMIT = _groq_error(groq.RateLimitError, 429, "Rate limit reached for model openai/gpt-oss-120b")
MODELS = dict(text_model="t", vision_model="v", fallback_text_model="t-small", fallback_vision_model="v-alt")
FALLBACK_CFG = LLMConfig(**MODELS, prices_usd_per_million_tokens={
    "t": {"input": 0.15, "output": 0.60}, "v": {"input": 0.08, "output": 0.40},
    "t-small": {"input": 0.075, "output": 0.30}, "v-alt": {"input": 0.80, "output": 4.00}})


def _groq(fail, **models):
    # models passed explicitly: a .env loaded by another test may set GROQ_*_MODEL
    llm = GroqLLM(FALLBACK_CFG, api_key="test-key", **{**MODELS, **models})
    llm._client = FakeGroqClient('{"name": "PT X"}', prompt_tokens=1000, completion_tokens=100, fail=fail)
    return llm


def test_rate_limited_text_model_falls_back_and_is_priced_as_the_fallback():
    llm = _groq({"t": RATE_LIMIT})
    data, info = llm.draft_profile("request")
    assert data == {"name": "PT X"}
    assert [r["model"] for r in llm._client.requests] == ["t", "t-small"]
    assert (info["model"], info["fallback_from"]) == ("t-small", "t")
    assert llm.usage.calls == 1 and llm.usage.cost_usd == pytest.approx(1000 / 1e6 * 0.075 + 100 / 1e6 * 0.30)
    assert llm.usage.log[0]["fallback_from"] == "t"


def test_rate_limited_vision_model_falls_back_to_the_vision_fallback():
    llm = _groq({"v": RATE_LIMIT})
    _, info = llm.extract_page_image(b"png")
    assert info["model"] == "v-alt" and llm._client.requests[1]["messages"] == llm._client.requests[0]["messages"]


def test_tokens_per_minute_413_counts_as_a_rate_limit():
    too_big = _groq_error(groq.APIStatusError, 413, "Request too large for model `t` on tokens per minute (TPM): "
                                                    "Limit 8000, Requested 9500")
    assert _groq({"t": too_big}).extract_page_text("page")[1]["model"] == "t-small"


def test_other_errors_do_not_fall_back():
    auth = _groq_error(groq.AuthenticationError, 401, "Invalid API Key")
    llm = _groq({"t": auth})
    with pytest.raises(groq.AuthenticationError):
        llm.extract_page_text("page")
    assert [r["model"] for r in llm._client.requests] == ["t"]


def test_fallback_also_limited_or_switched_off_raises():
    with pytest.raises(groq.RateLimitError):
        _groq({"t": RATE_LIMIT, "t-small": RATE_LIMIT}).extract_page_text("page")
    llm = _groq({"t": RATE_LIMIT}, fallback_text_model="")
    with pytest.raises(groq.RateLimitError):
        llm.extract_page_text("page")
    assert len(llm._client.requests) == 1
