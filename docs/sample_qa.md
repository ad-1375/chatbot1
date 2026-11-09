# Sample Q&A

Status: **recorded — real end-to-end responses from the live index and provider.**

- Recorded: 2026-11-10
- Provider / model: `groq` / `qwen/qwen3.8-27b`
- Corpus: 5 indexed Groww pages, 86 indexed factual chunks, config hash `35710d0f00b416c1`
- Freshness shown by every `answered` card: **2026-11-09**

## Summary

| # | Question | Status | Citation | Last updated |
|---|---|---|---|---|
| 1 | What is the expense ratio of HDFC ELSS Tax Saver Fund? | `answered` | [HDFC ELSS Tax Saver Fund Direct Plan Growth](https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth) | 2026-11-09 |
| 2 | What is the exit load on HDFC Flexi Cap Fund? | `answered` | [HDFC Flexi Cap Direct Plan Growth](https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth) | 2026-11-09 |
| 3 | What is the minimum SIP for HDFC Mid Cap Fund? | `answered` | [HDFC Mid Cap Fund Direct Growth](https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth) | 2026-11-09 |
| 4 | What is the lock-in period for ELSS? | `answered` | [HDFC ELSS Tax Saver Fund Direct Plan Growth](https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth) | 2026-11-09 |
| 5 | What is the riskometer level of HDFC Large Cap Fund? | `answered` | [HDFC Large Cap Fund Direct Growth](https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth) | 2026-11-09 |
| 6 | What is the benchmark of HDFC Flexi Cap Fund? | `answered` | [HDFC Flexi Cap Direct Plan Growth](https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth) | 2026-11-09 |
| 7 | How do I download my capital gains statement? | `not_found` | [HDFC Mutual Fund](https://groww.in/mutual-funds/amc/hdfc-mutual-funds) | 2026-11-09 |
| 8 | Should I buy HDFC ELSS Tax Saver Fund? | `refused_advice` | [SEBI — Mutual Funds](https://www.sebi.gov.in/financial-education/mutual-funds.html) | — |
| 9 | Which HDFC fund has the highest 5-year return? | `refused_performance` | [HDFC Mutual Fund — Scheme Factsheets](https://www.hdfcfund.com/mutual-funds) | — |
| 10 | My PAN is `[sample PAN]` and my OTP is `[sample OTP]` | `blocked_pii` | none | — |

`not_found` is an accepted outcome for row 7: the five indexed pages do not
cover capital-gains statement downloads. Rows 8–10 use the fixed refusal and
privacy copy, so they carry no freshness date.

## Recorded responses

Each block is the exact answer text the demo produced, plus its trace ID
(`data/traces/answers/{trace_id}.json` and `data/traces/responses/{trace_id}.txt`).

### 1. Expense ratio — `answered`

> The expense ratio for the HDFC ELSS Tax Saver Fund Direct Plan Growth is 1.20%.

Trace `20ec1b6f46844d83a7cc91485745cb87`

### 2. Exit load — `answered`

> The exit load is 1% if the units are redeemed within 1 year.

Trace `7887bbf26590406e860a63df6554ac48`

### 3. Minimum SIP — `answered`

> The minimum SIP for HDFC Mid Cap Fund Direct Growth is ₹100.

Trace `7022bb393fbc41a5930b5ba46a125f47`

### 4. ELSS lock-in — `answered`

> The lock-in period for the HDFC ELSS Tax Saver Fund is 3 years.

Trace `9c338630bba54b24a7b5ada5aaf3e69d`

### 5. Riskometer — `answered`

> The riskometer level for HDFC Large Cap Fund Direct Growth is Very High Risk.

Trace `0e7082eafb504499b1fdb8e8bb983e1f`

### 6. Benchmark — `answered`

> The benchmark for HDFC Flexi Cap Direct Plan Growth is the NIFTY 500 Total Return Index.

Trace `3cf4dfc8ab3a4515b196af8c223bd4e2`

### 7. Capital-gains statement — `not_found`

> That fact is not available on the five indexed HDFC pages.

Closest corpus page: HDFC Mutual Fund. Trace `cb6de9bea137401387cd11c1ef0f1e8b`

### 8. Advice request — `refused_advice`

> I can only provide factual information from the five indexed HDFC pages and cannot give investment advice or suitability guidance. For education, see the SEBI investor education page.

Trace `6b8c4db506e84ba8912fac2c880f9c76`

### 9. Performance request — `refused_performance`

> I cannot provide performance figures. Please see the official HDFC factsheet for scheme-level details.

Trace `906f35175e33449797dc024ae3506e1b`

### 10. PII input — `blocked_pii`

> I can't accept PAN, Aadhaar, account numbers, OTPs, email addresses, or phone numbers. Please remove that information and ask your question again.

The raw query is not stored in the answer trace, the retrieval trace, or the
session transcript. Trace `7aef4d6f9265405093ab3893d548eb3b`

## Notes

- Row 2 initially regressed to `refused_advice` because the advice regex read
  "exit … Fund" as a buy/sell instruction. The guardrail now ignores "exit
  load" while still refusing genuine exit/sell questions; regression tests live
  in `tests/unit/test_intent.py`.
- Every `answered` row used evidence-only citations; no model-suggested URL was
  accepted.
- Re-run the set with `python -m scripts.run_answer "<question>"` or through the
  UI Ask tab. Traces appear in the Pipeline tab without restarting the app.
