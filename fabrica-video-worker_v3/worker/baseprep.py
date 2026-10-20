"""BASE_PREP (v15, flag BASE_PREP, padrão desligado): prepara cada Base UMA vez por formato e reutiliza
o arquivo preparado em todos os renders daquela produção (em vez de escalar/cortar a Base em cada render).

- SPLIT: tira 1080x960 (cover+crop central), 30 fps, H.264 CRF 18, sem áudio.
- REACT: canvas 1080x1920 (cover+crop), 30 fps, H.264 CRF 18, sem áudio.

Trade-off documentado: a Base passa por DUAS compressões (preparo + render final). CRF 18 no preparo
mantém a perda visual imperceptível na composição final, mas por isso a flag fica desligada por padrão.

Segurança:
- Chave = sha256(storage_path|artifact_id|cache_key|formato): nunca reusa Base de outra produção/dono.
- Escrita atômica (.part -> os.replace) + lock por chave (threading + flock): dois jobs nunca preparam
  a mesma Base ao mesmo tempo e um arquivo incompleto nunca é entregue.
- Falha no preparo -> o chamador usa o caminho legado (filtro por render) no mesmo job.
- Os preparados vivem no CACHE_DIR (mesma limpeza TTL/LRU do cache principal NÃO se aplica aqui; a pasta
  baseprep é limpa junto com o cache no startup e respeita o mesmo disco efêmero do contêiner).
"""
import fcntl
import hashlib
import os
import threading
from pathlib import Path

from worker import cache, metrics
from worker.config import FFMPEG_THREADS
from worker.ffmpeg import FPS, H, W, _run

_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def enabled() -> bool:
    return os.getenv("BASE_PREP", "0").strip() == "1"


def _dir() -> Path:
    d = cache.CACHE_DIR / "baseprep"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(ref: dict, fmt: str) -> str | None:
    sp, aid = ref.get("storage_path"), ref.get("artifact_id")
    if not sp or not aid:
        return None
    raw = f"{sp}|{aid}|{ref.get('cache_key') or ''}|{fmt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _lock(key: str):
    with _GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def prepare_filter(fmt: str) -> str:
    """Filtro de preparo da Base por formato (sem fps: aplicado no render)."""
    if fmt == "SPLIT":
        hh = H // 2
        return f"scale={W}:{hh}:force_original_aspect_ratio=increase,crop={W}:{hh},setsar=1"
    if fmt == "REACT":
        return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
    raise ValueError(f"BASE_PREP não suporta formato {fmt}")


def prepared_base(ref: dict, fmt: str, download) -> Path | None:
    """Retorna o arquivo local da Base preparada para `fmt`, preparando se necessário.
    `download(storage_path, dest) -> Path` é o storage.download do Worker (com cache).
    Retorna None quando a flag está desligada ou a referência não é cacheável."""
    if not enabled():
        return None
    key = _key(ref, fmt)
    if not key:
        return None
    out = _dir() / f"{key}.mp4"
    lock_p = _dir() / f"{key}.lock"
    with _lock(key), open(lock_p, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            if out.exists() and out.stat().st_size > 0:
                metrics.add("cache_hit", 1)
                return out
            out.unlink(missing_ok=True)
            metrics.add("cache_miss", 1)
            part = out.with_name(f"{key}.part.{os.getpid()}.{threading.get_ident()}.mp4")  # extensão p/ o ffmpeg
            try:
                src = download(ref["storage_path"], _dir() / f"{key}.src.mp4")
                fc = f"[0:v]fps={FPS},{prepare_filter(fmt)},format=yuv420p[v]"
                _run(["ffmpeg", "-y", "-i", str(src), "-filter_threads", "1", "-filter_complex", fc,
                      "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                      "-pix_fmt", "yuv420p", "-r", str(FPS), "-threads", str(FFMPEG_THREADS),
                      "-x264-params", f"threads={FFMPEG_THREADS}:lookahead-threads=1:rc-lookahead=10",
                      "-movflags", "+faststart", str(part)], part)
                os.replace(part, out)
            finally:
                part.unlink(missing_ok=True)
                (_dir() / f"{key}.src.mp4").unlink(missing_ok=True)
            return out
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
