# Vercel container image for the HDFC MF Facts Assistant.
#
# Vercel builds this image, serves it as a Function on Fluid compute, and routes
# HTTP + WebSocket traffic to it. The five-page corpus is built into the image
# so no artifacts need to be committed and no provider key is needed at build.
#
# See docs/deploy-vercel.md.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TOKENIZERS_PARALLELISM=false \
    HF_HOME=/opt/hf-cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    EMBEDDING_MODEL_PATH=/app/models/all-MiniLM-L6-v2 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# curl is for manual health checks inside the container. No compiler is needed:
# torch, lxml, and selectolax all ship manylinux wheels for cp312.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch keeps the image far smaller than the default CUDA wheels.
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.3"

COPY pyproject.toml README.md ./
COPY config ./config
COPY src ./src
COPY scripts ./scripts
COPY docs ./docs
COPY models ./models
COPY .streamlit ./.streamlit

# Editable install keeps the package at /app/src/mf_faq, so the app resolves
# data/, docs/, and config/ under /app (PROJECT_ROOT = parents[2]).
RUN pip install -e .

# Build the corpus from exactly the five configured pages. This needs network
# access during the Vercel build but no LLM provider key. The embedding model is
# bundled under models/ (see EMBEDDING_MODEL_PATH above) because the build
# network cannot reach huggingface.co.
RUN python -m scripts.run_ingest \
    && python -m scripts.run_chunk \
    && python -m scripts.run_embed \
    && python -m scripts.run_index

# Vercel routes container traffic to $PORT (80 by default for containers).
ENV PORT=80
EXPOSE 80

CMD ["sh", "-c", "streamlit run src/mf_faq/ui/app.py --server.port=${PORT:-80} --server.address=0.0.0.0"]
