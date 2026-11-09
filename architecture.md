# Architecture — HDFC Mutual Fund Facts-Only FAQ Assistant

| Field | Value |
|---|---|
| **Document** | Technical Architecture Document |
| **Version** | 1.2 |
| **Date** | 10 Nov 2026 |
| **Status** | Draft for build |
| **Source PRD** | [`PRD.md`](PRD.md) |
| **System** | Small-corpus RAG assistant over five public Groww pages |
| **Stack** | Python 3.12 · Streamlit · sentence-transformers (`all-MiniLM-L6-v2`) · ChromaDB · Groq (`qwen/qwen3.8-27b`) |
| **Deployment** | Local single-user demo; optional Streamlit Community Cloud / Hugging Face Spaces |

---

## 1. Purpose and scope

This document translates [`PRD.md`](PRD.md) into an implementable technical design. It defines:

- The system's runtime and build-time components.
- How data moves from five seed URLs to a citation-backed answer.
- Module boundaries, interfaces, and dependency rules.
- Storage layout, schemas, and artifact lifecycle.
- Guardrail placement, failure behavior, and security boundaries.
- Deployment topology, observability, testing, and key architecture decisions.

**In scope:** one AMC + four HDFC schemes, exactly five indexed pages, facts-only Q&A, one citation per answer, no advice, no performance claims, no PII, and a fully inspectable pipeline.

**Out of scope:** multi-tenant hosting, authentication, live NAV feeds, other AMCs, crawling, PDF parsing, voice, regional languages, and production SLAs. These follow PRD §3.2.

---

## 2. Architectural goals and drivers

| Driver | Architectural response |
|---|---|
| **Small, fixed corpus** | No crawler. A config-driven source registry fetches exactly five URLs; depth is always zero. |
| **Facts over prose** | Structure-aware atomic chunks preserve field labels and values; one chunk can support one fact. |
| **One verifiable citation** | The service resolves the citation server-side from retrieved evidence, never trusts an LLM-generated URL. |
| **No advice / no performance** | Guardrails run outside the LLM, before retrieval or after generation; the model is never the only line of defense. |
| **No PII** | Screening happens before embedding, LLM calls, tracing, or persistence. Raw blocked input is not written to disk. |
| **Inspectability** | Every stage writes immutable artifacts; the UI reads those artifacts instead of reconstructing pipeline state. |
| **Configurability** | Embedding model, vector DB, chunking strategy, retrieval parameters, and LLM provider are configuration, not code. |
| **Reproducibility** | Config hash, input hashes, model revision, and timestamps are recorded in every manifest. |
| **Demo latency** | Local MiniLM + local Chroma; only the final answer generation is a network call. |
| **Local-first** | One Python process for serving, local filesystem for state, no database server, no message broker. |

**Non-goal:** horizontal scalability. The design optimizes for a clear, auditable five-page demo, not for thousands of concurrent users.

---

## 3. System context (C4 level 1)

```
                         ┌──────────────────────────────┐
                         │        Retail user /         │
                         │      support team member     │
                         └──────────────┬───────────────┘
                                        │ HTTPS (local browser)
                                        ▼
┌───────────────────────────────────────────────────────────────────────┐
│                     HDFC MF Facts Assistant                           │
│                                                                       │
│  ┌─────────────────────────┐        ┌──────────────────────────────┐  │
│  │  Ingestion CLI          │        │  Streamlit query app         │  │
│  │  (offline build plane)  │        │  (online serving plane)      │  │
│  └───────────┬─────────────┘        └──────────────┬───────────────┘  │
│              │                                     │                  │
│              ▼                                     ▼                  │
│  ┌─────────────────────────┐        ┌──────────────────────────────┐  │
│  │ Artifacts + ChromaDB    │◀───────│ Retrieval / generation /     │  │
│  │ (local filesystem)      │        │ validation / traces          │  │
│  └───────────┬─────────────┘        └──────────────┬───────────────┘  │
└──────────────┼─────────────────────────────────────┼──────────────────┘
               │ fetch 5 URLs                        │ HTTPS
               ▼                                     ▼
   ┌───────────────────────┐              ┌──────────────────────────┐
   │ groww.in (5 pages)    │              │ LLM provider API         │
   │ public scheme pages   │              │ Groq (default) / Gemini  │
   └───────────────────────┘              │ / OpenAI / Anthropic /   │
                                          │ Mistral                  │
                                          └──────────────────────────┘

   ┌──────────────────────────────────────────────────────────────────┐
   │ Official reference links (AMFI / SEBI / HDFC AMC / Groww)        │
   │ Used only for refusal and education links; never indexed.        │
   └──────────────────────────────────────────────────────────────────┘
```

### 3.1 Actors and external systems

| Actor / system | Interaction | Trust level |
|---|---|---|
| End user | Types a question in the browser | Untrusted input |
| Ingestion CLI | Fetches, parses, chunks, embeds, indexes | Trusted local operator |
| Groww pages | Public HTML source of facts | Untrusted content (prompt-injection surface) |
| LLM provider | Generates the answer from supplied context | Untrusted output; no tools, no browsing |
| Official reference sites | Educational/refusal links only | Trusted destination, not indexed |
| Local filesystem | Artifacts, Chroma, traces | Trusted, but access-controlled by OS |
| Browser | Renders text and links | Untrusted rendering surface (XSS) |

### 3.2 Trust boundaries

1. **Browser → Streamlit:** all text is untrusted; escape on render; cap length.
2. **Groww → ingestion:** scraped text is untrusted data; never treated as instructions.
3. **Application → LLM API:** send only the user question (post-PII screen) and retrieved public text; no secrets, no traces.
4. **LLM output → application:** schema-validate, citation-check, sentence-check, advice/performance-check.
5. **Application → filesystem:** only sanitized, schema-valid data; PII never persisted.
6. **Configuration → runtime:** secrets come from `.env`, never from the browser or scraped content.

---

## 4. Container and component architecture

### 4.1 Runtime containers

| Container | Type | Responsibility | State |
|---|---|---|---|
| **Streamlit app** | Long-running local process | Ask UI, Pipeline Inspector, Sources, Evaluation tabs | Session state only (chat, max 3 turns) |
| **Query service** | In-process Python module | Guardrails, retrieval, prompt assembly, LLM call, validation | Stateless per request |
| **Ingestion CLI** | Short-running local process | Fetch → extract → chunk → embed → index | Writes artifacts; not running during queries |
| **ChromaDB** | Embedded persistent store | Vector search + metadata filters | Derived from chunks/embeddings |
| **Local filesystem** | Storage | Raw HTML, documents, chunks, manifests, traces, eval results | Durable across runs |
| **LLM API** | External service | Answer generation | Stateless |

### 4.2 Component diagram

```
┌────────────────────────────────────────────────────────────────────────────┐
│ PRESENTATION                                                               │
│  ui/app.py                                                                 │
│   ├─ AskView ───────────────┐                                             │
│   ├─ PipelineInspector ─────┼── reads artifacts/manifests directly         │
│   ├─ SourcesView ───────────┤                                             │
│   └─ EvaluationView ────────┘                                             │
└─────────────────────────────┼──────────────────────────────────────────────┘
                              │ AnswerService.answer(QueryRequest)
┌─────────────────────────────▼──────────────────────────────────────────────┐
│ QUERY ORCHESTRATION                                                        │
│  generation/answer.py  (AnswerService)                                     │
│   ├─ guardrails/pii.py          ── block before persistence                │
│   ├─ guardrails/intent.py       ── advice / performance / scope            │
│   ├─ retrieval/resolver.py      ── scheme + field + session follow-up      │
│   ├─ retrieval/retriever.py     ── embed query, Chroma top-k, dedupe       │
│   ├─ generation/prompts.py      ── context + instruction assembly          │
│   ├─ generation/client.py       ── provider factory + adapters             │
│   ├─ guardrails/citation.py     ── evidence-bound citation resolution      │
│   ├─ guardrails/validators.py   ── schema, sentences, safety               │
│   └─ tracing/writer.py          ── retrieval + answer traces               │
└──────────────┬───────────────────────────────┬─────────────────────────────┘
               │                               │
┌──────────────▼─────────────┐   ┌─────────────▼─────────────────────────────┐
│ RETRIEVAL                  │   │ GENERATION                                │
│  embeddings/embedder.py    │   │  generation/client.py                     │
│  index/chroma_store.py     │   │   ├─ GroqAdapter                          │
│  retrieval/retriever.py    │   │   ├─ GeminiAdapter                        │
│                            │   │   ├─ OpenAIAdapter                        │
│                            │   │   ├─ AnthropicAdapter                     │
│                            │   │   └─ MistralAdapter                      │
└──────────────┬─────────────┘   └─────────────┬─────────────────────────────┘
               │                               │
┌──────────────▼───────────────────────────────▼─────────────────────────────┐
│ SHARED                                                                     │
│  config.py · contracts.py · logging.py · errors.py · ids.py               │
└────────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────────┐
│ INGESTION (offline, separate entry point)                                  │
│  ingest/loader.py → ingest/extract.py → chunking/chunker.py                │
│  → embeddings/embedder.py → index/chroma_store.py → reports/manifests      │
└────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Component responsibilities

| Component | Responsibility | Must not |
|---|---|---|
| `config.py` | Load YAML/env, interpolate, validate, compute config hash | Read secrets from YAML or expose them |
| `contracts.py` | Pydantic models for all stage I/O | Contain business logic |
| `ingest/loader.py` | Fetch five seed URLs, snapshot HTML, retry/fallback | Follow links or discover URLs |
| `ingest/extract.py` | Clean HTML into a block tree/document | Rewrite factual values |
| `chunking/chunker.py` | Structure-aware chunks, field labels, exclusions | Merge across schemes or split KV pairs |
| `embeddings/embedder.py` | Encode chunks/queries with the configured model | Silently change model or dimension |
| `index/chroma_store.py` | Upsert/query Chroma, metadata filters, drift guard | Be the source of truth for text |
| `retrieval/resolver.py` | Scheme/field aliases, session follow-up, ambiguity | Guess a scheme when the query is ambiguous |
| `retrieval/retriever.py` | Top-k, threshold, dedupe, MMR, one-page selection | Return `indexed: false` content |
| `generation/prompts.py` | Build system/user/repair prompts | Include secrets or unconverted PII |
| `generation/client.py` | Provider adapters, retries, normalized errors | Log raw API keys or full provider payloads |
| `generation/answer.py` | Orchestrate the request state machine | Let unvalidated LLM text reach the UI |
| `guardrails/*` | PII, intent, sentence, advice/performance checks | Depend on the LLM to enforce policy |
| `guardrails/citation.py` | Validate/repair the final citation from evidence; enforce allowlist | Trust the model URL or fabricate section anchors |
| `tracing/writer.py` | Write retrieval/answer traces safely | Persist blocked PII or secrets |
| `ui/app.py` | Render answers and artifacts | Recompute pipeline logic or render raw HTML |

### 4.4 Dependency rules

```
ui        → generation.answer, tracing.reader
answer    → guardrails, retrieval, generation.prompts, generation.client, tracing.writer
retrieval → embeddings, index, contracts
index     → contracts, config
embeddings→ contracts, config
chunking  → contracts, config
ingest    → contracts, config
```

Rules:

- Ingestion code never imports query or UI code.
- Retrieval never writes documents or chunks.
- Guardrails are independent of any provider SDK.
- Only `generation/client.py` imports provider SDKs.
- Only `config.py` reads environment variables.
- Contracts are dependency-free and shared by both planes.

---

## 5. Data architecture

### 5.1 Two-plane model

| Plane | Trigger | Output | Mutability |
|---|---|---|---|
| **Build plane** | `python -m scripts.run_pipeline` (or stage scripts) | Raw HTML, documents, chunks, embeddings, Chroma, manifests | Append-only snapshots; index rebuildable |
| **Serve plane** | User query in Streamlit | Answer JSON, retrieval trace, answer trace | Per-request, append-only |
| **Evaluation plane** | `python -m eval.run` | Scorecard JSON/Markdown | Per-run, append-only |

The **JSONL artifacts are the audit source of truth**. Chroma is a **derived serving index** that can be rebuilt from chunks + embeddings at any time. This separation prevents a corrupted or changed vector index from silently altering the evidence record.

### 5.2 Artifact flow

```
Five seed URLs
   │
   ▼
raw/{page_id}/{timestamp}.html ──────────────┐
   │                                          │ audit snapshot
   ▼                                          │
documents.jsonl + text/{page_id}.txt          │
   │                                          │
   ▼                                          │
chunks.jsonl + excluded.jsonl                 │
   │                                          │
   ▼                                          │
embeddings.npy + embedding_ids.json           │
   │                                          │
   ▼                                          │
Chroma collection  ◀── derived, rebuildable ──┘
   │
   ▼
retrieval trace ──▶ prompt ──▶ LLM ──▶ answer trace ──▶ UI
```

### 5.3 Storage layout

```
data/
├─ raw/{page_id}/{timestamp}.html        # immutable HTML snapshots
├─ processed/
│  ├─ documents.jsonl                    # one line per page
│  ├─ text/{page_id}.txt                 # cleaned, human-readable text
│  └─ load_report.md
├─ chunks/
│  ├─ chunks.jsonl                       # all chunks, including excluded
│  ├─ excluded.jsonl                     # non-indexed content + reasons
│  ├─ chunk_stats.json
│  └─ chunk_report.md
├─ embeddings/
│  ├─ embeddings.npy                     # float32 N × 384
│  ├─ embedding_ids.json
│  ├─ embedding_manifest.json
│  ├─ embedding_preview.json
│  ├─ embed_report.md
│  └─ cache/                             # content-hash → vector cache
├─ chroma/                               # Chroma persistent directory
├─ manifests/
│  ├─ pipeline_latest.json               # stage statuses + hashes
│  └─ pipeline_{run_id}.json
├─ index_manifest.json
├─ index_report.md
├─ retrieval/calibration_report.md
└─ traces/
   ├─ retrieval/{trace_id}.json
   ├─ answers/{trace_id}.json
   └─ prompts/{trace_id}.txt
```

### 5.4 Source of truth and lifecycle

| Data | Source of truth | Derived from | Retention |
|---|---|---|---|
| Page facts | Raw HTML snapshot + `documents.jsonl` | — | Indefinite (demo) |
| Chunks | `chunks.jsonl` | Documents | Indefinite |
| Vectors | `embeddings.npy` | Chunks | Indefinite |
| Search index | Chroma | Chunks + vectors | Rebuildable |
| Answer evidence | Retrieval/answer traces | Query + index | 7 days |
| Evaluation | `eval/results_*.json` | Golden set + live system | Indefinite |
| Config | `config.yaml` + `.env` | — | Versioned; secrets never stored |

**Lifecycle rules**

- A re-fetch creates a **new timestamped snapshot**; it never overwrites the prior raw HTML.
- `documents.jsonl` is rebuilt atomically per successful ingestion run (write temp + rename).
- `chunks.jsonl` and `excluded.jsonl` are rebuilt atomically from the current documents.
- Embeddings and Chroma are rebuilt when chunk IDs or embedding config change.
- `pipeline_latest.json` records which artifact versions belong together.

### 5.5 Data classification

| Class | Examples | Handling |
|---|---|---|
| **Public** | Groww page text, scheme facts, reference URLs | Groww text indexed and cited; reference URLs link-only, never indexed |
| **Internal** | Chunks, embeddings, traces, manifests, config (non-secret) | Local only; visible in inspector |
| **Sensitive** | API keys, `.env` | Server-side only; never logged/traced/rendered |
| **Prohibited** | PAN, Aadhaar, account, OTP, email, phone | Blocked before persistence; never sent to LLM |

### 5.6 Contracts

All schemas live in `src/mf_faq/contracts.py` and mirror PRD §12:

- `SourcePage`
- `Block` / `SectionTree` (internal extraction model)
- `Chunk`
- `EmbeddingManifest`
- `IndexManifest`
- `QueryRequest`
- `GuardResult`
- `ResolvedQuery`
- `RetrievedEvidence`
- `PromptContext`
- `LLMResult`
- `AnswerResponse`
- `RetrievalTrace`
- `AnswerTrace`
- `GoldenItem`
- `EvalResult`

Every persisted artifact includes `schema_version`, `created_at`, and `config_hash` where applicable.

---

## 6. Ingestion architecture (build plane)

### 6.1 Orchestration model

Ingestion is a **deterministic, resumable DAG** of five stages. Each stage can run standalone (useful for demos) and as part of the full pipeline.

```
fetch → extract → chunk → embed → index
```

`scripts/run_pipeline.py` orchestrates the stages and writes a `PipelineManifest`:

```json
{
  "run_id": "ing_20261108T073000Z",
  "config_hash": "sha256:…",
  "started_at": "2026-11-08T07:30:00Z",
  "finished_at": "2026-11-08T07:31:12Z",
  "stages": {
    "fetch":   {"status": "success", "outputs": ["data/raw/…"], "input_hash": "…", "output_hash": "…"},
    "extract": {"status": "success", "outputs": ["data/processed/documents.jsonl"], "input_hash": "…", "output_hash": "…"},
    "chunk":   {"status": "success", "outputs": ["data/chunks/chunks.jsonl"], "input_hash": "…", "output_hash": "…"},
    "embed":   {"status": "success", "outputs": ["data/embeddings/embeddings.npy"], "input_hash": "…", "output_hash": "…"},
    "index":   {"status": "success", "outputs": ["data/chroma/"], "input_hash": "…", "output_hash": "…"}
  },
  "counts": {"pages": 5, "chunks": 0, "indexed": 0, "excluded": 0},
  "warnings": []
}
```

**Stage behavior**

| Stage | Input | Output | Skip rule | Force flag |
|---|---|---|---|---|
| `fetch` | Config seed URLs | Raw HTML + fetch metadata | Same URL + same day + `--reuse` | `--refetch` |
| `extract` | Raw HTML | `documents.jsonl`, text | Raw hash unchanged | `--reextract` |
| `chunk` | Documents | `chunks.jsonl`, excluded | Documents hash unchanged + chunk config unchanged | `--rechunk` |
| `embed` | Chunks | `embeddings.npy`, IDs | Chunk hash + embedding config unchanged | `--reembed` |
| `index` | Chunks + vectors | Chroma collection | Manifest matches active config | `--rebuild` |

**Atomic writes:** each stage writes to a temporary path and renames on success; a failed stage never leaves a half-written artifact that looks valid.

### 6.2 Source registry

The source registry is **configuration, not discovery**. `config.yaml` lists exactly five records:

```yaml
corpus:
  mode: fixed_seed_only
  follow_links: false
  max_depth: 0
  allowlist_domains: ["groww.in"]
  seed_pages:
    - {id: c1_amc_hdfc, url: "...", page_type: amc, scheme_id: amc_hdfc}
    - {id: c2_hdfc_large_cap, url: "...", page_type: scheme, scheme_id: hdfc_large_cap}
    - {id: c3_hdfc_flexi_cap, url: "...", page_type: scheme, scheme_id: hdfc_flexi_cap}
    - {id: c4_hdfc_elss, url: "...", page_type: scheme, scheme_id: hdfc_elss}
    - {id: c5_hdfc_mid_cap, url: "...", page_type: scheme, scheme_id: hdfc_mid_cap}
```

**Enforcement**

- Loader accepts a `list[SeedPage]`, not a URL string from the user.
- There is no recursive fetch function in the codebase.
- A unit test asserts that the loader calls `fetch` exactly five times.
- A validator rejects any `url` whose registrable domain is not in `allowlist_domains`.
- The pipeline manifest records the seed list hash, so a changed source set is visible.

### 6.3 Loader

**Interface**

```python
class PageLoader(Protocol):
    def load(self, seed: SeedPage) -> LoadedPage: ...

@dataclass
class LoadedPage:
    seed: SeedPage
    html: str
    status: int
    final_url: str
    headers: dict[str, str]
    fetched_at: datetime
    renderer: Literal["httpx", "playwright"]
    content_hash: str
```

**Algorithm**

1. Read the seed from config; validate domain and scheme.
2. Check `robots.txt` for the target path (cache result for the run).
3. `httpx.get` with timeout, retry/backoff, identifying User-Agent, 2-second delay.
4. Save the response even on non-200 status for audit.
5. Probe visible text length with a lightweight parser (scripts/styles/nav removed). If text < `min_text_chars_for_fallback` (800), re-fetch with Playwright and keep the richer renderer. Full extraction runs in the next stage; missing fact labels are reported there and are never "fixed" by fetching another page.
6. Save raw HTML under `data/raw/{page_id}/{timestamp}.html`.
7. Return a `LoadedPage`; the extractor runs separately and writes `documents.jsonl`.
8. On total failure, mark the page `failed` and continue; the pipeline exits non-zero only if fewer than five pages load.

**Guardrails**

- No redirects off `groww.in`; if final URL is off-domain, reject the page and record the reason.
- Max response size (e.g., 5 MB) to prevent accidental huge downloads.
- No cookies, no login, no JavaScript execution unless fallback is required.
- Playwright runs headless with a fresh context and no persistent profile.

### 6.4 Extractor

The extractor converts HTML into a **block tree** that preserves semantic relationships.

```python
@dataclass
class Block:
    kind: Literal["heading", "paragraph", "kv_pair", "table_row", "list_item"]
    text: str
    section_path: list[str]
    label: str | None = None
    value: str | None = None
    table_headers: list[str] | None = None
    ordinal: int = 0
```

**Steps**

1. Parse with `selectolax` (fast) or `lxml`; remove `script`, `style`, `noscript`, `svg`, `iframe`, nav, footer, cookie banners, ads, app-download prompts, "similar funds", and news modules.
2. Identify the main content container; fall back to `trafilatura` if selectors fail.
3. Walk headings `h1–h4` to build `section_path`.
4. Emit definition-list pairs as `kv_pair`; tables as header + row blocks; lists as `list_item`; paragraphs as `paragraph`.
5. Normalize whitespace/Unicode; keep numeric values, units, and punctuation exactly.
6. Scrub corpus emails and phone numbers from text.
7. Attach document metadata and write:
   - `documents.jsonl` (one JSONL record per page)
   - `text/{page_id}.txt` (human-readable)
   - `load_report.md` (status, word count, fact coverage, fallback renderer)

**Fact coverage check:** the extractor looks for label variants for the seven target facts and records `present/missing/uncertain` per page. Missing facts are reported, not invented.

### 6.5 Chunker

**Strategy:** structure-aware atomic fact chunks. `ChunkingStrategy` is a protocol with three implementations:

```python
class ChunkingStrategy(Protocol):
    def chunk(self, doc: SourcePage, blocks: list[Block], cfg: ChunkConfig) -> list[Chunk]: ...

class StructureAwareChunker: ...   # default
class RecursiveChunker: ...        # fallback / comparison
class FixedWindowChunker: ...      # fallback / comparison
```

**Pipeline**

1. Walk blocks with `section_path`.
2. Emit atomic candidates:
   - `kv_pair` → one chunk;
   - `table_row` → one chunk with header names in text;
   - `list_item` → one chunk if self-contained;
   - `paragraph` → one chunk, or split at sentence boundaries if oversized.
3. Prefix text with `{page/scheme name} > {section_path} > {field}`.
4. Merge adjacent small blocks only within the same section and field.
5. Never split a key-value pair or a table row.
6. Assign deterministic `chunk_id = sha1(doc_id|section_path|field|ordinal)[:16]`.
7. Classify `content_class`:
   - `factual` → indexed;
   - `performance` → stored, `indexed: false`;
   - `boilerplate` / `navigation` → stored, `indexed: false`.
8. Deduplicate by normalized-text SHA-256 within a page.
9. Enforce token limits with the **embedding model's tokenizer**.

**Why the tokenizer is shared:** if the embedder changes to a model with a different tokenizer/limit, chunk sizes must be recalculated. The chunker reads `embedding.model` and `embedding.max_seq_length` from config rather than hardcoding.

**Exclusion rules**

| Content | Classification | Reason |
|---|---|---|
| Returns, CAGR, NAV history, alpha/beta/Sharpe | `performance` | PRD no-performance rule |
| Ratings, reviews, "similar funds", news | `boilerplate`/`navigation` | Not a scheme fact |
| Nav/footer/legal boilerplate | `boilerplate` | Noise |
| Scheme facts, AMC facts, statement steps | `factual` | Indexed |

### 6.6 Embedder

**Interface**

```python
class Embedder:
    def encode_documents(self, texts: list[str]) -> np.ndarray: ...
    def encode_query(self, text: str) -> np.ndarray: ...
    @property
    def dimension(self) -> int: ...
    @property
    def manifest(self) -> EmbeddingManifest: ...
```

**Algorithm**

1. Validate config: model name, device, batch size, normalization, max sequence length.
2. Load the model once; record name, revision/commit, dimension, and max length.
3. Verify `max_seq_length >= chunking.max_tokens`; fail if not.
4. Encode in batches, `normalize_embeddings=True`, `float32`.
5. Look up a content-hash cache first; embed only misses.
6. Write `embeddings.npy` in chunk order; write `embedding_ids.json`.
7. Validate shape, dtype, NaN/Inf, and L2 norms.
8. Write `embedding_manifest.json` with config hash and model revision.

**Performance:** MiniLM on CPU encodes this corpus in seconds; the model is loaded once per process and reused by query serving.

### 6.7 Indexer

**Interface**

```python
class VectorStore(Protocol):
    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None: ...
    def query(self, vector: np.ndarray, k: int, where: dict | None) -> list[Hit]: ...
    def count(self) -> int: ...
    def health(self) -> StoreHealth: ...
```

**Algorithm**

1. Initialize `chromadb.PersistentClient(path=config.vector_db.persist_dir)`.
2. Get or create the collection with cosine distance.
3. Validate the collection manifest against the active embedding manifest:
   - same model, dimension, and config hash → proceed;
   - mismatch → block and require `--rebuild`.
4. Batch upsert 100 records at a time with IDs, documents, embeddings, and flattened metadata.
5. Flatten `section_path` to a string; convert `None` to `""`; keep booleans as booleans.
6. Verify count equals indexed chunk count.
7. Run smoke queries with scheme/field filters.
8. Write `index_manifest.json` and `index_report.md`.

**Idempotency:** upsert uses deterministic `chunk_id`; running twice must not increase the count.

### 6.8 Ingestion failure matrix

| Failure | Detection | Behavior | Artifact |
|---|---|---|---|
| HTTP 403/404 | Status code | Record failed page; continue; pipeline fails if <5 pages | `load_report.md` |
| Timeout after retries | Exception | Same as above | `load_report.md` |
| Off-domain redirect | Final URL check | Reject page; security log | `load_report.md` |
| Thin/JS-only HTML | Text < 800 chars or missing facts | Playwright retry; then mark missing | `load_report.md` |
| No main content | Extractor fallback fails | Page marked failed; no chunks | `load_report.md` |
| Tokenizer/model missing | Load exception | Abort stage with actionable message | `embed_report.md` |
| Embedding NaN/norm failure | Validation | Abort stage; never index | `embed_report.md` |
| Chroma config mismatch | Manifest comparison | Block indexing/querying; require rebuild | `index_report.md` |
| Partial write | Temp-file strategy | Discard temp; previous artifacts remain valid | manifest status |

**Fail-closed rule:** a stage that cannot validate its output does not advance the pipeline. Stale artifacts are better than silently corrupted ones, provided the manifest identifies the last valid run.

### 6.9 Ingestion sequence

```
Operator          Pipeline           Loader          Extractor        Chunker         Embedder        Chroma
   │                 │                 │                │                │                │              │
   ├─ run pipeline ─▶│                 │                │                │                │              │
   │                 ├─ load seeds ───▶│                │                │                │              │
   │                 │                 ├─ GET 5 URLs ──▶│                │                │              │
   │                 │                 │◀─ HTML ────────┤                │                │              │
   │                 │◀─ LoadedPage ───┤                │                │                │              │
   │                 ├─ extract ──────────────────────▶│                │                │              │
   │                 │◀─ documents.jsonl ───────────────┤                │                │              │
   │                 ├─ chunk ──────────────────────────────────────────▶│                │              │
   │                 │◀─ chunks.jsonl ────────────────────────────────────┤                │              │
   │                 ├─ embed ───────────────────────────────────────────────────────────▶│              │
   │                 │◀─ embeddings.npy + manifest ───────────────────────────────────────┤              │
   │                 ├─ index ───────────────────────────────────────────────────────────────────────────▶│
   │                 │◀─ count + manifest ────────────────────────────────────────────────────────────────┤
   │◀─ manifest ─────┤                 │                │                │                │              │
```

---

## 7. Query architecture (serve plane)

### 7.1 Request state machine

```
RECEIVED
   │
   ├─ length > 500 ─────────────────────────────▶ error (ask to shorten)
   │
   ├─ PII detected ──────────────────────────────▶ BLOCKED_PII  (no trace of raw query)
   │
   ├─ intent = advice ───────────────────────────▶ REFUSED_ADVICE
   ├─ intent = performance ──────────────────────▶ REFUSED_PERFORMANCE
   ├─ intent = out_of_scope ─────────────────────▶ OUT_OF_SCOPE
   │
   ▼
RESOLVE (scheme, field, session context)
   │
   ├─ scheme missing/ambiguous ──────────────────▶ CLARIFY
   │
   ▼
RETRIEVE (embed query → Chroma → filter/score/dedupe/MMR)
   │
   ├─ no evidence above threshold ───────────────▶ NOT_FOUND
   │
   ▼
BUILD PROMPT → GENERATE (LLM) → PARSE JSON
   │
   ├─ schema invalid ──▶ repair once ──▶ still invalid ──▶ EXTRACTIVE_FALLBACK
   ├─ citation invalid ─▶ repair once ─▶ still invalid ──▶ EXTRACTIVE_FALLBACK
   ├─ sentence limit violated ─▶ shorten once ─▶ still ─▶ EXTRACTIVE_FALLBACK
   ├─ advice/performance language ─▶ force safe refusal
   ├─ PII echo ─▶ redact + safe response
   │
   ▼
VALIDATED → ANSWERED / NOT_FOUND / REFUSED_* → trace → UI
```

**Key architectural choice:** guardrails short-circuit **before** retrieval where possible. This avoids spending an LLM call on a request that must be refused and ensures a refusal cannot be influenced by retrieved text.

### 7.2 Request lifecycle

`AnswerService.answer(request: QueryRequest) -> AnswerResponse` executes:

1. **Create trace ID early.** It is used for logs and artifacts, but no raw query is persisted until the PII screen passes.
2. **Validate input length.** Reject >500 characters with a fixed message.
3. **PII screen.** Runs regex + context checks. On hit: emit `blocked_pii`, write only a security record with `pii_type` and salted hash, return.
4. **Intent screen.** Rule patterns first; optional lightweight LLM classifier only when rules are inconclusive. Refusals use fixed templates + one approved link.
5. **Session context.** If the query is a follow-up (e.g., "and its exit load?"), carry forward the last resolved scheme for up to 3 turns. Session state is memory-only.
6. **Resolve scheme and field.** Exact/alias/fuzzy matching; record confidence and matched alias.
7. **Clarify if needed.** If the fact is scheme-specific and no scheme is resolved, return `clarify` with the four scheme chips. No retrieval occurs.
8. **Embed query.** Use the same `Embedder` instance/config as ingestion; verify manifest compatibility.
9. **Retrieve.** Top-12 with metadata filters, fallback without filters if zero results, threshold, dedupe, MMR, final 5, 1,200-token cap, one-page selection.
10. **Not-found short-circuit.** If no candidate passes threshold, return `not_found` with the closest scheme page.
11. **Build prompt.** System rules + resolved scheme/field + delimited context blocks with `url`, `title`, `section`, `fetched_at`.
12. **Generate.** Provider adapter call with timeout/retries; capture raw text, usage, latency.
13. **Parse and validate.** Pydantic schema; citation against evidence; sentence count; advice/performance/PII checks.
14. **Repair once.** Shorten/fix JSON with a constrained repair prompt if validation fails.
15. **Extractive fallback.** If repair fails, take the top chunk's first ≤3 sentences, attach its URL, mark `fallback: true`.
16. **Persist traces.** Retrieval trace + answer trace + prompt file, with PII-safe fields.
17. **Return** the `AnswerResponse` to the UI.

### 7.3 Sequence diagram

```
User      Streamlit       AnswerService      Guardrails       Retriever        Chroma         LLM API
 │            │                 │                 │               │               │              │
 ├─ ask ─────▶│                 │                 │               │               │              │
 │            ├─ answer(req) ──▶│                 │               │               │              │
 │            │                 ├─ PII screen ───▶│               │               │              │
 │            │                 │◀─ pass ─────────┤               │               │              │
 │            │                 ├─ intent ───────▶│               │               │              │
 │            │                 │◀─ factual ──────┤               │               │              │
 │            │                 ├─ resolve scheme/field ──────────▶│               │              │
 │            │                 │◀─ hdfc_elss / lock_in ───────────┤               │              │
 │            │                 ├─ embed query ───────────────────▶│               │              │
 │            │                 ├─ search(top_k=12, filters) ──────────────────────▶│              │
 │            │                 │◀─ hits + scores ──────────────────────────────────┤              │
 │            │                 ├─ select context (MMR, thresholds) ┤               │              │
 │            │                 ├─ build prompt ────────────────────┤               │              │
 │            │                 ├─ generate(prompt) ────────────────────────────────────────────────▶│
 │            │                 │◀─ JSON answer ──────────────────────────────────────────────────────┤
 │            │                 ├─ validate + repair/fallback ─────▶│               │              │
 │            │                 ├─ write traces ────────────────────┤               │              │
 │            │◀─ response ─────┤                 │               │               │              │
 │◀─ render ──┤                 │                 │               │               │              │
```

### 7.4 Resolver design

**Scheme resolution order**

1. Exact full name/slug match.
2. Alias match (table in PRD §10.2).
3. Token overlap + fuzzy score above threshold.
4. Session last scheme for follow-ups.
5. If still none and the query is scheme-specific → `clarify`.
6. If the query is AMC-wide (statement/AMC basics) → `amc_hdfc`.

**Field resolution**

- Dictionary of canonical fields + synonyms.
- Deterministic match first; embedding retrieval fills the gap when no field is resolved.
- If two fields are requested ("exit load and minimum SIP"), resolve the first as primary and retrieve evidence for both. The answer may cover both only within the 3-sentence limit; otherwise it answers the primary and asks the user to query the second separately. The response keeps one citation and logs `multi_field: true`.

**Conflict rules**

| Situation | Action |
|---|---|
| Two schemes named | `clarify` with both options |
| Scheme + comparison language | `refused_advice` |
| Scheme + return language | `refused_performance` |
| No scheme + AMC-wide fact | Resolve to AMC page |
| No scheme + scheme-specific fact | `clarify` |
| Unknown field | Vector-only retrieval |
| Follow-up with no noun | Use last scheme, max 3 turns |

### 7.5 Retrieval and context assembly

**Retrieval parameters** (config-driven):

| Parameter | Default | Purpose |
|---|---|---|
| `top_k` | 12 | Candidate pool |
| `final_k` | 5 | Context chunks |
| `score_threshold` | 0.30 | Evidence cutoff (calibrated) |
| `mmr_lambda` | 0.7 | Diversity vs. relevance |
| `max_context_tokens` | 1200 | Prompt budget |
| `use_metadata_prefilter` | true | Scheme/field narrowing |
| `one_page_citation` | true | Single-source answers |

**Context assembly algorithm**

1. Filter out `indexed: false`, non-factual, and disallowed URLs.
2. Convert Chroma distance to `score = 1 - distance`.
3. Drop below threshold.
4. Deduplicate by `(doc_id, field, normalized_text)`.
5. Group by `doc_id`; keep the best-supported page.
6. Apply MMR within that page; select up to `final_k`.
7. Trim to `max_context_tokens` without splitting a chunk.
8. Order by score descending, then assign prompt labels `[S1]…[S5]`.

**Why one page first:** the product promises exactly one citation. Selecting evidence from one page avoids the temptation to merge facts across pages and makes the citation unambiguous.

### 7.6 Citation resolution

The LLM is asked for a `citation_url`, but the **service owns the final citation**:

1. Compare the returned URL to the evidence set (exact string match after normalization).
2. If it matches, keep the answer text and attach the **evidence record's** title/section/URL.
3. If it does not match, do not display the model URL. Use the top evidence record's URL and section; mark `citation_repaired: true`.
4. If no evidence exists, the request should already have taken the not-found path; if not, return `error`/`not_found`.
5. Validate the final URL domain against the allowlist.
6. Render the URL as text with `rel="noopener noreferrer"`.

Refusals do not use corpus evidence; their single link comes from the verified reference registry (§9.6) and is validated the same way. If no verified link exists, the response is text-only and the configuration warning is logged.

This "server-side citation" design is stronger than prompt-only compliance and is a key ADR (see §14).

### 7.7 Session state

- Stored in Streamlit `session_state`, memory only.
- Fields: `messages` (rendered cards), `last_scheme`, `turn_count`, `last_trace_id`.
- Reset on "Clear conversation" or browser session end.
- No cookies, no server-side user store, no analytics.
- Follow-ups only inherit the scheme, not prior answers; each answer is regenerated from evidence.

### 7.8 Error and degradation behavior

| Scenario | User sees | Internal action |
|---|---|---|
| LLM timeout/rate limit | Safe `error`/not-found message with scheme link | Retry twice with backoff; log provider error code only |
| Invalid JSON | Extractive fallback | One repair attempt first |
| Citation invalid | Corrected citation or fallback | Log `citation_repaired` |
| No evidence | `not_found` | Include closest scheme page |
| Chroma missing | Setup message | Startup health check fails fast |
| Config drift | Rebuild instruction | Block queries; do not mix indexes |
| Model missing | Setup error | Do not start serving |
| PII detected | Fixed privacy message | No retrieval, no LLM, no raw query trace |

### 7.9 Answer trace

Every non-blocked request writes one JSON trace (PRD §10.3) plus the exact prompt. Required fields:

- `trace_id`, `retrieval_trace_id`, `created_at`
- `status`, `intent`, `resolved_scheme`, `resolved_field`
- `filters`, `filter_fallback`
- `evidence` (chunk IDs, scores, sections)
- `prompt_hash`, `raw_response`, `parsed`
- `guardrails` (pii/advice/performance/citation/sentences/fallback)
- `usage`, `latency_ms`, `provider`, `model`, `config_hash`

**PII rule:** blocked requests get a separate minimal security record, never a full answer trace. `raw_response` is PII-echo screened and redacted before it is written; if redaction would destroy the answer, the extractive fallback is used instead.

### 7.10 Response and UI rendering

The UI is a thin renderer over `AnswerResponse`; it never composes facts or citations itself. Each response type has one fixed card layout.

| Response type | Card contents | Evidence panel |
|---|---|---|
| `answered` | Answer text (≤3 sentences) · one source title + URL · `Last updated from sources: {cited.fetched_at}` · disclaimer | Yes: chunk IDs, scores, section paths |
| `not_found` | Not-found copy · closest scheme page link · "try rephrasing" hint · disclaimer | Optional: top candidates below threshold |
| `clarify` | Clarifying question · four scheme chips · disclaimer | No |
| `refused_advice` / `refused_performance` | Fixed refusal copy · one verified reference link · disclaimer | No |
| `blocked_pii` | Fixed privacy copy stating nothing was stored · disclaimer | No |
| `error` | Safe error copy · retry hint · disclaimer | Optional: trace ID only |

**Rules**

- `Last updated from sources` is computed server-side: cited evidence `fetched_at` for factual answers; corpus refresh date for refusals/education; never the current wall-clock time.
- Exactly one URL is rendered per card. Sources are shown as text plus a `rel="noopener noreferrer"` link; no markdown link syntax is trusted from the model.
- The persistent disclaimer ("Facts-only. No investment advice.") is visible above the input and in the footer.
- The welcome panel shows three example questions as chips; clicking one submits it as a normal query.
- The Ask tab also shows the `trace_id` and an expandable evidence panel with scores.
- All model/page text is escaped; `unsafe_allow_html` stays off.
- "Clear conversation" resets session state; no chat survives the browser session.

Visual system, tokens, and screen inventory: §7.11.

### 7.11 UI design system and screen inventory (Stitch)

**Provenance.** The visual layer was designed in Google Stitch from the written
brief in [`docs/ui-design-prompt.md`](docs/ui-design-prompt.md) and exported as
`stitch_hdfc_mf_facts_assistant.zip`. The extracted artifacts live under
[`design/stitch/`](design/stitch/) and are design references only: Stitch emits
static HTML/Tailwind, while the shipped application stays Streamlit (§4.1,
ADR-010). No Stitch markup is served to users.

| Artifact | Path | Content |
|---|---|---|
| Design system | `design/stitch/DESIGN.md` | Tokens, type scale, component rules |
| Main screen | `design/stitch/main/code.html` | All four tabs in one 1120 px desktop frame |
| Empty state | `design/stitch/empty_state/` | First run: centred composer, example chips, no answers |
| Refusal state | `design/stitch/refusal_state/` | Advice refusal card emphasis, no citation |
| Mobile | `design/stitch/mobile_390px/` | 390 px single column, scrollable tab row |

#### Tokens

The palette is closed. No hue outside this table is permitted. Stitch also emits
`tertiary` and `error` roles in the Tailwind config of every screen, but they are
never applied to markup (`text-tertiary`, `bg-error`, and friends: zero
occurrences) and must stay that way.

| Token | Hex | Application |
|---|---|---|
| Primary green | `#00D09C` | Primary button fill, active tab underline, timeline steps, focus border |
| Deep green | `#00B386` | Button hover, citation links, small AA-compliant green text |
| Green tint | `#E8FBF6` | `Answered` chip fill, selected states, citation pills |
| Ink | `#44475B` | Headings, body copy, tabular values |
| Muted | `#7C7E8C` | Metadata, timestamps, placeholders, helper copy |
| Surface | `#FFFFFF` | Cards, header bar, table rows |
| Subtle surface | `#F7F8FA` | App canvas, table headers, neutral chips, row hover |
| Border | `#E6E9EB` | Every 1 px hairline, divider, and card perimeter |

**Typography.** Manrope, Inter fallback: `headline-xl` 32/40 (700), `headline-lg`
24/32 (700), `headline-md` 20/28 (600), `headline-sm` 16/24 (600), `body-lg`
16/24, `body-md` 15/22.5, `body-sm` 13/19.5, `label-lg` 14/20 (600), `label-md`
12/16 (600, 0.02 em), `label-sm` 11/14 (600, 0.04 em). Numeric cells use
`font-variant-numeric: tabular-nums`.

**Geometry.** 8 px rhythm (`space-xs` 4, `space-sm` 8, `space-md` 16, `space-lg`
24, `space-xl` 32, `space-2xl` 48). Content frame 1120 px centred, 24 px desktop
gutters, 16 px mobile. Radius: 12 px cards, 8 px buttons/inputs/chips, 4 px
citation pills, 999 px only for step dots and counters. Elevation is planar: 1 px
borders instead of shadows, with a single `0 1px 3px rgba(0,0,0,0.03)` allowed on
the composer.

#### Screen anatomy

| Tab | Composition |
|---|---|
| Ask | Sub-header (shield badge, title, version chip, corpus/ingestion/status meta) → tab row → composer card (search glyph, input, solid green `Ask`) → disclaimer line → `Quick Queries` chip row → stacked answer cards |
| Answer card | Status chip + trace ID (left), mono meta (right) → muted `Query:` line → 16 px answer (≤3 sentences) → hairline → one Groww link with `Last updated from sources: {date}` |
| Pipeline | Trace header with query ID → segmented `Retrieve` / `Answer` control → vertical rail with green step dots and five cards (validation/PII, embed+retrieve, rerank, grounding, synthesis), each carrying latency and a count |
| Sources | Intro line plus freshness note → table of scheme name, Groww URL, indexed chunks, last fetched; hover row `#F7F8FA` |
| Evaluation | Summary bar with status counts and suite timestamp → 10-row table (#, question, status, citation, last verified) with tinted alt rows |

**Status chip grammar.** `answered` is the only tinted chip (`#E8FBF6` fill,
`#00B386` text, check icon). `not_found`, `refused_*`, and `blocked_pii` all use
the neutral chip (`#F7F8FA` fill, `#44475B` text, `#E6E9EB` border) and differ by
label and icon. State is never signalled by colour alone, which also keeps the
four states distinguishable to colour-blind users and consistent with the closed
palette above.

#### Streamlit mapping

The structural tokens are applied in `.streamlit/config.toml` under `[theme]`:
`base = "light"`, `primaryColor` `#00D09C`, `backgroundColor` `#FFFFFF`,
`secondaryBackgroundColor` `#F7F8FA`, `textColor` `#44475B`, `borderColor`
`#E6E9EB`, `linkColor` `#00B386` with `linkUnderline`, dataframe header on
`#F7F8FA`/`#7C7E8C`, `baseRadius` 0.75 rem, `buttonRadius` 0.5 rem,
`baseFontSize` 15, `showWidgetBorder`, and the green role re-pointed to the
brand tint. `tests/unit/test_theme_config.py` fails if any hex in `[theme]`
leaves the closed palette, and the localhost binding test still reads the same
file.

What the theme cannot deliver:

- **Manrope.** `theme.font` only accepts generic families, so the app keeps
  Streamlit's sans stack. Loading Manrope needs a stylesheet injection, which the
  `unsafe_allow_html=False` rule (and
  `tests/security/test_ui_rendering.py`) forbids.
- **Four semantic alert colours.** `cards.py` maps `answered` to
  `st.success`, refusals and clarify to `st.warning`, not-found and out-of-scope
  to `st.info`, and PII/errors to `st.error`. Those roles keep their default
  orange/blue/red so real failures stay distinguishable; only the green role is
  rebranded. Moving to the design's single neutral chip is a `cards.py` change,
  not a theme change.
- **Layout chrome.** The fixed Material-icon header, segmented control, timeline
  rail, and zebra-striped tables have no native equivalent. Rendering them needs
  custom components, which would mean rewriting
  `tests/security/test_ui_rendering.py` and `tests/e2e/test_ui_smoke.py`.

The Stitch screens therefore remain the target for a future layout pass; the
palette and geometry are live today.

#### Fidelity gaps — mock content must not ship

Stitch invents plausible-looking data. Every item below is fictional and must be
replaced by the application's own values before any visual port:

| Mock in the screens | Reality |
|---|---|
| `HDFC Top 100 Fund`, `HDFC Small Cap Fund` | Not in the corpus. The registry is the AMC overview plus Large Cap, Flexi Cap, ELSS, and Mid Cap (ADR-003), and the Sources table omits the AMC overview entirely |
| Per-scheme chunk counts 42/48/44/46/39 | The real build indexes 86 chunks across the five pages |
| `v1.4.2 Production`, `Confidential · Audit Rigor Compliant`, `SEBI Compliance Engine`, `Grounding Confidence: 100%`, `0 hallucinations detected` | Operational and compliance claims the system cannot make and does not measure |
| `Auto-refreshed via canonical API daily at 18:30 UTC` | Refresh is build-time only; the freshness date is the image build date |
| SID/AMFI citations, `Roshi Jain`, `0.78%` TER | Facts from documents the assistant does not index; citations must resolve from the corpus (ADR-008) |
| Person avatar in the header | Implies authentication, which is out of scope (§1) |
| Tab labels `Pipeline Trace`, `Sources (5)`, `Evaluation Bench` | Product tabs are Ask, Pipeline, Sources, Evaluation; keep the product names |
| Evaluation row that re-prints a folio number and Aadhaar fragment | Blocked rows never echo the captured value in the app (§10.2); the recorded set is redacted |
| Status chips are all neutral except `Answered` | The shipped app still colours refusals amber, not-found blue, and PII red via Streamlit alerts; unifying them is a `cards.py` change |
| Manrope throughout | Not loaded; `theme.font` accepts only generic families and CSS injection is blocked |

**Change control.** `design/stitch/DESIGN.md` and `.streamlit/config.toml` change
together; screens are regenerated from the prompt rather than hand-edited.

---

## 8. LLM integration architecture

### 8.1 Provider abstraction

Only `generation/client.py` knows about provider SDKs. The rest of the system depends on a small protocol:

```python
class LLMClient(Protocol):
    name: str
    model: str

    def generate(self, request: LLMRequest) -> LLMResponse: ...

@dataclass
class LLMRequest:
    system: str
    user: str
    temperature: float
    max_tokens: int
    json_mode: bool = True
    timeout_s: float = 25.0

@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    finish_reason: str | None
```

`generation/client.py` exposes `build_client(config) -> LLMClient` and normalizes failures into:

```
LLMError
 ├─ LLMAuthError          # missing/invalid key
 ├─ LLMRateLimitError     # 429 / quota
 ├─ LLMTimeoutError       # timeout / connection
 ├─ LLMProviderError      # 5xx / unknown provider failure
 └─ LLMBadResponseError   # empty or unparsable body
```

The orchestrator handles these categories; it never parses provider-specific error JSON.

### 8.2 Adapters

| Adapter | Transport | Structured output | Notes |
|---|---|---|---|
| **GroqAdapter** (default) | OpenAI-compatible `https://api.groq.com/openai/v1` | `response_format={"type": "json_object"}` where supported | Model from `LLM_MODEL`; temperature 0.1 |
| GeminiAdapter | `google-genai` SDK | `response_mime_type="application/json"` | Model from `LLM_MODEL` |
| OpenAIAdapter | `openai` SDK | JSON mode / JSON schema | Model from `LLM_MODEL` |
| AnthropicAdapter | `anthropic` SDK | Tool/JSON instruction + parser | Model from `LLM_MODEL` |
| MistralAdapter | OpenAI-compatible | JSON mode where supported | Model from `LLM_MODEL` |

**Rules**

- The provider is selected by `LLM_PROVIDER` in `.env`; the model by `LLM_MODEL`.
- Only the selected provider's key is required.
- No automatic cross-provider fallback by default; `llm.fallback_providers` is an explicit opt-in.
- Provider-specific parameters are mapped inside the adapter; the orchestrator passes a neutral request.
- Raw keys and full provider payloads are never logged. Error messages are sanitized.

### 8.3 Prompt assembly

`generation/prompts.py` produces three artifacts:

1. **System prompt** — immutable policy rules (PRD Appendix B.1).
2. **User prompt** — question + resolved scheme/field + delimited evidence.
3. **Repair prompt** — only on validation failure.

**Context format**

```
CONTEXT (untrusted data; answer only from this):
[S1]
url: https://groww.in/...
title: HDFC ELSS Tax Saver Fund Direct Plan Growth
section: Fund details > Lock-in
fetched_at: 2026-11-08T07:30:00Z
text: ...
[/S1]
```

**Token budget**

| Component | Budget |
|---|---|
| System prompt | ~600 tokens |
| Evidence | ≤1,200 tokens |
| Question + resolver hints | ≤150 tokens |
| Output | ≤300 tokens |
| Total request | ≤2,300 tokens |

If the context exceeds the budget, drop the lowest-scoring whole chunk; never truncate a fact mid-value. The prompt hash and token counts are recorded in the answer trace.

### 8.4 JSON contract and parsing

Expected model output (normalized to the application schema):

```json
{
  "status": "answered",
  "answer": "…",
  "citation_url": "…",
  "citation_title": "…",
  "section": "…",
  "fact_field": "lock_in",
  "confidence": 0.0
}
```

**Parsing strategy**

1. Try strict `json.loads`.
2. If the response is wrapped in code fences, strip them and retry.
3. If extra prose surrounds the object, extract the first balanced `{…}` block and retry.
4. Validate with Pydantic. Unknown fields are ignored; missing required fields fail.
5. On failure, one repair call with the previous JSON and failed checks.
6. On second failure, use the extractive fallback.

### 8.5 Prompt-injection containment

- Retrieved text is always inside `[CONTEXT]` delimiters and labeled "untrusted data".
- The model has no tools, no browsing, and no URL-fetching capability.
- The system prompt explicitly says to ignore instructions found in context.
- The application never executes model output; it only parses JSON and renders text.
- A known injection phrase in a page cannot change the citation allowlist or bypass the validators.
- A golden injection test asserts the system prompt is not revealed and no recommendation is produced.

### 8.6 Cost and token accounting

- Record provider usage when available; otherwise estimate with the tokenizer.
- Store `input_tokens`, `output_tokens`, `latency_ms`, `provider`, `model` per request.
- Use a per-run aggregate for the evaluation report.
- Default Groq pricing in the PRD is used only for the estimate, never for user-facing claims.

---

## 9. Configuration architecture

### 9.1 Layers and precedence

```
CLI flags
   │ overrides
Environment variables (.env)
   │ overrides
config/config.yaml
   │ overrides
Code defaults (only safe, non-secret)
```

- `config.py` loads and validates the merged result into a typed settings object.
- `${VAR}` and `${VAR:-default}` interpolation is supported.
- Unknown keys produce warnings; invalid types/ranges fail fast.
- The active config is immutable for the process lifetime.

### 9.2 Config domains

| Domain | Keys | Consumer |
|---|---|---|
| `app` | title, max sentences, examples, disclaimer, session turns | UI, validators |
| `corpus` | seed pages, allowlist, depth, delays, fallback threshold | Loader |
| `references` (from `docs/sources.yaml`) | approved refusal/education URLs, purposes, verification status | Guardrails, Sources tab |
| `chunking` | strategy, token limits, overlap, exclusions, tokenizer | Chunker |
| `embedding` | model, device, batch, normalize, max length, cache | Embedder |
| `vector_db` | provider, path, collection, distance, batch | Indexer/Retriever |
| `retrieval` | top_k, final_k, threshold, MMR, context budget, filters | Retriever |
| `llm` | provider, model, temperature, max tokens, timeout, retries | LLM client |
| `guardrails` | PII, refusals, citation, sentence, repair, fallback | Guardrails |
| `logging` | level, JSON, trace dir, redaction, retention | Tracing/logging |
| `evaluation` | golden set path, report dir | Eval harness |

### 9.3 Config hash and drift detection

```
config_hash = sha256(canonical_json(non_secret_config))
```

- Stored in every artifact manifest.
- Embedding manifest, index manifest, and chunks must share the same relevant hash.
- Query serving compares the active config hash with the index manifest:
  - mismatch in embedding/chunking → block and instruct rebuild;
  - mismatch in unrelated UI keys → allow, but log a warning.
- The hash excludes API keys and other secrets.

### 9.4 Secrets

- `.env` only; gitignored.
- Loaded once at process start; never placed in `config.yaml`, prompts, traces, or logs.
- UI shows only whether a provider key is configured (`present/missing`), never the value.
- Provider errors are sanitized before display.

### 9.5 Profiles

| Profile | Purpose | Overrides |
|---|---|---|
| `local` (default) | Demo on a laptop | `device: auto`, persistent Chroma, verbose inspector |
| `ci` | Tests/eval | Tiny fixtures, deterministic temp dirs, no network, fake LLM |
| `hosted` | Optional Streamlit Cloud / HF Spaces | Prebuilt index or startup build, secrets from platform env |

Profiles are implemented as config overlays, not separate code paths.

### 9.6 Reference link registry

Refusal and education links are versioned separately from the indexed corpus. `docs/sources.yaml` is the machine-readable registry (the submission `sources.md`/`.csv` are generated from it); each entry records `id`, `url`, `purposes`, `verified`, `verified_at`, and `scraped: false`.

```yaml
references:
  - id: sebi_investor_education
    url: "https://investor.sebi.gov.in/"
    purposes: ["advice_refusal"]
    verified: true
    scraped: false
  - id: hdfc_factsheet
    url: "https://www.hdfcfund.com/statutory-disclosure/factsheet"
    purposes: ["performance_refusal"]
    verified: true
    scraped: false
```

Rules:

- Guardrails load only `verified: true` links; unverified entries are ignored.
- A refusal never accepts a link from the model or the user.
- If no verified link exists for a purpose, the refusal falls back to a text-only message and logs a configuration warning.
- Reference pages are **not** embedded, not stored in Chroma, and not treated as factual corpus evidence.

---

## 10. Security, privacy, and compliance architecture

### 10.1 Security boundaries and controls

| Boundary | Threat | Control |
|---|---|---|
| Browser → app | XSS via model text or page text | Escape all text; render links as text + `noopener noreferrer`; no `unsafe_allow_html` |
| User → app | Prompt injection / policy bypass | System rules, intent guard, output validation, no tools |
| Groww → ingestion | Malicious/accidental instructions in page text | Context delimiters; "data not instructions"; no execution |
| App → LLM | Secret/PII leakage | PII screen before call; only public context; keys never in prompt |
| LLM → app | Hallucinated citation/fact | Schema + allowlist + evidence match + fallback |
| App → filesystem | PII persistence | Blocked before trace; redaction; retention policy |
| Config → app | Secret exposure | `.env` only; masked UI; sanitized errors |

### 10.2 PII data flow

```
User input
   │
   ▼
PII screen (regex + context)
   │
   ├─ HIT ──▶ fixed blocked_pii message
   │          └─ security log: trace_id, pii_type, salted hash only
   │             (no raw query, no embedding, no LLM call, no filesystem trace)
   │
   └─ PASS ──▶ intent → resolver → embed → retrieve → prompt → LLM
                         │
                         └─ trace stores normalized query only
```

**Architectural guarantees**

- The PII guard is the **first** stage after input length validation.
- A blocked request never reaches the embedder, Chroma, the LLM, or the answer trace writer.
- Logs use a safe schema; raw queries are not logged at INFO.
- The UI message explicitly says nothing was stored.
- Corpus PII (emails/phones) is scrubbed during extraction.

### 10.3 Citation and URL safety

- Only `http`/`https` URLs are accepted.
- Domain must match the corpus allowlist or the approved reference allowlist, depending on response type.
- URL length and character set are validated.
- The displayed URL always comes from an evidence record, not directly from model text.
- External links open in a new tab with `rel="noopener noreferrer"`.
- No URL shorteners, redirects, or `data:`/`javascript:` schemes.

### 10.4 Scraping compliance

- Exactly five URLs; no link following.
- `robots.txt` check before fetch; polite delay; identifying User-Agent.
- Cache raw HTML to avoid repeat requests.
- No login, paywall bypass, or personal data collection.
- If access is disallowed, stop and document; fall back to an approved manual snapshot only with explicit approval.
- Attribution to Groww page title + URL; no logos or implied endorsement.

### 10.5 Data retention

| Data | Retention | Deletion |
|---|---|---|
| Raw HTML/documents/chunks/embeddings | Demo lifetime | `scripts/reset_data.py --stage …` |
| Chroma | Rebuildable | `--rebuild` drops the collection |
| Traces | 7 days | `scripts/purge_traces.py` |
| Eval results | Demo lifetime | Manual |
| Secrets | Never persisted by app | `.env` managed by operator |

---

## 11. Observability and traceability

### 11.1 Trace model

| ID | Created | Links |
|---|---|---|
| `run_id` | Ingestion run | Pipeline manifest |
| `doc_id` | Page | Raw HTML, document, chunks |
| `chunk_id` | Chunk | Embedding row, Chroma record |
| `trace_id` | Query | Retrieval trace, answer trace, prompt file, UI card |
| `eval_run_id` | Evaluation run | Per-query results, scorecard |

### 11.2 Structured logs

One JSON object per event:

```json
{
  "ts": "2026-11-08T07:35:00.123Z",
  "level": "INFO",
  "trace_id": "ans_...",
  "stage": "retrieve",
  "event": "retrieval_completed",
  "latency_ms": 84,
  "candidates": 12,
  "selected": 3,
  "top_score": 0.81,
  "filter_fallback": false
}
```

**Never logged:** API keys, raw blocked PII, full provider payloads, system prompt contents.

### 11.3 Metrics

| Metric | Type | Source |
|---|---|---|
| `ingest_pages_total` | counter | Pipeline manifest |
| `ingest_failures_total` | counter | Load report |
| `chunks_total` / `indexed_total` | gauge | Chunk report |
| `query_total` | counter | Answer traces |
| `query_status_total{status}` | counter | Answer traces |
| `retrieval_top_score` | histogram | Retrieval trace |
| `answer_latency_ms` | histogram | Answer trace |
| `llm_tokens_total` | counter | Usage |
| `guardrail_blocks_total{type}` | counter | Security log |
| `citation_repairs_total` | counter | Answer trace |

For the demo, these can be derived from artifact files; a Prometheus endpoint is optional and out of scope.

### 11.4 Pipeline Inspector mapping

| UI sub-tab | Reads |
|---|---|
| Load | `data/raw/*`, `documents.jsonl`, `load_report.md` |
| Chunk | `chunks.jsonl`, `excluded.jsonl`, `chunk_stats.json`, `chunk_report.md` |
| Embed | `embedding_manifest.json`, `embedding_preview.json`, `embed_report.md` |
| Index | `index_manifest.json`, `index_report.md`, Chroma count |
| Retrieve | Latest/selected `traces/retrieval/*.json`, calibration report |
| Generate | `traces/prompts/*.txt`, `traces/answers/*.json` |
| Evaluate | `eval/results_*.json`, `docs/eval_report.md` |

The inspector is **read-only**. It never triggers a pipeline stage unless the admin rebuild control is enabled.

---

## 12. Performance and capacity

### 12.1 Latency budget (target p95 ≤5 s)

| Stage | Budget | Notes |
|---|---|---|
| Input + PII + intent | 20 ms | Regex; no network |
| Resolve | 10 ms | In-memory aliases |
| Query embedding | 50 ms | CPU MiniLM, cached model |
| Chroma top-12 | 40 ms | Local, five-page index |
| Context assembly | 20 ms | Dedupe/MMR |
| LLM generation | 3,500 ms | Groq p95; slower providers ≤8 s |
| Validation/repair | 100 ms | Local |
| Trace write | 20 ms | Local JSON |
| **Total** | **~3.8 s** | Well under 5 s for Groq |

### 12.2 Build performance

| Operation | Target |
|---|---|
| Fetch five pages | < 20 s (2 s delay + responses) |
| Extract | < 2 s |
| Chunk | < 1 s |
| Embed (~100–300 chunks) | < 10 s CPU |
| Index | < 5 s |
| **Cold build** | **< 60 s** |

### 12.3 Capacity assumptions

- Corpus: 5 pages, likely 100–300 chunks.
- Embedding matrix: < 1 MB (`300 × 384 × 4` bytes).
- Chroma: local SQLite, tens of MB at most.
- Concurrency: single-user demo; cap concurrent queries at 2 to avoid CPU thrash.
- Memory: MiniLM + Python + Chroma under ~1 GB.

### 12.4 Caching

| Cache | Key | Invalidation |
|---|---|---|
| Raw HTML | `page_id + date` | `--refetch` |
| Embedding | `content_hash + model` | model/config change |
| Model | process lifetime | restart |
| Chroma client/collection | process lifetime | rebuild/restart |
| Prompt | no cache (per query) | — |

### 12.5 Scalability path (not in v1)

If the corpus grows beyond the demo:

1. Move artifacts to object storage with content-addressed names.
2. Replace local Chroma with a server deployment or a managed vector store.
3. Add a reranker (cross-encoder) after recall.
4. Batch/async query handling behind an API.
5. Add auth and per-user retention policy.

These are explicitly out of scope for the current architecture.

---

## 13. Deployment and runtime

### 13.1 Local topology

```
┌──────────────────────────── Windows/macOS/Linux ────────────────────────────┐
│                                                                             │
│  Terminal 1: python -m scripts.run_pipeline     (build, then exits)         │
│       └─ writes data/ + Chroma                                              │
│                                                                             │
│  Terminal 2: streamlit run src/mf_faq/ui/app.py                             │
│       ├─ loads config + embedder + Chroma (read-only)                       │
│       ├─ serves http://localhost:8501                                       │
│       └─ calls Groq over HTTPS                                              │
│                                                                             │
│  .env (secrets)        config/config.yaml (non-secrets)                    │
│  data/ (artifacts)     docs/ (deliverables)                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 13.2 Startup sequence

1. Load and validate config; fail on missing `LLM_MODEL`/provider key.
2. Load embedding model (warm cache; no network if cached).
3. Verify Chroma path, collection, count, and manifest against active config.
4. Verify `chunks.jsonl` and embedding IDs are consistent.
5. Show a setup message if any check fails; do not serve queries against a partial index.
6. Initialize the LLM client and UI session state.

**Health check command:**

```bash
python -m mf_faq.healthcheck
```

Checks: config validity, model availability, Chroma writability/count, index manifest, provider key presence (masked), and trace directory permissions.

### 13.3 Commands

```bash
# Full build
python -m scripts.run_pipeline

# Stage-by-stage (demo-friendly)
python -m scripts.run_ingest --refetch
python -m scripts.run_chunk --rechunk
python -m scripts.run_embed --reembed
python -m scripts.run_index --rebuild

# Serve
streamlit run src/mf_faq/ui/app.py

# Evaluate
python -m eval.run --config config/config.yaml

# Maintenance
python -m scripts.purge_traces --days 7
python -m scripts.reset_data --stage chroma
```

### 13.4 Hosted option

**Streamlit Community Cloud / Hugging Face Spaces**

- Build the index during deployment or on first startup.
- Store secrets in the platform's secret manager, not in the repo.
- Treat the filesystem as ephemeral; rebuild or ship a prebuilt index.
- Disable admin rebuild in hosted mode.
- Document the cold-start behavior in the README.

**Not supported:** multi-user auth, persistent user chat, horizontal scaling, or a public write API.

### 13.5 Configuration matrix

| Setting | Local | CI | Hosted |
|---|---|---|---|
| `embedding.device` | `auto` | `cpu` | `cpu` |
| `vector_db.persist_dir` | `data/chroma` | temp dir | `/tmp/chroma` or prebuilt |
| `logging.json` | true | true | true |
| `DEMO_ADMIN` | false | false | false |
| LLM | real Groq | fake adapter | real provider |
| Network | allowed for build | blocked | allowed for LLM |

---

## 14. Architecture decision records (ADRs)

### ADR-001 — Separate build plane from serve plane

**Status:** Accepted

**Context:** The demo must show every RAG stage explicitly, and query serving must be fast and read-only.

**Decision:** Ingestion runs as an offline CLI that writes artifacts. The Streamlit app only reads the index and artifacts; it does not fetch or index during a query.

**Consequences:**
- Clear stage boundaries and reproducible demos.
- A query cannot accidentally mutate the corpus.
- Rebuilding is an explicit operator action.
- The UI must handle "index missing/stale" states.

---

### ADR-002 — JSONL artifacts are the source of truth; Chroma is derived

**Status:** Accepted

**Context:** Chroma is excellent for search but opaque for audit. The brief requires each stage's data to be inspectable.

**Decision:** Persist documents, chunks, embeddings, and manifests as files. Chroma is a derived index rebuilt from chunks + vectors.

**Consequences:**
- Full audit trail and easy inspector rendering.
- Index can be dropped/rebuilt without data loss.
- Slight duplication (text in JSONL and Chroma); accepted for a five-page corpus.
- Consistency checks are required between artifacts and the collection.

---

### ADR-003 — Fixed seed registry instead of a crawler

**Status:** Accepted

**Context:** The brief permits exactly five pages and forbids following internal links.

**Decision:** The loader accepts a typed list of five seed records from config. No recursive crawl function exists.

**Consequences:**
- Trivial to prove compliance (fetch-count test). The test counts **top-level document fetches** (plus explicit robots checks), not subresource requests; Playwright may load page assets but never follows page links.
- Adding a page is a config change plus a deliberate rebuild.
- No discovery, sitemap parsing, or link extraction.
- Site navigation changes cannot expand the corpus accidentally.

---

### ADR-004 — Structure-aware atomic fact chunks

**Status:** Accepted

**Context:** Fixed windows merge facts; sentence splitting loses labels and breaks tables.

**Decision:** Emit one chunk per key-value fact/table row/list item, with a section breadcrumb and canonical field. Exclude non-factual content from the index.

**Consequences:**
- High retrieval precision and clean citations.
- Chunk count is larger than naive splitting; acceptable at this scale.
- Requires a reliable DOM extractor and field taxonomy.
- Strategies remain swappable for comparison.

---

### ADR-005 — Local MiniLM with a model-agnostic embedder interface

**Status:** Accepted

**Context:** The brief mandates `all-MiniLM-L6-v2`; the architecture must allow changing it later.

**Decision:** Wrap the model behind `Embedder`; read model name, max length, and device from config. Persist a manifest and block queries on model/dimension mismatch.

**Consequences:**
- Meets the requirement and remains configurable.
- 384-d vectors are cheap and fast on CPU.
- Changing the model requires re-chunking if its tokenizer/limit differs.
- MiniLM's numeric/table weakness is mitigated by atomic chunks and metadata filters.

---

### ADR-006 — Provider adapters with model ID from `.env`

**Status:** Accepted

**Context:** Groq is the default; Gemini/OpenAI/Anthropic/Mistral must be selectable; the model ID must come from `.env`.

**Decision:** Define a neutral `LLMClient` protocol and provider adapters. `LLM_PROVIDER` and `LLM_MODEL` come from environment variables.

**Consequences:**
- No vendor lock-in and no hardcoded model ID.
- Provider-specific quirks stay in adapters.
- Structured-output support varies; the parser/repair layer must handle plain text fallbacks.
- Only one key is required at a time.

---

### ADR-007 — Guardrails outside the LLM, with deterministic fallback

**Status:** Accepted

**Context:** A prompt instruction alone cannot guarantee no advice, no PII, or exactly one citation.

**Decision:** Enforce PII/intent before retrieval and validate output after generation. On failure: one repair, then an extractive answer from the top chunk.

**Consequences:**
- Safety does not depend on model compliance.
- Slightly more code and one extra LLM call on failures.
- Extractive fallback guarantees a usable, grounded response.
- Refusals are consistent and testable.

---

### ADR-008 — Server-side citation resolution

**Status:** Accepted

**Context:** LLMs can invent or alter URLs, and the product promises one verified source.

**Decision:** The LLM's URL is advisory. The service maps the answer to a retrieved evidence record and renders that record's URL/title/section. Invalid model URLs are discarded. Implemented in `guardrails/citation.py` and called by `AnswerService` after parsing.

**Consequences:**
- Citation validity can be 100% by construction.
- The citation may point to the page rather than an exact section anchor (Groww anchors are not guaranteed).
- The model still needs to choose the right page/fact; retrieval quality remains critical.

---

### ADR-009 — Performance content excluded from the index

**Status:** Accepted

**Context:** The corpus pages contain returns/NAV/ratings that the product must never use.

**Decision:** Store performance chunks with `indexed: false` and a reason, but never retrieve them. The prompt also forbids performance claims.

**Consequences:**
- Defense in depth against performance answers.
- The inspector can show what was excluded and why.
- Some legitimate NAV facts are unavailable; accepted by scope.

---

### ADR-010 — Single-process Streamlit app with synchronous queries

**Status:** Accepted

**Context:** A tiny demo UI with a five-page corpus does not need an API server or job queue.

**Decision:** Serve with Streamlit; call the query service in-process; cap concurrent queries at 2.

**Consequences:**
- Minimal moving parts and easy local demo.
- No horizontal scaling or background jobs.
- Long LLM calls occupy a request; acceptable for a single user.
- Hosting is optional and limited.

---

### ADR-011 — One-page evidence rule

**Status:** Accepted

**Context:** Exactly one citation is required, but retrieval can surface facts from multiple pages.

**Decision:** Select the best-supported page and build context only from that page for factual answers.

**Consequences:**
- Citation is unambiguous.
- Multi-page synthesis is not supported (and is out of scope).
- If the best fact is split across pages, the top page wins; the other is ignored.

---

### ADR-012 — Session context only, never persisted

**Status:** Accepted

**Context:** Follow-ups are useful, but storing chat raises privacy and retention concerns.

**Decision:** Keep messages and the last resolved scheme in Streamlit `session_state` only; reset on clear/session end.

**Consequences:**
- No user history on disk.
- No cross-session memory.
- Follow-up support is limited to 3 turns.

---

## 15. Testing architecture

### 15.1 Test layers

| Layer | What it proves | Network | Location |
|---|---|---|---|
| **Unit** | Parsers, chunk boundaries, token counts, resolvers, PII regex, sentence counter, citation validator | No | `tests/unit/` |
| **Contract** | Pydantic schemas round-trip; artifact manifests valid | No | `tests/contract/` |
| **Integration** | Load → extract → chunk → embed → index on saved fixtures | No | `tests/integration/` |
| **Query integration** | Retrieval + fake LLM + validators + traces | No | `tests/integration/` |
| **End-to-end** | Streamlit/service path with a fake provider | No | `tests/e2e/` |
| **Live smoke** | One real Groq call and five real fetches (manual/CI opt-in) | Yes | `tests/live/` |
| **Evaluation** | 25-query golden set, metrics, gates | Configurable | `eval/` |
| **Security** | PII, injection, citation tampering, URL allowlist | No | `tests/security/` |
| **Performance** | p95 latency and build time budgets | No | `tests/perf/` (manual) |

### 15.2 Fixtures and determinism

- Commit small fixtures: five extracted documents, a chunk sample, a golden set, and a fake LLM response set.
- Do **not** commit raw HTML if licensing/size is a concern; use a documented snapshot command instead.
- Use temp directories for Chroma/artifacts in tests.
- Freeze time and random seeds where possible.
- Fake LLM adapter returns scripted JSON, including invalid/advice/citation-error cases.
- Tokenizer/model tests use a cached model; CI can skip with `-m "not model"` if unavailable.

### 15.3 Critical test cases

| Area | Cases |
|---|---|
| Loader | Exactly 5 fetches; off-domain redirect rejected; retry; Playwright fallback; no link following |
| Extractor | KV pairs, table rows, nested headings, boilerplate removal, corpus PII scrub |
| Chunker | No KV split; no table-row split; token limits; deterministic IDs; exclusions |
| Embedder | 384 dims, normalized, no NaN, cache hit/miss, manifest mismatch |
| Index | Count, idempotent upsert, metadata filters, drift guard, rebuild |
| Resolver | All aliases; legacy Flexi slug; ambiguous scheme; follow-up; AMC-wide facts |
| Retriever | Threshold, dedupe, MMR, filter fallback, one-page rule, not-found |
| PII | PAN/email/OTP block; amounts/percent/date false positives pass |
| Intent | Advice/performance/out-of-scope/ambiguous |
| Generation | Valid JSON, code-fenced JSON, extra prose, repair path, extractive fallback |
| Citation | Invented URL rejected; off-allowlist rejected; evidence URL rendered |
| UI | Disclaimer visible, 3 examples, evidence panel, trace ID, no unsafe HTML |
| Eval | Scorecard totals, gate failure exit code |

### 15.4 CI gates

```bash
pytest tests/unit tests/contract tests/integration
pytest tests/security
python -m eval.run --config config/config.yaml --fail-on-gate
```

Hard gates (from PRD §15.3): citation validity 100%, one citation 100%, sentence limit 100%, advice/performance refusal 100%, PII persistence 0, factual accuracy ≥90%, recall@5 ≥90%, p95 ≤5 s.

### 15.5 Test doubles

| Dependency | Double |
|---|---|
| Groww | Saved HTML fixtures |
| MiniLM | Real cached model for integration; deterministic hash stub for unit |
| Chroma | Temp persistent client or in-memory client |
| LLM | Fake adapter with scripted responses/errors |
| Clock | Fixed UTC clock for IDs/dates |
| Secrets | Dummy env vars |

---

## 16. Requirements traceability

| PRD requirement | Architectural component(s) | Verification |
|---|---|---|
| FR-01 five seeds, no crawl | `SourceRegistry`, `PageLoader` | fetch-count unit test; manifest seed hash |
| FR-02 raw + clean + metadata | `PageLoader`, `Extractor`, `ArtifactStore` | artifact existence + schema tests |
| FR-03 structure-aware chunks | `StructureAwareChunker` | chunk invariants |
| FR-04 configurable chunking | `ChunkingStrategy`, `ChunkConfig` | strategy switch test |
| FR-05 MiniLM embeddings | `Embedder` | dims/norm test |
| FR-06 embeddings + ID map | `Embedder`, `EmbeddingManifest` | count/ID tests |
| FR-07 Chroma index | `ChromaStore` | count/filter tests |
| FR-08 configurable model/DB | `config.py`, factory | config-switch test |
| FR-09 scheme/field aliases | `SchemeResolver`, `FieldResolver` | alias table tests |
| FR-10 top-k retrieval | `Retriever` | retrieval tests/traces |
| FR-11 one valid citation | `CitationResolver`, `OutputValidator` | citation tests |
| FR-12 ≤3 sentences | `SentenceValidator` | validator tests |
| FR-13 last updated | `AnswerService` | response schema/UI test |
| FR-14 advice refusal | `IntentGuard`, `RefusalTemplates` | golden set |
| FR-15 performance refusal | `IntentGuard`, `PerformanceFilter` | golden set |
| FR-16 PII block | `PIIScreen`, `SecurityLog` | PII tests, trace inspection |
| FR-17 not found | `AnswerService`, `Retriever` | golden set |
| FR-18 clarify | `SchemeResolver` | golden set |
| FR-19 three examples/disclaimer | `ui/app.py` | UI test |
| FR-20 evidence + trace ID | `ui/app.py`, `TraceReader` | UI test |
| FR-21 pipeline inspector | `ui/app.py`, artifact readers | inspector checklist |
| FR-22 secrets only env | `config.py`, logging | secret scan |
| FR-23 provider adapters | `generation/client.py` | adapter smoke tests |
| FR-24 JSON validation/repair | `OutputValidator`, `AnswerService` | fake LLM tests |
| FR-25 injection defense | `PromptBuilder`, `OutputValidator` | security test |
| FR-26 traces | `TraceWriter` | trace schema test |
| FR-27 eval harness | `eval/run.py` | scorecard test |
| FR-28 source registry | `docs/sources.*` | link verification |
| FR-29 docs | repository docs | checklist |
| FR-30 out-of-scope | `IntentGuard` | golden set |
| FR-31 follow-up | session state | UI test |
| FR-32 admin rebuild | UI flag + localhost guard | UI test |

---

## 17. Core interfaces and build order

### 17.1 Core interfaces

```python
# config.py
class Settings:
    app: AppConfig
    corpus: CorpusConfig
    chunking: ChunkConfig
    embedding: EmbeddingConfig
    vector_db: VectorDBConfig
    retrieval: RetrievalConfig
    llm: LLMConfig
    guardrails: GuardrailConfig
    logging: LoggingConfig
    config_hash: str

def load_settings(config_path: str = "config/config.yaml") -> Settings: ...

# ingest/loader.py
class PageLoader(Protocol):
    def load(self, seed: SeedPage) -> LoadedPage: ...

# ingest/extract.py
class Extractor(Protocol):
    def extract(self, page: LoadedPage) -> SourcePage: ...

# chunking/chunker.py
class ChunkingStrategy(Protocol):
    def chunk(self, doc: SourcePage, blocks: list[Block], cfg: ChunkConfig) -> list[Chunk]: ...

# embeddings/embedder.py
class Embedder(Protocol):
    def encode_documents(self, texts: list[str]) -> np.ndarray: ...
    def encode_query(self, text: str) -> np.ndarray: ...
    @property
    def manifest(self) -> EmbeddingManifest: ...

# index/chroma_store.py
class VectorStore(Protocol):
    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None: ...
    def query(self, vector: np.ndarray, k: int, where: dict | None) -> list[Hit]: ...
    def count(self) -> int: ...

# retrieval/retriever.py
class Retriever(Protocol):
    def retrieve(self, query: ResolvedQuery) -> RetrievalResult: ...

# guardrails/citation.py
class CitationResolver(Protocol):
    def resolve(
        self,
        llm_citation: str | None,
        evidence: list[RetrievedEvidence],
        allowlist: set[str],
    ) -> Citation: ...

# generation/client.py
class LLMClient(Protocol):
    def generate(self, request: LLMRequest) -> LLMResponse: ...

# generation/answer.py
class AnswerService:
    def answer(self, request: QueryRequest) -> AnswerResponse: ...
```

### 17.2 Build order and dependency gates

| Step | Build | Depends on | Gate |
|---|---|---|---|
| 1 | `contracts.py`, `config.py`, `errors.py`, `ids.py` | — | Schema/config tests |
| 2 | `loader.py`, `extractor.py` | 1 | Five fixtures load; coverage report |
| 3 | `chunker.py` | 2 | Chunk invariants + report |
| 4 | `embedder.py` | 1, 3 | 384-d/norm tests |
| 5 | `chroma_store.py` | 4 | Count/filter/idempotency |
| 6 | `resolver.py`, `retriever.py` | 5 | Recall@5 on fixture queries |
| 7 | `pii.py`, `intent.py`, `prompts.py`, `client.py`, `validators.py`, `answer.py` | 6 | Golden-set behavior |
| 8 | `ui/app.py` + trace readers | 7 | Inspector/UI checklist |
| 9 | `eval/run.py` | 8 | Hard gates |
| 10 | Docs, demo, packaging | 9 | Definition of done |

**Parallel opportunities:** loader/extractor fixtures can be prepared while config/contracts stabilize; UI shell can be built against a fake `AnswerService`; documentation can be written from the PRD before the pipeline is complete.

### 17.3 Data flow invariants

These must always hold; tests assert them:

1. `len(fetched_pages) == 5`; every `url` ∈ seed allowlist.
2. `chunk.doc_id` exists in `documents.jsonl`; `chunk.url == document.url`.
3. `chunk.token_count <= chunking.max_tokens` for indexed chunks.
4. `indexed == false` implies `content_class != factual`.
5. `len(embeddings) == len(indexed_chunks)`.
6. `embedding_ids[i] == indexed_chunks[i].chunk_id`.
7. Chroma count == indexed chunk count.
8. Every factual answer's citation URL ∈ evidence URLs.
9. Every answer's `answer` has ≤3 sentences.
10. Blocked PII never appears in any trace file.
11. Every trace has a `config_hash` matching the active config.
12. `index_manifest.model == embedding_manifest.model` and dimensions match.

---

## 18. Architecture risks and open questions

| # | Risk / question | Architectural impact | Mitigation / default |
|---|---|---|---|
| A1 | Groww DOM changes break extractors | Chunk quality and coverage | Snapshot fixtures; selector fallbacks; coverage assertions; manual review |
| A2 | Groww blocks automated access | Build plane unavailable | Polite fetching; manual snapshot with approval; document limitation |
| A3 | Playwright fallback adds heavy dependency | Install size/startup | Optional extra; only invoked when text is thin |
| A4 | MiniLM weak on numeric/table retrieval | Answer accuracy | Atomic chunks, field filters, synonym map, threshold calibration |
| A5 | Chroma concurrent access from Streamlit threads | Read consistency | One client per process; read-only after build; limit concurrency |
| A6 | LLM provider JSON mode differences | Parse failures | Neutral prompt + tolerant parser + one repair + extractive fallback |
| A7 | Citation section granularity | UI may link to page, not section | Display section label separately; no fabricated anchors |
| A8 | Statement-download fact missing from AMC page | One required FAQ unavailable | Not-found + official reference; no sixth-page scrape |
| A9 | Growing corpus beyond five pages | Chunk/index design still works, UI setup does not | Out of scope; documented extension path |
| A10 | Hosted filesystem is ephemeral | Index disappears on restart | Prebuild index or startup build; document cold start |
| A11 | PII false positives on amounts/dates | Bad UX | Context-aware regex + negative tests |
| A12 | Multi-field questions overwhelm 3-sentence limit | Partial answers | Answer primary field; include second only if it fits; log `multi_field` |
| A13 | Model returns advice despite prompt | Safety failure | Output validator forces refusal; eval gate |
| A14 | Source list scope ambiguity (Groww vs official) | Citation policy | PRD decision: Groww facts + official refusal links; recorded |
| A15 | Secrets accidentally logged | Security failure | Env-only, sanitized errors, log scan, tests |

**Open questions to confirm before build**

1. **Hosted demo:** is a local app + video sufficient, or is Streamlit Cloud required?
2. **Snapshot fallback:** if Groww blocks fetching, may we use a manually saved snapshot for the demo?
3. **Statement download:** if C1 lacks instructions, is not-found acceptable, or should the official AMC help page be added to the **reference** list only?
4. **Multi-field queries:** answer the first field only, or attempt both within the 3-sentence limit?
5. **Model fallback:** should Groq failures automatically try Gemini/OpenAI, or return a safe error?
6. **Inspector scope:** should the Pipeline tab expose raw HTML, or only cleaned text and artifacts (to reduce accidental PII/licensing exposure)?

---

## 19. Appendix — Runtime checklist

### 19.1 Before demo

- [ ] `.env` has the selected provider key and `LLM_MODEL`.
- [ ] `python -m mf_faq.healthcheck` passes.
- [ ] Five pages fetched and coverage report reviewed.
- [ ] Chunk report shows no invariant violations.
- [ ] Embedding manifest and Chroma manifest match.
- [ ] Golden set has been re-verified against current pages.
- [ ] Last-updated dates are current.
- [ ] Advice, performance, PII, and not-found paths manually tested.
- [ ] Pipeline Inspector opens all seven sub-tabs.
- [ ] Disclaimer visible on the Ask tab.
- [ ] No secrets in logs, traces, or the repository.

### 19.2 Key file map

| Concern | File |
|---|---|
| Configuration | `config/config.yaml`, `.env`, `src/mf_faq/config.py` |
| Contracts | `src/mf_faq/contracts.py` |
| Ingestion | `src/mf_faq/ingest/`, `src/mf_faq/chunking/` |
| Embeddings/index | `src/mf_faq/embeddings/`, `src/mf_faq/index/` |
| Retrieval | `src/mf_faq/retrieval/` |
| Prompts/generation | `src/mf_faq/generation/` |
| Guardrails | `src/mf_faq/guardrails/` |
| Citation resolution | `src/mf_faq/guardrails/citation.py` |
| Tracing | `src/mf_faq/tracing/` |
| UI | `src/mf_faq/ui/app.py` |
| UI design reference | `design/stitch/`, `docs/ui-design-prompt.md` (see §7.11) |
| Evaluation | `eval/` |
| Tests | `tests/` |
| Docs | `README.md`, `PRD.md`, `architecture.md`, `docs/` |

---

## 20. Change log

| Version | Date | Change |
|---|---|---|
| 1.0 | 08 Nov 2026 | Initial architecture derived from `PRD.md`; components, data flow, interfaces, guardrails, deployment, testing, ADRs, and traceability defined |
| 1.1 | 10 Nov 2026 | Added §7.11 UI design system and screen inventory from the Google Stitch export (`design/stitch/`): tokens, type scale, per-tab anatomy, Streamlit mapping, and the mock-content gaps that must not ship |
| 1.2 | 10 Nov 2026 | Applied the Groww palette and geometry to `.streamlit/config.toml`, added the `tests/unit/test_theme_config.py` palette guard, and recorded what a Streamlit theme cannot reproduce (generic fonts only, four semantic alert colours) |
