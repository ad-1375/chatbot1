# HDFC MF Facts Assistant

A facts-only FAQ assistant over exactly five public Groww pages:

1. HDFC AMC overview
2. HDFC Large Cap Fund
3. HDFC Flexi Cap Fund
4. HDFC ELSS Tax Saver Fund
5. HDFC Mid Cap Fund

Answers are short (≤3 sentences), factual, and carry exactly one source link plus a “Last updated from sources” date. The assistant refuses investment advice, portfolio questions, and return/performance questions, and blocks PAN, Aadhaar, account numbers, OTPs, emails, and phone numbers.

**Facts-only. No investment advice.**

## Status

Phase 1 (project scaffold, config, contracts, health check), Step 1
(ingestion), Step 2 (atomic chunking), Step 3 (embeddings), Step 4
(Chroma indexing), Step 5 (retrieval and answer logic), and Step 6
(Streamlit UI) are implemented.
Step 1 fetches exactly the five configured Groww pages, snapshots the raw HTML,
extracts cleaned text plus atomic blocks, scrubs corpus emails/phones, and
writes the load report, manifest, and coverage table. Step 2 turns those blocks
into context-prefixed atomic facts, keeps performance/boilerplate/navigation
content for audit but out of the index, and fails closed if a chunk invariant
breaks. Step 3 embeds only the indexed chunks with MiniLM, validates every
vector invariant before writing, and records a model/config manifest. Step 4
upserts those vectors into a persistent Chroma collection, flattens metadata for
filtering, verifies count and filter smoke tests, and blocks stale or
incompatible indexes unless `--rebuild` is used. Step 5 adds PII and intent
guardrails, scheme/field resolution, filtered retrieval, grounded generation
through the configured provider, validation with one repair and an extractive
fallback, one validated citation, and traces for every status. Step 6 adds the
Streamlit UI: an Ask tab with escaped response cards, a six-stage read-only
Pipeline inspector, a Sources tab, and an Evaluation tab. The UI keeps
conversation state in `st.session_state` only and never enables raw HTML.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

Fill `.env`:

```dotenv
LLM_PROVIDER=groq
LLM_MODEL=<your Groq model id>
GROQ_API_KEY=<your key>
```

Only the active provider's key is required. Supported providers: Groq (default), Gemini, OpenAI, Anthropic, Mistral. Provider switching is manual through `.env`; there is no automatic cross-provider retry in v1.

## Verify the scaffold

```powershell
python -m mf_faq.healthcheck
pytest tests/unit -q
```

`LLM_MODEL` and the active provider key are reported as `present` or `missing`; values are never printed. On a fresh install the artifact check prints “no artifacts yet (fresh install)”. After Steps 1–4 the artifact check reports matching document, chunk, embedding, and Chroma counts; only the LLM warnings remain until `.env` is filled.

## Ingest the five pages (Step 1)

```powershell
python -m scripts.run_ingest
pytest tests/unit/test_loader.py tests/unit/test_extractor.py tests/integration/test_load_pipeline.py -q
```

The run must load **5/5** pages. It writes:

```text
data/raw/{doc_id}/{timestamp}.html      raw snapshots
data/processed/documents.jsonl          five validated SourcePage rows
data/processed/blocks.jsonl             atomic blocks for the chunker
data/processed/load_report.md           status, renderer, hash, fact coverage
data/manifests/load_manifest.json       machine-readable manifest
```

Fetch policy: only the five configured URLs, no link discovery, 2-second delay,
3 retries with backoff, 30-second timeout, 5 MB cap, and redirects rejected
unless the final host remains on `groww.in`. `robots.txt` is checked once per
domain per run.

## Chunk into atomic facts (Step 2)

```powershell
python -m scripts.run_chunk
pytest tests/unit/test_taxonomy.py tests/unit/test_chunker.py tests/integration/test_chunk_pipeline.py -q
```

Step 2 requires the five Step 1 documents and blocks. It writes:

```text
data/chunks/chunks.jsonl        every chunk (indexed and audit-only)
data/chunks/excluded.jsonl      non-indexed chunks with exclusion reasons
data/chunks/chunk_stats.json    counts, token stats, per-page distributions
data/chunks/chunk_report.md     human-readable report and invariant checklist
```

Chunk rules: one `kv_pair` or `table_row` per chunk (never split), paragraphs
split only at sentence boundaries when over `max_tokens`, adjacent small
factual paragraphs merged only within the same section and field up to
`min_tokens`, and exact normalized duplicates removed within a page. Every
chunk is prefixed `{page} > {section} > {field}: {fact}`. Oversize atomic facts
stay whole with `oversize: true` and `indexed: false`.

Performance, boilerplate, and navigation content (returns, NAV, holdings,
ratings, similar funds, fund-manager biographies, contact details, legal text)
is retained in `chunks.jsonl`/`excluded.jsonl` for audit but never indexed.
The MiniLM tokenizer is used when `transformers` can load it locally;
otherwise Step 2 falls back to a deterministic word/punctuation counter and
records `tokenizer.fallback: true` in the stats.

The pipeline validates every `implementation.md` §4.6 invariant before writing:
exactly five pages, unique deterministic IDs, no indexed chunk over
`max_tokens`, no split key-value/table rows, a reason on every excluded chunk,
no cross-page/scheme chunks, and no indexed oversize chunk.

## Embed the chunks (Step 3)

```powershell
python -m scripts.run_embed
pytest tests/unit/test_embedder.py tests/integration/test_embed_pipeline.py -q
```

Step 3 requires the Step 2 `chunks.jsonl`, keeps only `indexed: true` rows, and
writes:

```text
data/embeddings/embeddings.npy            (N, 384) float32 L2-normalized rows
data/embeddings/embedding_ids.json        row i maps to the same chunk as chunks.jsonl row i
data/embeddings/embedding_manifest.json   model, dimension, max sequence length, count, config hash
data/embeddings/embedding_preview.json    first three vectors: ID, first eight dimensions, norm, text
data/embeddings/embed_report.md           human-readable report and invariant checklist
```

Model: `sentence-transformers/all-MiniLM-L6-v2`, dimension 384, batch size 32,
max sequence length 256. Vectors are cached by
`sha256(model + content_hash + normalize)`, so duplicate content is encoded once.
Set `EMBEDDING_MODEL_PATH` to a local snapshot directory to load the weights from
disk instead of the Hugging Face cache (the Vercel image uses
`models/all-MiniLM-L6-v2` this way; the default Hub id is unchanged locally).
The pipeline verifies `embedding.max_seq_length >= chunking.max_tokens`, encodes
in chunk order, and fails closed unless the vector count matches the indexed
chunk count, IDs are unique and ordered, and every row is `float32`, finite,
384-d, and within `1e-3` of unit L2 norm. Rerunning produces identical IDs and
near-identical (here bit-identical) vectors.

## Index in ChromaDB (Step 4)

```powershell
python -m scripts.run_index
python -m scripts.run_index --rebuild
pytest tests/unit/test_chroma_store.py tests/integration/test_index_pipeline.py -q
```

Step 4 requires the Step 3 embeddings and manifest. It writes:

```text
data/chroma/                 persistent Chroma collection (cosine)
data/index_manifest.json     collection, count, model, dimension, distance, config hash
data/index_report.md         count, distance, metadata, and filter smoke tests
```

The collection is `mf_faq_v1`, created with `embedding_function=None` because
vectors are pre-computed by Step 3. Records use `chunk_id` as the Chroma ID and
store scalar metadata: `doc_id`, `url`, `title`, `scheme_id`, `page_type`,
`section`, `field`, `chunk_type`, `content_class`, `indexed`, and `fetched_at`.
Lists are joined with `" > "`; `None` becomes `""`.

Upserts run in batches of 100 and are idempotent, so re-running keeps the count
stable. `--rebuild` drops and recreates the collection instead. Before writing,
the pipeline verifies the count, then runs three filter smoke tests (by scheme,
by field, and by `indexed`). The drift guard compares the live collection,
`index_manifest.json`, and `embedding_manifest.json` on model, dimension,
distance, config hash, and count; a mismatch raises `IndexDriftError` and tells
the operator to rebuild. Stale IDs that are not in the current embedding set
also require `--rebuild`, so old and new vectors are never mixed.

## Ask questions (Step 5)

```powershell
python -m scripts.run_answer "What is the expense ratio of HDFC ELSS Tax Saver?"
python -m scripts.run_answer "What about the exit load?" --last-scheme hdfc_elss --turn-count 1
pytest tests/unit/test_pii.py tests/unit/test_intent.py tests/unit/test_resolver.py tests/unit/test_retriever.py tests/unit/test_validators.py tests/unit/test_citation.py tests/unit/test_sentences.py tests/unit/test_client.py -q
pytest tests/security -q
pytest tests/integration/test_answer_service.py -q
```

Step 5 requires the Step 4 index and the active provider key in `.env`. The
decision order is fixed and fail-closed:

```text
length (500 chars) → PII screen → intent screen → resolve scheme + field
→ clarify if ambiguous → embed + retrieve → not_found if no evidence
→ generate → parse + validate (one repair, then extractive fallback)
→ citation resolver → trace + response
```

- **PII** blocks PAN, Aadhaar, account numbers, OTPs, emails, phones, and
  cards before embedding, retrieval, or any LLM call. The security log at
  `data/traces/security/pii_blocks.jsonl` stores only a timestamp, trace and
  session IDs, PII types, and a salted hash.
- **Intent** refuses advice (`refused_advice`, one SEBI/AMFI link) and
  performance/return questions (`refused_performance`, one official HDFC
  factsheet link). Another AMC or an unindexed scheme is `out_of_scope` with no
  link.
- **Resolution** uses exact names/slugs, aliases, fuzzy matching, and the
  session `last_scheme` for up to three follow-up turns. Two named schemes
  clarify with both; a missing scheme clarifies with four options.
- **Retrieval** applies `scheme_id`/`field` filters, retries once unfiltered
  with `filter_fallback: true`, rejects cross-scheme fallback evidence, drops
  low-score/non-factual/off-allowlist records, deduplicates, groups by page,
  applies MMR, and returns at most five chunks from **one** page. Scores are
  `1 - cosine_distance`; the threshold is `0.30`.
- **Generation** calls only the configured provider (Groq, Gemini, OpenAI,
  Anthropic, or Mistral) through `httpx`. Retries cover timeouts, 429, and 5xx;
  no key, header, or raw provider payload is logged.
- **Validation** enforces ≤3 sentences, plain text, no advice/performance
  language, no PII echo, and (when enabled) numbers present in the evidence.
  Invalid JSON gets one repair call; a second failure uses the top chunk’s first
  ≤3 sentences as an `answered` extractive fallback. Model advice or performance
  language forces the matching refusal template.
- **Citations** are always resolved by the service. Model URLs are ignored.
  Every citation is an exact member of `docs/sources.yaml` and, for `answered`,
  comes from the selected evidence page.

Traces are written for every status:

```text
data/traces/retrieval/{trace_id}.json   candidates, scores, filters, fallback, selected, latency
data/traces/answers/{trace_id}.json     status, answer, citation, evidence IDs, guardrails, timings
data/traces/prompts/{trace_id}.txt      system/user prompt (only when the LLM is called)
data/traces/responses/{trace_id}.txt    raw model response and any repair response (sanitized)
data/traces/security/pii_blocks.jsonl   redacted PII block log
```

The raw-response file is written only when the model output is PII-free, so a
blocked value can never reappear in the Pipeline inspector. It is separate from
the structured, redacted answer trace.

PII traces omit the query text and never contain the raw value. Statuses that
short-circuit before retrieval (`clarify`, `blocked_pii`, `out_of_scope`, and
the fixed refusals) write only an answer trace.

## Use the Streamlit UI (Step 6)

```powershell
streamlit run src/mf_faq/ui/app.py
pytest tests/e2e -q
pytest tests/security/test_ui_rendering.py -q
```

The app binds to localhost only (`.streamlit/config.toml` sets
`server.address = "localhost"`), so it is not reachable from the LAN or the
internet. It has four tabs:

- **Ask** — title, persistent “Facts-only. No investment advice.” note, three
  example questions, chat input, and the footer disclaimer. Every response card
  shows a text-plus-colour status, escaped answer text, exactly one validated
  link when the status requires it, the freshness date, collapsed evidence, and
  a short trace hint. Clarify cards show the four eligible schemes and re-submit
  the question with the chosen scheme; PII cards show the fixed privacy copy and
  never echo the raw query.
- **Pipeline** — read-only sub-tabs for Load, Chunk, Embed, Index, Retrieve, and
  Answer. The inspector reads the real artifacts, shows the latest request
  without restarting the app, and renders “artifact not found” plus the expected
  path when a file is missing.
- **Sources** — the five indexed corpus URLs and the approved reference links
  (references are never fetched or indexed).
- **Evaluation** — the recorded ten-question acceptance summary from
  `docs/sample_qa.md`. Only the summary table is rendered; the recorded answers,
  traces, and any sample values stay in the file.

Session state stores `last_scheme` and `turn_count`; scheme memory expires after
three follow-up turns. There is no server-side chat history. UI tests inject a
fake answer service and a fake Chroma store through `st.session_state`, so they
never call a provider, fetch a page, or mutate the live index.

## Deploy on Vercel

Streamlit needs a long-lived WebSocket server, so the app deploys as a
**container image** (`Dockerfile` + `vercel.json` with `"framework": "container"`),
not as a static or standard serverless app. The five pages are ingested, chunked,
embedded, and indexed during the image build, so `data/` is never committed and
no provider key is needed at build time. The MiniLM snapshot is vendored under
`models/all-MiniLM-L6-v2` and loaded through `EMBEDDING_MODEL_PATH`, because
Vercel's build network cannot reach `huggingface.co`. Full steps, every
parameter, limits, and troubleshooting:
[`docs/deploy-vercel.md`](docs/deploy-vercel.md).

Short version:

1. Vercel → **Add New → Project** → import `ad-1375/chatbot1`.
2. Framework Preset **Container**, Root Directory `.`, build settings default.
3. Environment variables (Production): `LLM_PROVIDER=groq`,
   `LLM_MODEL=qwen/qwen3.8-27b`, `GROQ_API_KEY=<key>` (Sensitive), optional
   `DEMO_ADMIN=false`.
4. Deploy, then check `https://<project>.vercel.app/_stcore/health` returns `ok`.

```powershell
# CLI equivalent
vercel link
'groq'             | vercel env add LLM_PROVIDER production
'qwen/qwen3.8-27b' | vercel env add LLM_MODEL production
'<key>'            | vercel env add GROQ_API_KEY production
vercel --prod
```

Because Vercel functions cap request duration (300 s on Hobby), the Streamlit
WebSocket reconnects periodically and in-memory chat state resets; facts are
stateless and still answer correctly.

## Layout

```text
config/config.yaml      typed runtime settings
src/mf_faq/             application package (config, contracts, healthcheck, ingest, chunking, embeddings, index, guardrails, retrieval, generation)
scripts/                pipeline entry points (run_ingest, run_chunk, run_embed, run_index, and run_answer implemented)
models/                 vendored all-MiniLM-L6-v2 snapshot for offline container builds
tests/                  unit, integration, security, and e2e tests
data/                   rebuildable artifacts: raw, processed, chunks, embeddings, chroma, traces
docs/                   source registry, sample Q&A, disclaimer, Vercel deployment guide
```

## Scope limits

- Exactly five indexed URLs. No crawling, sitemap, link following, or sixth page.
- One page per factual answer; citations are resolved from retrieved evidence, never from model output.
- Facts-only answers are capped at three sentences; model output cannot add advice, performance claims, or invented URLs.
- Follow-up scheme memory lasts three turns; clarify, PII, out-of-scope, and refusal statuses never call the LLM.
- Performance content is retained for audit but never indexed.
- Reference links in `docs/sources.yaml` are used only in refusal/education copy and are never indexed.
- Hosted demo runs as a single container image (Vercel) with an ephemeral filesystem; no accounts, server-side chat history, or shared multi-user state. The local run binds to localhost only.

## Data and secrets

- `.env` is gitignored; keys never appear in YAML, logs, prompts, traces, or the UI.
- `data/` is gitignored because it is rebuildable from the five pages.
- PII is blocked before embedding, retrieval, or any LLM call, and is never persisted.
