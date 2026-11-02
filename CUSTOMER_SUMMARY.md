# Supplier invoice reconciliation: what we built and what it found

**For:** Head of Accounts Payable, PT Nusantara Retail Group · **Batch:** PT Prima Kertas Indonesia, September 2026

## What it does

Your team receives each supplier's monthly invoices as a PDF and the ERP export as a spreadsheet. The system:

- reads every invoice and credit note in the PDF, including scanned pages
- compares each one, line by line, with what was booked in the ERP
- gives each document one status, with a plain-language reason and a suggested next step

It never changes a figure on its own. Every result shows exactly where each number came from (PDF page or ERP row), so a reviewer can check it in under a minute. The final decision on each document stays with your team.

## What it found in September

The supplier's 15 documents total **Rp 307,896,675**. The ERP shows **Rp 311,153,348**. The **Rp 3,256,673** difference is **fully explained, down to the rupiah**.

- **7 of 16 documents agree** and can be paid as booked. One differs by Rp 1 of rounding.
- **Rp 17,238,178 double payment prevented.** Invoice 0147 is booked twice in the ERP (E-10456 and E-10460).
- **Rp 1,786,591 of over-billing held** across five invoices:
  - 0144: 120 boxes billed vs 100 booked (Rp 527,250)
  - 0145: a higher unit price (Rp 666,000)
  - 0151: a delivery fee not on the order (Rp 277,500)
  - 0148: the supplier charged VAT on the full amount instead of the 11/12 base (Rp 204,841)
  - 0149: the invoice subtotal doesn't match its own lines (Rp 111,000)
- **Rp 15,688,629 not yet booked.** Invoice 0146 is a liability missing from the ERP.
- **Rp 3,493,714 booked without an invoice.** ERP entry for invoice 0155, currently parked.
- **One reference to fix.** Invoice 0154 quotes PO-88239, but the ERP has PO-88293. The amounts agree; the digits look swapped.

Each item has a ready action, for example "reverse duplicate entry E-10460 before the next payment run" or "ask the supplier for a credit note of Rp 527,250".

## How well it works

- **September:** all 16 documents got the correct status and cause. The batch runs in under 2 seconds.
- **14 simulated "next month" batches**, each with a deliberately planted error (deleted, duplicated or changed entries, wrong references, formatting changes): all 14 were caught, with nothing else disturbed.
- **Poor-quality scans** are the weak spot. Mildly degraded copies of the invoices were read correctly 13 times out of 14. Very poor copies could not be read automatically. In every test, a page the system could not read reliably was **sent to a person, never approved by mistake**.

## What it cannot do yet

- It is set up for one supplier layout (Prima Kertas). A new supplier's invoices can be read with AI assistance, but should be reviewed manually for their first month.
- It assumes the supplier uses your ERP item codes. Suppliers with their own codes need a code-mapping table.
- Very poor scans need the AI reading option switched on, or manual entry.
- Approvals are saved locally. There are no user logins or two-person sign-off yet.

## What we recommend next

1. **Pilot on October with 3–5 suppliers**, including one that sends scanned invoices. Measure reviewer time against the current two days.
2. **Onboard the remaining suppliers in waves**, one month in "review everything" mode each, then switch to auto-approval for documents that fully agree.
3. **Add logins, two-person approval above an amount threshold, and an export of approved amounts** to the payment run.
4. **Connect directly to the ERP** instead of monthly exports, so duplicates and missing bookings are caught as they happen.
