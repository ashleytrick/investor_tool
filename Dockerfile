FROM python:3.11-slim

# uv for reproducible installs from pyproject + uv.lock.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Install deps first so layer caches when only source changes.
COPY pyproject.toml uv.lock* ./
RUN uv sync --extra web --frozen || uv sync --extra web

# Copy the rest of the repo.
COPY . .

# Streamlit listens on 8080 (Fly maps :80 / :443 to internal :8080).
ENV STREAMLIT_SERVER_PORT=8080 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["uv", "run", "--extra", "web", "streamlit", "run", "web/app.py", \
     "--server.port=8080", "--server.address=0.0.0.0", \
     "--server.headless=true"]
