FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# CPU images use PyTorch's cpu-only wheel index; GPU builds pass
# --build-arg TORCH_INDEX=https://pypi.org/simple so PyPI's CUDA-bundled
# torch wheels are installed instead.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

# ffmpeg includes libass/drawtext support; fonts make caption output predictable.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-v10.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --index-url "${TORCH_INDEX}" \
        torch==2.8.0 torchvision==0.23.0 \
    && pip install --no-cache-dir -r requirements-v10.txt
# ultralytics pulls the non-headless opencv wheels; force the headless
# contrib build back on so cv2 works without libGL in the container.
RUN pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python || true
RUN pip install --no-cache-dir --force-reinstall --no-deps opencv-contrib-python-headless==4.11.0.86

COPY main.py renderer.py face_detection.py ./
COPY autoframe_v10 ./autoframe_v10
COPY third_party ./third_party
COPY models ./models

# Fail the build when Git LFS pointer files were checked out instead of the
# real model binaries (they are a few hundred bytes of ASCII text).
RUN python - <<'EOF'
import hashlib, sys
expected = {
    "models/yolov11n-face.pt": "9420a9d4933940d2202f511d45df1750e3c1546dd9efc7a3985caae3bbbf7ed2",
    "third_party/LR-ASD/weight/finetuning_TalkSet.model": "6b4ef53694e874e96cf630198dc479c78aebb3993bbf166aee3d926dfe7d9342",
}
for path, want in expected.items():
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if digest != want:
        sys.exit(f"model checksum mismatch for {path}: {digest} != {want}")
print("model checksums OK")
EOF

# Cloud Run injects PORT. One worker per container prevents a single render from
# oversubscribing CPU; Cloud Run concurrency is also pinned to one in deploy.sh.
CMD ["functions-framework", "--target=vertical_render", "--host=0.0.0.0", "--port=8080"]
