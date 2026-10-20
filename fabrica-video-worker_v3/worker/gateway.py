"""Cliente HTTP do Gateway: sessão persistente, timeouts separados, retry com backoff+jitter.

Classificação de erros:
- GatewayUnavailable: falha transitória (rede, timeout, 429, 5xx) esgotou as tentativas.
  O job NÃO deve ser marcado FAILED por isso (o processamento pode ter dado certo).
- GatewayError: erro permanente (401/403/400/404/422...). Sem retentativa.
Nunca loga token, headers ou payload.
"""
import os
import random
import threading
import time

import requests

from worker.config import GATEWAY_URL, WORKER_SECRET_TOKEN

CONNECT_TIMEOUT = float(os.getenv("GATEWAY_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(os.getenv("GATEWAY_READ_TIMEOUT", "90"))
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}

# Tentativas por operação. Estado (complete/fail) insiste mais: perder a confirmação custa reprocessar.
ATTEMPTS = {"health": 3, "claim": 4, "storage-url": 6, "complete": 10, "fail": 8}
MAX_BACKOFF = 60.0

# Uma sessão por thread: requests.Session não é garantidamente thread-safe (v10: jobs concorrentes).
_local = threading.local()


def _sess():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "fabrica-video-worker/1.5", "Accept": "application/json"})
        _local.s = s
    return s


class GatewayError(RuntimeError):
    """Erro permanente do Gateway (não repetir)."""

    def __init__(self, msg, status=None, data=None):
        super().__init__(msg)
        self.status, self.data = status, data


class GatewayUnavailable(RuntimeError):
    """Falha transitória após esgotar as tentativas."""


def _log(op, path, attempt, total, dur, status=None, err=None):
    s = f"🌐 gateway op={op} path={path} tentativa={attempt}/{total} dur={dur:.1f}s"
    if status is not None:
        s += f" http={status}"
    if err:
        s += f" erro={err}"
    print(s, flush=True)


def _backoff(attempt, retry_after=None):
    if retry_after:
        try:
            return min(MAX_BACKOFF, float(retry_after))
        except ValueError:
            pass
    base = min(MAX_BACKOFF, 2.0 * (2 ** (attempt - 1)))
    return base / 2 + random.uniform(0, base / 2)  # jitter


def request(method: str, path: str, payload=None, op: str | None = None):
    op = op or path.strip("/")
    total = ATTEMPTS.get(op, 4)
    url = f"{GATEWAY_URL}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {WORKER_SECRET_TOKEN}"}
    last = ""
    for attempt in range(1, total + 1):
        t0 = time.monotonic()
        try:
            r = _sess().request(method, url, json=payload, headers=headers,
                                 timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except (requests.Timeout, requests.ConnectionError) as e:
            last = type(e).__name__
            _log(op, path, attempt, total, time.monotonic() - t0, err=last)
            if attempt < total:
                time.sleep(_backoff(attempt))
            continue
        dur = time.monotonic() - t0
        try:
            data = r.json()
        except ValueError:
            data = {"error": (r.text or "Resposta inválida do Gateway")[:300]}
        if r.ok:
            if attempt > 1 or dur > 10:
                _log(op, path, attempt, total, dur, r.status_code)
            return data
        _log(op, path, attempt, total, dur, r.status_code, str(data.get("error", ""))[:200])
        if r.status_code in TRANSIENT_STATUS:
            last = f"HTTP {r.status_code}"
            if attempt < total:
                time.sleep(_backoff(attempt, r.headers.get("retry-after")))
            continue
        raise GatewayError(f"Gateway {method} {path} HTTP {r.status_code}: {data}", r.status_code, data)
    raise GatewayUnavailable(f"Gateway {method} {path} indisponível após {total} tentativas ({last})")


def health():
    return request("GET", "/health", op="health")


def claim_next_job():
    return request("POST", "/claim", op="claim").get("job")


def complete_job(job_id: str, result: dict | None = None):
    # Idempotente no servidor: job já COMPLETED responde 200.
    return request("POST", "/complete", {"job_id": job_id, "result": result or {}}, op="complete")


def fail_job(job_id: str, error: str):
    # Idempotente no servidor: nunca rebaixa um job COMPLETED.
    return request("POST", "/fail", {"job_id": job_id, "error": error[:2000]}, op="fail")


def storage_url(action: str, storage_path: str):
    return request("POST", "/storage-url", {"action": action, "storage_path": storage_path}, op="storage-url")
