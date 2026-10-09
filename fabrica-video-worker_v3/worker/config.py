import os


GATEWAY_URL = os.getenv(
    "GATEWAY_URL",
    "https://fabricadigietal.lovable.app/api/public/worker",
).rstrip("/")

WORKER_SECRET_TOKEN = os.getenv("WORKER_SECRET_TOKEN")


if not WORKER_SECRET_TOKEN:
    raise RuntimeError("WORKER_SECRET_TOKEN não configurada")
