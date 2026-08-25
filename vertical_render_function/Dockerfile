FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# ffmpeg includes libass/drawtext support; fonts make caption output predictable.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py renderer.py ./

# Cloud Run injects PORT. One worker per container prevents a single render from
# oversubscribing CPU; Cloud Run concurrency is also pinned to one in deploy.sh.
CMD ["functions-framework", "--target=vertical_render", "--host=0.0.0.0", "--port=8080"]
