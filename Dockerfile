FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
RUN pip install --no-cache-dir uv==0.11.3
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

COPY . .

# data/ is never committed (.gitignore, .dockerignore): the image generates gold and the anomaly answer key
# with the deterministic generator, so a deployed instance matches CI. Compose mounts ./data over it locally.
RUN python -m synth.generate_gold --out data --clean

EXPOSE 8000
# Migrations first (advisory lock: parallel replicas are safe), then the API on $PORT. APP_ROLE=console runs
# the Streamlit console from the same image.
CMD ["sh", "scripts/start.sh"]
