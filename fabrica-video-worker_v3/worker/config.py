import os

GATEWAY_URL = os.getenv(
    "GATEWAY_URL",
    "https://fabricadigietal.lovable.app/api/public/worker",
).rstrip("/")

WORKER_SECRET_TOKEN = os.getenv("WORKER_SECRET_TOKEN")

# Limita threads do FFmpeg/x264 para conter o uso de RAM no Railway.
FFMPEG_THREADS = int(os.getenv("FFMPEG_THREADS", "2"))

if not WORKER_SECRET_TOKEN:
    raise RuntimeError("WORKER_SECRET_TOKEN não configurada")
