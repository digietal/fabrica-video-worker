"""Download/upload exclusivamente via Gateway (/storage-url). Sem acesso direto ao banco/storage.

Transferências com retry (rede/5xx/429). Upload é idempotente (x-upsert: true, mesmo caminho).
Cada tentativa pede uma URL assinada nova (evita URL expirada).
"""
import random
import time
from pathlib import Path

import requests

from worker import cache, metrics
from worker.gateway import GatewayUnavailable, storage_url

UA = {"User-Agent": "fabrica-video-worker/1.3"}
ATTEMPTS = 4
TRANSIENT = {408, 429, 500, 502, 503, 504}


def _sleep(attempt):
    base = min(30.0, 3.0 * 2 ** (attempt - 1))
    time.sleep(base / 2 + random.uniform(0, base / 2))


def _input_ref(storage_path: str):
    """Objeto {storage_path, artifact_id, cache_key, kind} do Gateway para este caminho, se houver."""
    job = metrics.current_job() or {}
    inputs = (job.get("payload") or {}).get("inputs") or {}
    for v in inputs.values():
        for ref in (v if isinstance(v, list) else [v]):
            if isinstance(ref, dict) and ref.get("storage_path") == storage_path:
                return ref
    return None


def download(storage_path: str, destination: Path) -> Path:
    """Download com cache local para artifacts imutáveis (ver worker/cache.py)."""
    ref = _input_ref(storage_path)
    if ref and cache.key_for(ref):
        return cache.fetch(ref, destination, lambda sp, dst, _: _download(sp, dst))
    return _download(storage_path, destination)


def _download(storage_path: str, destination: Path) -> Path:
    t0 = time.monotonic()
    try:
        return _download_raw(storage_path, destination)
    finally:
        metrics.add("download_s", time.monotonic() - t0)


def _download_raw(storage_path: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        url = storage_url("download", storage_path).get("url")
        if not url:
            raise RuntimeError(f"Gateway não retornou URL de download para {storage_path}")
        try:
            with requests.get(url, headers=UA, stream=True, timeout=(10, 120)) as r:  # sem Authorization
                if r.status_code in TRANSIENT:
                    last = f"HTTP {r.status_code}"
                elif not r.ok:
                    raise RuntimeError(f"Download falhou HTTP {r.status_code} para {storage_path}")
                else:
                    expected = r.headers.get("Content-Length")
                    written = 0
                    with open(destination, "wb") as f:
                        for chunk in r.iter_content(1024 * 1024):
                            f.write(chunk)
                            written += len(chunk)
                    if expected and expected.isdigit() and int(expected) != written \
                            and not r.headers.get("Content-Encoding"):
                        last = f"incompleto {written}/{expected} bytes"
                        destination.unlink(missing_ok=True)
                    else:
                        metrics.add("download_bytes", written)
                        return destination
        except (requests.Timeout, requests.ConnectionError) as e:
            last = type(e).__name__
        print(f"📥 download tentativa={attempt}/{ATTEMPTS} falhou ({last}) {storage_path}", flush=True)
        if attempt < ATTEMPTS:
            _sleep(attempt)
    raise GatewayUnavailable(f"Download indisponível após {ATTEMPTS} tentativas ({last}): {storage_path}")


def upload(local_path: Path, storage_path: str, content_type: str) -> str:
    t0 = time.monotonic()
    try:
        out = _upload(local_path, storage_path, content_type)
        metrics.add("upload_bytes", Path(local_path).stat().st_size)
        return out
    finally:
        metrics.add("upload_s", time.monotonic() - t0)


def _upload(local_path: Path, storage_path: str, content_type: str) -> str:
    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        url = storage_url("upload", storage_path).get("url")
        if not url:
            raise RuntimeError(f"Gateway não retornou URL de upload para {storage_path}")
        try:
            with open(local_path, "rb") as f:
                # URL assinada: NÃO enviar Authorization.
                r = requests.put(url, data=f, headers={**UA, "Content-Type": content_type, "x-upsert": "true"},
                                 timeout=(10, 600))
            if r.ok:
                return storage_path
            if r.status_code not in TRANSIENT:
                raise RuntimeError(f"Upload falhou HTTP {r.status_code} para {storage_path}: {r.text[:300]}")
            last = f"HTTP {r.status_code}"
        except (requests.Timeout, requests.ConnectionError) as e:
            last = type(e).__name__
        print(f"📤 upload tentativa={attempt}/{ATTEMPTS} falhou ({last}) {storage_path}", flush=True)
        if attempt < ATTEMPTS:
            _sleep(attempt)
    raise GatewayUnavailable(f"Upload indisponível após {ATTEMPTS} tentativas ({last}): {storage_path}")


def run_dir(storage_path: str) -> str:
    """<owner>/<project>/<run>/... -> <owner>/<project>/<run>"""
    segs = storage_path.split("/")
    if len(segs) < 4:
        raise RuntimeError(f"storage_path fora do padrão <owner>/<project>/<run>/...: {storage_path}")
    return "/".join(segs[:3])
