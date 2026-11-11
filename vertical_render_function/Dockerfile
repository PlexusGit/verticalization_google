FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# CPU images use PyTorch's cpu-only wheel index; GPU builds pass
# --build-arg TORCH_INDEX=https://pypi.org/simple so PyPI's CUDA-bundled
# torch wheels are installed instead.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
# GPU builds also pass INSTALL_YOLO=1: the v10 detector needs ultralytics
# (AGPL-3.0) and the YOLO weights. The CPU image runs v12 (YuNet, MIT) without them.
ARG INSTALL_YOLO=0

# ffmpeg includes libass/drawtext support; fonts make caption output predictable.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-autoframe.txt requirements-yolo.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --index-url "${TORCH_INDEX}" torch==2.8.0 \
    && pip install --no-cache-dir -r requirements-autoframe.txt
RUN if [ "${INSTALL_YOLO}" = "1" ]; then \
        pip install --no-cache-dir --index-url "${TORCH_INDEX}" torchvision==0.23.0 \
        && pip install --no-cache-dir -r requirements-yolo.txt; \
    fi
# ultralytics pulls the non-headless opencv wheels; force the headless
# contrib build back on so cv2 works without libGL in the container.
RUN pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python || true
RUN pip install --no-cache-dir --force-reinstall --no-deps opencv-contrib-python-headless==4.11.0.86

COPY main.py renderer.py face_detection.py ./
COPY autoframe_v10 ./autoframe_v10
COPY third_party ./third_party
COPY models ./models
RUN if [ "${INSTALL_YOLO}" != "1" ]; then rm -f models/yolov11n-face.pt; fi

# Fail the build when Git LFS pointer files were checked out instead of the
# real model binaries (they are a few hundred bytes of ASCII text).
RUN python - <<'EOF'
import hashlib, sys
import os
expected = {
    "models/face_detection_yunet_2023mar.onnx": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    "third_party/LR-ASD/weight/finetuning_TalkSet.model": "6b4ef53694e874e96cf630198dc479c78aebb3993bbf166aee3d926dfe7d9342",
}
if os.environ.get("INSTALL_YOLO") == "1":
    expected["models/yolov11n-face.pt"] = "9420a9d4933940d2202f511d45df1750e3c1546dd9efc7a3985caae3bbbf7ed2"
for path, want in expected.items():
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if digest != want:
        sys.exit(f"model checksum mismatch for {path}: {digest} != {want}")
print("model checksums OK")
EOF

# Cloud Run injects PORT. One worker per container prevents a single render from
# oversubscribing CPU; Cloud Run concurrency is also pinned to one in deploy.sh.
CMD ["functions-framework", "--target=vertical_render", "--host=0.0.0.0", "--port=8080"]
