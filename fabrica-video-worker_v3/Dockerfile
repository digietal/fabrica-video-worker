FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Modelo de recorte (Robust Video Matting, MobileNetV3, ~15 MB), baixado no build.
RUN mkdir -p /app/models && python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp32.onnx', '/app/models/rvm_mobilenetv3_fp32.onnx')"

COPY worker ./worker

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "worker.main"]
