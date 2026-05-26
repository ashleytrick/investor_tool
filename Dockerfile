FROM python:3.11-slim

# uv for reproducible installs from pyproject + uv.lock.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install deps first so the layer caches when only source changes.
# Both extras are baked in: `api` (FastAPI backend the React frontend
# talks to in prod) and `web` (Streamlit UI for local debugging).
COPY pyproject.toml uv.lock* ./
RUN uv sync --extra api --extra web --frozen || uv sync --extra api --extra web

# Copy the rest of the repo.
COPY . .

ENV PYTHONUNBUFFERED=1 \
    # FastAPI defaults; Streamlit users override the port locally.
    API_HOST=0.0.0.0 \
    API_PORT=8080

EXPOSE 8080

# Default CMD = FastAPI backend (production shape). To run the
# Streamlit operator UI in this image instead, override CMD:
#   docker run ... <image> uv run --extra web streamlit run web/app.py \
#       --server.port=8080 --server.address=0.0.0.0 --server.headless=true
CMD ["uv", "run", "--extra", "api", "uvicorn", "web.api:app", \
     "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
