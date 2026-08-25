# ffmpeg is compiled with NVENC support in this image. The same container also
# runs without a GPU; processor.py detects that condition and uses libx264.
FROM jrottenberg/ffmpeg:6.1-nvidia2204

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    FUNCTION_TARGET=process_transcode \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

WORKDIR /app
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt
COPY main.py processor.py ./

ENTRYPOINT []
CMD ["functions-framework", "--target=process_transcode", "--host=0.0.0.0", "--port=8080"]
