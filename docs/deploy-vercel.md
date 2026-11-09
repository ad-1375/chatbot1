# Deploy on Vercel (container image)

This app deploys to Vercel as a **container image**, not as a static site or a
Node/Python serverless bundle.

Why: Streamlit is a long-lived Python process that serves its frontend over a
WebSocket (`/_stcore/stream`). Vercel's static and standard serverless presets
start a process per request, so the Streamlit server would never keep running.
The [Container Runtime](https://vercel.com/docs/functions/runtimes/container)
runs the image as a Vercel Function on Fluid compute and routes HTTP plus
WebSocket traffic to the port the container listens on (`$PORT`, default `80`).

Deployment files already in the repo:

| File | Purpose |
|---|---|
| `Dockerfile` | `python:3.12-slim`, CPU-only torch, editable install, bundled MiniLM snapshot, corpus built at image build time |
| `.dockerignore` | keeps `.env`, `data/`, tests, and `.venv` out of the build context (but keeps `models/`) |
| `models/all-MiniLM-L6-v2/` | vendored 384-d embedding model, loaded via `EMBEDDING_MODEL_PATH` with no Hub access |
| `vercel.json` | `{"framework": "container"}` so the root `Dockerfile` is used |

The five Groww pages are fetched **during the image build**, so no `data/`
artifacts are committed and no provider key is needed to build. The embedding
model ships inside the image (see §6) because the build network cannot reach
`huggingface.co`. The provider key is only read at runtime.

---

## 1. Before you start

- Vercel account (Hobby is enough for a demo).
- The repo pushed to GitHub: `https://github.com/ad-1375/chatbot1`.
- Your Groq key (the same value as `GROQ_API_KEY` in your local `.env`).
- Optional: Node.js 18+ if you deploy with the Vercel CLI instead of the dashboard.
- Optional: Docker Desktop only if you want to build and test the image locally.

`.env` is **not** used on Vercel and is excluded from the image. Runtime settings
come from Vercel environment variables, which `Settings.load()` reads from the
process environment (dotenv never overrides real env values).

---

## 2. Deploy from the dashboard

1. Open **https://vercel.com/new** → **Import Git Repository** → choose
   `ad-1375/chatbot1`.
2. **Framework Preset**: `Container`.
   A root `Dockerfile` normally selects this automatically. If Vercel picked a
   different preset, set it to **Container** (this writes `"framework":
   "container"`, which `vercel.json` already contains).
3. **Root Directory**: `.` (repository root — leave the default).
4. **Build and Output Settings**: leave everything default. Vercel builds the
   `Dockerfile`; there is no install command, build command, or output directory
   to set.
5. **Environment Variables** — add these for **Production** (repeat for
   **Preview** if you want preview deployments to answer questions):

   | Name | Value | Environments | Notes |
   |---|---|---|---|
   | `LLM_PROVIDER` | `groq` | Production | required; one of `groq`, `gemini`, `openai`, `anthropic`, `mistral` |
   | `LLM_MODEL` | `qwen/qwen3.8-27b` | Production | required; must be a model id the provider serves |
   | `GROQ_API_KEY` | your key | Production | required; tick **Sensitive** |
   | `DEMO_ADMIN` | `false` | Production | optional; defaults to `false` |
   | `VERCEL_SUPPORT_LARGE_FUNCTIONS` | `1` | Production | optional; only for the 5 GB Large Functions beta, normally not needed for containers |

   Only the key for the active provider is required. If you switch
   `LLM_PROVIDER`, add that provider's key
   (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or
   `MISTRAL_API_KEY`) and redeploy.

   **Sensitive** variables can be created and used but never read back from the
   dashboard or CLI after saving — good default for `GROQ_API_KEY`.

6. Click **Deploy**. The first build downloads torch, builds the corpus with the
   bundled MiniLM model, fetches the five pages, and indexes 86 chunks; expect
   roughly 5–10 minutes (the ~90 MB model snapshot is already in the repo, so no
   Hugging Face download is attempted).

7. When the build finishes, open the deployment URL. `https://<project>.vercel.app`
   serves the Ask tab.

If a preview deployment shows a Vercel login wall, that is **Deployment
Protection** (Vercel Authentication), not the app. Production deployments are
public by default; for previews, turn protection off under **Project → Settings →
Deployment Protection** or use the production URL.

---

## 3. Deploy with the Vercel CLI

```powershell
npm install -g vercel
vercel login
vercel link          # select your scope, then create/link the chatbot1 project
```

Add the environment variables (each command reads the value from stdin, so the
key never lands in your shell history file):

```powershell
'groq'                 | vercel env add LLM_PROVIDER production
'qwen/qwen3.8-27b'     | vercel env add LLM_MODEL production
'<your-groq-key>'      | vercel env add GROQ_API_KEY production
'false'                | vercel env add DEMO_ADMIN production
```

Add the same names with `preview` instead of `production` if preview builds
should work. Then confirm the framework preset and deploy:

```powershell
vercel project update --framework container --yes
vercel --prod
```

`vercel --prod` builds the image remotely and prints the production URL. Useful
follow-ups:

```powershell
vercel ls                 # deployments and their state
vercel logs <url>         # runtime stdout/stderr
vercel env ls             # names only; values are never shown
vercel inspect <url>      # build output, image, and function details
```

To change a value, remove and re-add it, then redeploy:

```powershell
vercel env rm GROQ_API_KEY production
'<new-key>' | vercel env add GROQ_API_KEY production
vercel --prod
```

`vercel dev` also runs the container locally, but it needs Docker installed.

---

## 4. Verify the deployment

1. **Health endpoint** — should return `ok`:

   ```powershell
   Invoke-WebRequest https://<project>.vercel.app/_stcore/health -UseBasicParsing
   ```

2. **Build log** — look for `5/5` pages loaded, `774` chunks written, `86`
   indexed chunks, `86` embeddings, and an index count of `86`.
3. **Ask tab** — ask *"What is the expense ratio of HDFC ELSS Tax Saver Fund?"*
   Expect `answered`, `1.20%`, one Groww citation, and `Last updated from
   sources: <build date>`.
4. **Guardrail spot checks** — *"Should I buy HDFC ELSS?"* → `refused_advice`;
   *"My PAN is ABCDE1234F"* → `blocked_pii` with no echo of the value.
5. **Pipeline tab** — the Retrieve/Answer sub-tabs show the trace you just
   created (traces are written by the instance that served the request).
6. **Evaluation tab** — the recorded acceptance summary from
   `docs/sample_qa.md` renders; recorded answers and sample values do not.

---

## 5. Limits and caveats

These are the ones that actually affect this app (verified against the Vercel
docs, November 2026):

| Limit | Hobby | Effect here |
|---|---|---|
| Function memory | 2 GB / 1 vCPU (fixed) | Streamlit + CPU torch + MiniLM fits; don't add heavy dependencies |
| Function duration | 300 s max (Pro: 800 s) | The Streamlit WebSocket is pinned to the instance and is closed at this limit |
| Scale-down | 5 min idle (production) | Next visit cold-starts the image, which must reload torch + the embedding model |
| Bundle size | 500 MB uncompressed for Python; 5 GB with Large Functions beta | Applies to standard functions; container images instead follow the registry limits below |
| Container image | 15 GB total, **2 GB per compressed layer**, 2 M filesystem entries | `Dockerfile` installs torch in its own layer so no single layer approaches 2 GB |
| Request/response body | 4.5 MB | Chat messages are far below this |
| VCR storage | $0.10 per GB-month | A few GB of image storage; trivial for a demo |

What the duration and scale-down limits mean in practice:

- **Reconnects.** After 5 minutes on Hobby (13 minutes on Pro) the WebSocket is
  closed. Streamlit reconnects automatically, but the container instance may be
  gone, so `st.session_state` — the visible chat history and `last_scheme`
  follow-up memory — resets. Facts are stateless, so after a reconnect name the
  scheme explicitly instead of relying on "What about its exit load?".
- **Cold starts.** After 5 idle minutes the instance scales to zero. The next
  request pays a cold start (tens of seconds: container boot, Streamlit start,
  torch import, MiniLM load). Keep the tab open during a demo, or have an uptime
  monitor ping `/_stcore/health` every few minutes — each ping is a billed
  invocation.
- **Ephemeral disk.** The container filesystem is writable but per-instance.
  `data/traces/*` written at runtime lives only on the instance that wrote it, so
  the Pipeline tab only shows requests this instance handled. Rebuild-time
  artifacts (raw pages, chunks, embeddings, Chroma) are always present because
  they are baked into the image. No user data is persisted anywhere.
- **Corpus freshness.** Pages are fetched at image build time, so the
  `Last updated from sources` date is the build date. Redeploy to refresh the
  five pages.
- **Region.** New projects default to `iad1` (Washington, D.C.). For users in
  India, set **Project → Settings → Functions → Region** to `bom1` (Mumbai) and
  redeploy to cut latency.
- **Single instance per WebSocket.** Streamlit session state is in-process; with
  multiple concurrent viewers Vercel may run several instances, each with its own
  chat state. That is fine for a demo with no shared login.

---

## 6. The embedding model ships with the image

Vercel's build network can reach Groww but **not** `huggingface.co`. A build that
relies on `SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")`
therefore fails at `run_embed` with:

```text
FAIL: could not load embedding model 'sentence-transformers/all-MiniLM-L6-v2'
from local files: We couldn't connect to 'https://huggingface.co' ...
```

The fix is already in the repo: `models/all-MiniLM-L6-v2/` holds the MiniLM
snapshot, and the image points at it:

```dockerfile
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    EMBEDDING_MODEL_PATH=/app/models/all-MiniLM-L6-v2
...
COPY models ./models
```

`load_encoder()` reads `EMBEDDING_MODEL_PATH` and passes that directory to
`SentenceTransformer(..., local_files_only=True)`. The path is not part of
`config.yaml`, so `config_hash` and existing local indexes stay valid. When the
variable is unset, the loader uses `embedding.model` exactly as before, which is
why local development still resolves the Hub id from `~/.cache/huggingface`.

Vendored files (all from snapshot `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`):

```text
models/all-MiniLM-L6-v2/
  config.json
  config_sentence_transformers.json
  model.safetensors            ~91 MB
  modules.json
  sentence_bert_config.json
  special_tokens_map.json
  tokenizer.json
  tokenizer_config.json
  vocab.txt
  1_Pooling/config.json
```

The weights are bit-identical to the Hub snapshot, so embeddings (and therefore
the Chroma index and retrieval scores) do not change. `.gitattributes` marks
`models/**` as binary so Git never rewrites line endings in these files.

To refresh the snapshot, copy a newer Hub snapshot into the same directory (keep
the file list above) and redeploy. To use a different local directory without
changing the image, set `EMBEDDING_MODEL_PATH` to an absolute path or a path
relative to the project root.

---

## 7. If the build cannot fetch Groww

Step 1 of the `Dockerfile` fetches exactly the five configured URLs. If Vercel's
build network is blocked by Groww (HTTP 403/429 or a Cloudflare challenge), the
build fails at `python -m scripts.run_ingest`.

Check the build log first: the loader reports the status code per URL in
`data/processed/load_report.md`. If it is a network block rather than a code bug,
bake the already-built corpus into the image instead:

```powershell
# 1. Build the artifacts locally (they already exist if you ran the pipeline)
python -m scripts.run_ingest
python -m scripts.run_chunk
python -m scripts.run_embed
python -m scripts.run_index
```

```diff
# 2. .dockerignore — stop excluding the built artifacts
- data/
```

```diff
# 3. Dockerfile — reuse committed artifacts, fetch only when they are missing
-RUN python -m scripts.run_ingest \
-    && python -m scripts.run_chunk \
-    && python -m scripts.run_embed \
-    && python -m scripts.run_index
+RUN test -f data/index_manifest.json \
+    || (python -m scripts.run_ingest \
+        && python -m scripts.run_chunk \
+        && python -m scripts.run_embed \
+        && python -m scripts.run_index)
```

```powershell
# 4. Commit the artifacts (data/ is gitignored, so force-add it)
git add -f data
git commit -m "chore: commit built corpus for Vercel builds"
git push origin main
```

This trades repo size (a few MB of HTML/chunks/Chroma) for a build that needs no
outbound network. `data/raw` can be dropped from the force-add if you only need
the processed artifacts.

If Step 1 instead fails on `robots.txt`, do **not** work around it — the corpus is
limited to five pages and the fetch policy is part of the spec.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Build fails at `run_embed`, cannot connect to `huggingface.co` | `EMBEDDING_MODEL_PATH` missing, or `models/` not copied into the image | keep the model env vars and `COPY models ./models` from §6 |
| Build fails at `run_ingest`, HTTP 403/429 | Groww blocks the build IP | use the committed-corpus fallback in §7 |
| Build fails: image layer exceeds 2 GB | torch installed together with the rest | keep `pip install torch` in its own `RUN` layer (already done) |
| `_stcore/health` returns 404 / page never loads | app bound to `localhost` or wrong port | keep `--server.address=0.0.0.0 --server.port=${PORT:-80}` in the `CMD`; do not rely on `.streamlit/config.toml` (`server.address = "localhost"`) |
| WebSocket disconnects every 5 minutes | Hobby max duration | expected; facts still work after reconnect, chat history resets |
| First answer takes 30+ seconds | cold start after idle scale-down | expected; warm the app before a demo |
| `answered` → provider 401/403 | key not set for that environment, or wrong provider | `vercel env ls`, re-add the key for `production`, redeploy |
| `IndexDriftError` at runtime | runtime artifacts out of sync with the image | redeploy; do not mount a different `data/` |
| `ModuleNotFoundError: scripts` | command run from the wrong directory | keep `WORKDIR /app`; the image runs modules from `/app` |
| Vercel login wall on the URL | Deployment Protection on previews | use the production URL or disable protection for previews |

---

## 9. Local image check (optional)

Docker is not installed on the development machine used for this project, so the
image has not been built locally. With Docker Desktop available:

```powershell
docker build -t hdfc-mf-faq .
docker run --rm -p 8080:80 --env-file .env hdfc-mf-faq
# open http://localhost:8080 — the same four tabs as the local Streamlit app
```

`--env-file .env` only supplies runtime provider variables; the build itself
needs no key. The container binds `0.0.0.0:80` internally, so the app is
reachable from the host even though `.streamlit/config.toml` pins the local
development run to `localhost`.
