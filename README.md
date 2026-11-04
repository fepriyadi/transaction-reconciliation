# Supplier invoice ↔ ERP reconciliation (PT Nusantara Retail Group)

Reads a batch of supplier invoices and credit notes from a PDF, including scanned pages. Matches each one against the ERP AP export at document and line level. Gives every document one status, reason codes, a plain-language explanation and a suggested action. Shows that the batch difference is fully explained, and traces every figure to a PDF page and box or to an ERP sheet and cell. A Streamlit review queue leaves the final decision to a person.

On the September sample: **16/16 documents get the right status and cause**. The batch difference of **−Rp 3,256,673 is fully explained (Rp 0 unexplained)**. It runs in **under 2 s** with no LLM.

## Run it (5 commands)

```bash
brew install tesseract                     # OCR for scanned pages (Linux: apt install tesseract-ocr)
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install --no-deps -e .   # installs src/recon and the `recon` command
cp .env.example .env                       # optional: put a GROQ_API_KEY in it
recon ui                                   # UI: run the sample, or upload a new batch
```

A new batch (e.g. the October files in the demo) needs no code change:

```bash
recon run --pdf Supplier_Invoices_*_Oct2026.pdf --erp ERP_AP_Export_Oct2026.xlsx --out output/oct2026
recon check                                # validate config/ (profiles, rules, ERP mapping, LLM models)
recon eval                                 # writes eval/RESULTS.md (add --llm to evaluate the Groq vision path)
```

`recon` is the single entry point (`python -m recon` is the same). `recon ui` starts Streamlit and passes any extra arguments to it. The old `python run_batch.py ...` still works as an alias of `recon run ...`. `recon run -v` logs each step and warning, and `-vv` logs every LLM call with its tokens and cost.

Development checks (CI runs the same on every push, plus a Docker build):

```bash
pip install -r requirements-dev.txt
ruff check && mypy && pytest -q            # lint, types, 222 tests
```

With Docker (tesseract included):

```bash
docker build -t recon .
docker run --rm -p 8501:8501 --env-file .env -v "$PWD/output:/app/output" recon
```

Outputs in `--out`:

- `reconciliation_report.xlsx` with these sheets: Summary, Documents, Line_Discrepancies, Control_Totals, Bridge_by_Document, Arithmetic_Checks, Audit_Trail, Extraction_Log, ERP_Load, Reason_Codes (the status policy: severity, forced status and whether each code blocks auto-approval)
- the same core tables as CSV: `document_report.csv` (the Documents sheet), `line_discrepancies.csv`, `audit_trail.csv`
- `customer_summary.txt`

## September results

| Document | Status | Difference | Cause |
|---|---|---:|---|
| 0141 | MATCHED | 0 | |
| 0142 | MATCHED | 0 | ERP wrote the number as `PKI-INV-2026-09-0142` (format only) |
| 0143 | MATCHED_WITHIN_TOLERANCE | −1 | ERP PPN 1,749,259 vs rule 1,749,258 (rounding) |
| 0144 | AMOUNT_MISMATCH | +527,250 | AMP-110 invoiced 120 boxes, ERP 100 |
| 0145 | AMOUNT_MISMATCH | +666,000 | PK-A4-80-RM price 52,500 vs 49,500 |
| 0146 | MISSING_IN_ERP | +15,688,629 | Not booked |
| 0147 | DUPLICATE_IN_ERP | −17,238,178 | Booked twice (E-10456, E-10460) |
| 0148 | NEEDS_REVIEW | +204,841 | Invoice charged PPN on the full subtotal (DPP not 11/12) |
| 0149 | NEEDS_REVIEW | +111,000 | Invoice subtotal is Rp 100,000 above the sum of its lines |
| 0150 | MATCHED | 0 | Scanned page (p.10), read by OCR, all checks pass |
| 0151 | AMOUNT_MISMATCH | +277,500 | Delivery fee line not in ERP |
| 0152 | MATCHED | 0 | ERP date and total stored as text (normalised) |
| 0153 | MATCHED | 0 | 2 pages, 34 lines, different line order |
| 0154 | NEEDS_REVIEW | 0 | PO-88239 on invoice vs PO-88293 in ERP |
| CN 0012 | MATCHED | 0 | Credit note, −972,360 on both sides |
| 0155 | MISSING_IN_PDF | −3,493,714 | Parked ERP entry with no invoice |

The batch bridge:

```text
PDF total            307,896,675
ERP total            311,153,348
difference           −3,256,673
  not booked in ERP  +15,688,629      ERP without document   −3,493,714
  duplicate ERP      −17,238,178      line differences       +1,325,000
  invoice subtotal   +100,000         PPN on line diffs      +156,750
  invoice PPN calc   +204,841         ERP PPN rounding       −1
unexplained          0
```

## Architecture

```text
config/          supplier profiles, rules, ERP mapping, LLM models (validated by src/recon/config.py)
data/            the September sample
src/recon/       the package: everything `recon run` does
app.py           Streamlit UI (`recon ui`)
eval/            evaluation suites (`recon eval`)
tests/           pytest, including a fake LLM (tests/fakes.py)
```

```text
config/*.json ──► recon.config (schemas, validated once per run) ──► passed to every step below

PDF ──► extract ──► Documents(PDF) ──┐
         text layer / tesseract OCR   │
         supplier profile (JSON)      ├─► matching ──► reconcile ──► report (xlsx/csv) ──► Streamlit UI
         [Groq vision 2nd read]       │   doc key,     status from     audit trail         review queue
XLSX ──► erp_loader ──► Documents(ERP)┘   PO+total,    reason_codes,                       decision log
         sheets classified by header       lines by     bridge,                            (review)
                                           SKU          explain
```

| Module | Responsibility |
|---|---|
| `src/recon/normalize.py` | Canonical document numbers, SKUs, vendors; numbers in ID/US formats and OCR noise; dates (DMY/MDY, ambiguity) |
| `src/recon/rules.py` | R1–R5 with exact `Fraction` arithmetic and explicit half-up rounding; R7 self-consistency checks |
| `src/recon/config.py` | Pydantic schema for every file in `config/`; loaded and validated once per run, so a broken profile fails at load with the file and field named |
| `src/recon/extract/` | PDF → documents: `words` (text layer, OCR, deskew), `profile_parser` (labels + row grammar), `llm_reader` (schema-checked LLM reply), `arbitrate` (OCR retries, OCR vs vision), `assemble` (multi-page stitching, confidence) |
| `src/recon/erp_loader.py` | Reads **all** sheets, classifies them by header synonyms (`config/erp_mapping.json`), keeps `sheet!cell` per value |
| `src/recon/matching.py` | Document groups (duplicates kept, secondary PO+total key), line matching by SKU in three passes |
| `src/recon/reason_codes.py` | The status policy: every reason code with its severity, the status it forces, its precedence and the figures it highlights. Status, review requirement, queue order and audit highlighting are all read from here |
| `src/recon/reconcile.py` | Reason codes per document, typed difference `Bridge` and `ControlTotals`. R7 results are held on the result (`DocChecks`); input documents are never modified |
| `src/recon/explain.py` | Template explanations, actions and headlines |
| `src/recon/summary.py` | CFO summary: computed facts, the deterministic wording, and the LLM wording, which is used only if every number in it is one of the facts |
| `src/recon/pipeline.py` | `run_batch` runs the steps in order (extract → ERP → match → reconcile → totals → summary) and times them; each step is its own function |
| `src/recon/review.py` | Append-only reviewer decision log per batch; a decision made on different facts is reported as stale |
| `src/recon/llm.py` | The interfaces the pipeline depends on: `PageReader` (extraction), `SummaryWriter` (summary), `ProfileDrafter` (layout profile drafts), and `LLMClient`, which is all three. `GroqLLM` implements them, and `tests/fakes.py` has a fake LLM for tests. Also the `LLMPage` reply schema and the hallucination guard |
| `src/recon/profile_draft.py` | A new layout's profile: the LLM drafts it, a dry run puts it through `extract_pdf` on the same pages and compares every figure with the LLM's read and R1–R4, and it is written to `config/suppliers/` only when a reviewer confirms |
| `src/recon/report.py`, `src/recon/render.py` | Excel/CSV and every table the UI shows; a PDF page drawn with its figure boxes |
| `src/recon/cli.py`, `src/recon/uploads.py`, `app.py` | The single entry point `recon` (`run`, `ui`, `check`, `eval`), content-addressed upload storage, Streamlit UI (screen layout only) |
| `config/` | `rules.json` (tax factors, tolerances), `erp_mapping.json`, `suppliers/*.json` (one per layout), `supplier_register.json`, `llm.json` (models and prices; the code's defaults equal this file) |

## Where the LLM is used, and where it is not

The rule is: **code decides; the LLM reads and words.**

| Task | Done by | Why |
|---|---|---|
| Arithmetic, R1–R7 checks, matching, status, bridge | Code | Must be exact, repeatable and explainable to an auditor |
| Per-document explanation and action | Code (templates) | The same facts always give the same sentence, and every number in it comes from a computed figure |
| Text-layer pages of a known supplier | Code (profile) | The text layer is exact, so an LLM would only add risk and cost |
| Scanned pages | Tesseract + **Groq vision second read** | Two independent reads. If they agree, confidence is high. If they disagree, the read whose own arithmetic holds is used; if that doesn't settle it, the document is flagged `EXTRACTION_CONFLICT` |
| Pages of a supplier with no profile yet | **Groq** (text or vision) | Removes the layout-onboarding bottleneck for 40+ suppliers. Output is re-parsed and checked by code, and tagged `EXTRACTED_BY_LLM` for review |
| Drafting a new layout's supplier profile | **Groq** (text), on request in the UI | The LLM proposes the JSON; code dry-runs it on the same pages against the LLM's read and R1–R4, and a person confirms before it is saved. The reply is capped at `profile_max_completion_tokens` (4096, in `config/llm.json`) |
| AP-manager / CFO summary | **Groq**, optional | Wording only. The summary is rejected if it contains any number not present in the computed facts (rounded figures like "17.2 million" are rejected too). A deterministic summary is the fallback |

Without a `GROQ_API_KEY` everything still runs, fully deterministically.

## Key decisions and trade-offs

- **Profiles are config, not code.** A supplier layout is a JSON file: labels, value patterns and a row grammar (`qty uom price [disc] amount`, read from the right). The same parser runs on text-layer words and on OCR word boxes. This is why October runs without code changes.
- **Lines match by SKU, never by position.** The ERP sorts 0153's 34 lines differently, and repeated SKUs with different discounts are paired exactly first.
- **One identity for the bridge.** Each document's difference splits into named parts that add up by construction: line differences, the invoice's own subtotal error, PPN on subtotal differences, PPN calculation deviations on each side, and total errors. So "unexplained = 0" is a real check, not a plug.
- **R7 is applied to both sides.** A document's own numbers are checked against the *stated* previous figure, so one error is reported once instead of cascading. A tax deviation ≤ IDR 10 is classed as rounding (`ERP_TAX_ROUNDING`), not an error. That is what makes 0143 `MATCHED_WITHIN_TOLERANCE` rather than `NEEDS_REVIEW`.
- **Status precedence:** duplicate → missing → NEEDS_REVIEW (own arithmetic, key references, extraction) → AMOUNT_MISMATCH → within tolerance → matched. 0148 and 0149 also differ in amount, but they are NEEDS_REVIEW: the supplier's figures are wrong, so "pay the difference" is the wrong action. The precedence and each code's effect live in one table (`src/recon/reason_codes.py`); an unregistered code fails loudly instead of being classified by its name.
- **Configuration is validated, not trusted.** Every file in `config/` has a schema. A typo such as `foter_margin`, an invalid regex or a row grammar without an `amount` column stops the run before any page is read.
- **Sign convention:** credit notes are stored negative on both sides (the PDF prints them positive) so batch totals add up.
- **Secondary matching is never silent.** If document numbers differ but type, PO and total agree on exactly one candidate, the pair is made and flagged `MATCHED_BY_SECONDARY_KEY`.
- **No page is dropped.** A page that yields no document becomes its own `NEEDS_REVIEW` row (`EXTRACTION_FAILED`).

## Confidence scores and how they are used

- **Text layer:** 0.99.
- **OCR:** 0.6 + 0.3 × (lowest tesseract confidence among figure-bearing words).
  - Raised to **0.9 when every R1–R4 equation on the page holds**: a misread digit would have to keep all of them true by coincidence.
  - Capped at 0.5 when they don't.
- **OCR and vision agree:** 0.97.
- **LLM only:** 0.85.
- **Structural problems** (gaps in line numbers, missing total) cap it at 0.3–0.5.
- **ERP corroboration:** if every line and the total agree with the ERP (an independent source), the document's confidence is raised to 0.95.
- **Secondary-key matches:** capped at 0.6.

Use: a document is auto-approvable only if its status is MATCHED or MATCHED_WITHIN_TOLERANCE **and** confidence ≥ `auto_approve_min_confidence` (0.9, in `config/rules.json`) **and** its supplier is past the new-supplier period (see below). Otherwise it goes to the review queue with `EXTRACTION_LOW_CONFIDENCE` or `NEW_SUPPLIER`. With real volumes, I would calibrate the threshold on reviewer decisions: track how many auto-approved documents a human later overturns.

## Onboarding a new supplier layout safely

Enforced in code ([src/recon/onboarding.py](src/recon/onboarding.py)): every document of a supplier's first month goes to review, whether it was read by a layout profile or by the LLM. A supplier's go-live date is in `config/supplier_register.json`; documents dated within `new_supplier_review_days` (31, in `config/rules.json`) of it get `NEW_SUPPLIER` and are held for a person, even when they match the ERP exactly. A supplier missing from the register is always held. The status itself is not changed: a matching document stays MATCHED, it just can't be auto-approved.

The process around it (steps 3–4 are manual today):

1. Run its first batch with the LLM path. A supplier not in `config/supplier_register.json` is always held, so every document goes to review.
2. Draft `config/suppliers/<supplier>.json`: detection keywords, header labels and patterns, row grammar and decimal separator. In the UI (Extraction log → *New layout → supplier profile*) the LLM drafts it from the batch's pages and the reviewer picks the supplier's name in the ERP export. Code then reads the same pages with the draft and compares every figure with the LLM's read and with R1–R4; problems go back to the LLM for one revision. The reviewer can also edit the draft's JSON and re-check it; a saved profile then records that it was edited. The draft cannot be saved while a page goes unread, a date is not understood, or another supplier's page prints its detect text. Disagreements are drawn in red on the page and must be looked at. On confirmation it is written locally with who confirmed it, the supplier is added to `config/supplier_register.json` with that day as its go-live date (a supplier already registered keeps its date), and the batch is read again with it. `recon check` checks it against the schema.
3. Freeze 2–3 reviewed invoices as golden files and add an expected-results test like `tests/test_september_batch.py`.
4. Shadow mode for one cycle: the profile and LLM reads run side by side. The profile is promoted only when they agree and every arithmetic check passes.
5. Afterwards, any drift (a row-grammar miss, a gap in line numbers, failed checks on many documents) shows up as low confidence. Documents are held, not mis-approved.

Worked example: `config/suppliers/sagara_digital_supply.json` was drafted this way from a dummy October 2026 batch (`Supplier_Invoices_PT_Sagara_Digital_Supply_Oct2026_DUMMY.pdf`, 11 pages) by `openai/gpt-oss-120b`. The dry run had 0 disagreements, and it was confirmed on 2026-11-03, which added Sagara to `config/supplier_register.json` with that go-live date. Re-read with the profile, the batch gives 12 documents: 5 MATCHED, 1 MISSING_IN_ERP, 1 DUPLICATE_IN_ERP, 3 NEEDS_REVIEW, 1 AMOUNT_MISMATCH and 1 MISSING_IN_PDF, with the −Rp 3,047,150 difference fully explained. Every document dated before 2026-12-04 (go-live + 31 days) is in the new-supplier period, so all 12 carry `NEW_SUPPLIER` and none can be auto-approved. Steps 3–4 have not been done for it.

## Cost and runtime per batch

- **Deterministic path (measured):** 1.5–1.8 s for the 16-page September batch on a laptop. About 1 s of that is the OCR of the one scanned page, and a median-filter retry only runs when the first read doesn't add up. Cost: $0.
- **With Groq (estimated, not measured here),** at the models and list prices in `config/llm.json`:
  - one vision call per scanned page (`meta-models/muse-spark-open`, $0.08 / $0.40 per million input / output tokens; roughly 2.5k in + 1k out, ≈ $0.0006)
  - one summary call (`openai/gpt-oss-120b`, $0.15 / $0.60; roughly 1.5k in + 0.3k out, ≈ $0.0004)
  - so **about $0.001 per supplier batch** with one scanned page. Even if every page of 40 suppliers' 16-page batches went to the LLM, it stays under $0.50 a month
- **Fallback models when rate-limited:** a call that gets a rate limit (429, or Groq's 413 over the tokens-per-minute limit) is repeated once on `fallback_text_model` (`openai/gpt-oss-20b`, $0.075 / $0.30) or `fallback_vision_model` (`qwen/qwen3.8-27b`, $0.80 / $4.00, a Groq preview model that accepts images). Other errors are not retried. The cost is booked at the fallback's price, and the run log names the model that answered.
- The app shows actual calls, tokens, seconds and cost for each run (Overview → Run statistics). Prices are in `config/llm.json`; the config check refuses a configured model without a price, so the estimate can't silently read $0.

## Evaluation (full results in [eval/RESULTS.md](eval/RESULTS.md))

1. **September ground truth:** 16/16 status and difference, 16/16 expected cause, Rp 0 unexplained.
2. **ERP mutation suite:** 14 simulated new batches. Each injects one known error into a copy of the ERP:
   - quantity, price or discount changed
   - entry deleted or duplicated
   - PO or credit-note reference changed
   - number typed differently
   - total stored as Indonesian-format text
   - PPN off by 4 (rounding) or 500 (error)
   - ambiguous `01/09/2026` date
   - an entry for another vendor
   - renamed columns and sheet

   **14/14** were caught with the expected status, no other document changed, and the batch stayed fully explained.
3. **Scan stress test:** all 15 text pages re-rendered as degraded scans and read with OCR only. **Zero silently wrong documents** at every degradation level. The failures (shown in full in RESULTS.md):
   - mild (200 dpi, 0.7°): 13/14 exact
   - medium (150 dpi, blur, speckle): mostly misread but caught by the checks
   - harsh (110 dpi, 2.5°, heavy noise): document numbers unreadable, so every page is flagged

   This is the case the Groq vision read exists for. `recon eval --llm` measures it, but that path was **not run here** because no API key was available in the build environment.

## Known limitations

- Two supplier profiles exist. Only `prima_kertas.json` has golden-file tests and the evaluation suites. `sagara_digital_supply.json` was drafted by the LLM and confirmed in the UI from one dummy batch, with no golden-file test yet. Other layouts depend on the LLM path. The LLM path has been used live for profile drafting and summaries, but its extraction accuracy has not been evaluated. Tests with a fake LLM (`tests/test_llm_path.py`) cover what the pipeline does with the replies: a page read correctly, a reply in the wrong shape, a call that fails, the OCR and vision reads agreeing or not, the summary accepted or rejected, and the cost calculation. How accurate a real model's reading is remains unmeasured.
- One layout per supplier. A page is matched to a profile by its `detect` text (the supplier's name), and the first matching profile wins (`detect_profile` in `src/recon/extract/profile_parser.py`). If a supplier sends pages in two different templates, for example old and new, in the same PDF or across batches, a second profile with the same `detect` text is never chosen. Pages in the other template are parsed with the wrong layout, and the LLM fallback does not step in because a profile did match. Nothing is dropped silently: a page that yields no document number becomes an `EXTRACTION_FAILED` row (NEEDS_REVIEW), and its ERP entry shows as `MISSING_IN_PDF`. Different suppliers' layouts in one PDF are supported, since the profile is chosen page by page.
- Line matching assumes the supplier's SKU equals the ERP item code. Suppliers with their own codes need a SKU mapping table (not built).
- On scans, the date and PO are not covered by any arithmetic check. A misread PO surfaces as `PO_MISMATCH`, which is safe. A misread date only raises a `DATE_MISMATCH` warning.
- A duplicate booked under a *different* document number appears as `MISSING_IN_PDF`. It is still flagged, but not labelled as a duplicate.
- Review decisions are an append-only local log per batch (`decisions.jsonl`). Each decision records the status, difference and blocking reason codes it was made on, and is shown as stale when a re-run changes them. There is still no login, no four-eyes approval and no tamper-proof storage.
- Assumes IDR, one tax regime per run, and one PO per invoice. No partial payments and no e-Faktur (tax invoice) validation.
- Ambiguous ERP text dates are resolved with the configured order (`MDY`) and accepted if either reading equals the PDF date.

## With another week

- Evaluate and tune the Groq vision path on real scans; calibrate the confidence thresholds against reviewer decisions.
- The rest of the onboarding wizard: golden-file tests and shadow mode before a drafted profile is activated (steps 3–4 above); saving a profile from a deployed container needs `config/suppliers/` on a mounted volume.
- Several templates per supplier: when more than one profile matches the supplier's name, choose the one whose own labels (document-number label, table headers, total labels) appear on the page.
- A SKU mapping table with LLM-suggested mappings that a person approves.
- Read the ERP through its API instead of exports; validate e-Faktur QR codes against DJP (the tax authority); export a payment proposal for approved amounts only.
- Multi-user review with login, an append-only decision log and a four-eyes rule above an amount threshold.

## How AI tools were used

> **Draft: edit this to describe your own process before submitting.**

I built this with Claude Code (Anthropic's coding agent) as a pair programmer. I set the scope and the rule that the LLM never does arithmetic. I had the agent inspect the sample files first: it found the scanned page, the duplicate, the text-typed ERP cells and so on before any code was written. I then reviewed and iterated on each module. The evaluation suites came from asking "how would this fail on October's files?". They exposed two real problems that were fixed before submission:

- OCR noise breaking the row grammar
- a scanned invoice going to review for low confidence when an amount mismatch was the more useful status

I can explain and change every part of the code.
