"""Download/upload exclusivamente via Gateway (/storage-url). Sem acesso direto ao banco/storage."""
from pathlib import Path

import requests

from worker.gateway import storage_url

UA = {"User-Agent": "fabrica-video-worker/1.0"}


def download(storage_path: str, destination: Path) -> Path:
    signed = storage_url("download", storage_path)
    url = signed.get("url")
    if not url:
        raise RuntimeError(f"Gateway não retornou URL de download para {storage_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, headers=UA, stream=True, timeout=300) as r:  # sem Authorization
        if not r.ok:
            raise RuntimeError(f"Download falhou HTTP {r.status_code} para {storage_path}")
        with open(destination, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                f.write(chunk)
    return destination


def upload(local_path: Path, storage_path: str, content_type: str) -> str:
    signed = storage_url("upload", storage_path)
    url = signed.get("url")
    if not url:
        raise RuntimeError(f"Gateway não retornou URL de upload para {storage_path}")
    with open(local_path, "rb") as f:
        # URL assinada: NÃO enviar Authorization.
        r = requests.put(url, data=f, headers={**UA, "Content-Type": content_type, "x-upsert": "true"}, timeout=900)
    if not r.ok:
        raise RuntimeError(f"Upload falhou HTTP {r.status_code} para {storage_path}: {r.text[:300]}")
    return storage_path


def run_dir(storage_path: str) -> str:
    """<owner>/<project>/<run>/... -> <owner>/<project>/<run>"""
    segs = storage_path.split("/")
    if len(segs) < 4:
        raise RuntimeError(f"storage_path fora do padrão <owner>/<project>/<run>/...: {storage_path}")
    return "/".join(segs[:3])
