FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    COEUS_DATA_DIR=/data COEUS_RECORDINGS_DIR=/recordings TZ=Asia/Kolkata

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY coeus ./coeus

# Face models (~38 MB). If this download fails at build time, Coeus fetches them on first use,
# or you can copy them into /data/models yourself.
RUN mkdir -p /app/models \
 && (curl -fsSL -o /app/models/face_detection_yunet_2023mar.onnx \
      https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
  && curl -fsSL -o /app/models/face_recognition_sface_2021dec.onnx \
      https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx \
  || echo "model download skipped") \
 && printf '#!/bin/sh\nmkdir -p /data/models\ncp -n /app/models/*.onnx /data/models/ 2>/dev/null || true\nexec "$@"\n' > /entrypoint.sh \
 && chmod +x /entrypoint.sh

EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD curl -fsS http://127.0.0.1:8765/healthz || exit 1
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "coeus.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers", "--forwarded-allow-ips", "*"]
