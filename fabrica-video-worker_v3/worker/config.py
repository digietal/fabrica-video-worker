import os


GATEWAY_URL = os.getenv(
    "GATEWAY_URL",
    "https://fabricadigietal.lovable.app/api/public/worker",
).rstrip("/")

WORKER_SECRET_TOKEN = os.getenv("WORKER_SECRET_TOKEN")

# Memória (Railway): threads limitadas em FFmpeg/x264 e no modelo de recorte.
FFMPEG_THREADS = int(os.getenv("FFMPEG_THREADS", "2"))

# BACKGROUND_REMOVE (provider local, sem API externa): Robust Video Matting em ONNX (CPU).
BG_PROVIDER = os.getenv("BG_PROVIDER", "rvm").lower()
RVM_MODEL_PATH = os.getenv("RVM_MODEL_PATH", "/app/models/rvm_mobilenetv3_fp32.onnx")
BG_WIDTH = int(os.getenv("BG_WIDTH", "540"))            # largura do matte (altura = 16:9 vertical)
BG_DOWNSAMPLE = float(os.getenv("BG_DOWNSAMPLE", "0.4"))  # downsample_ratio do RVM
BG_MIN_COVERAGE = float(os.getenv("BG_MIN_COVERAGE", "0.01"))  # abaixo disso: nenhuma pessoa detectada -> FAILED

if not WORKER_SECRET_TOKEN:
    raise RuntimeError("WORKER_SECRET_TOKEN não configurada")
