"""Cache local de entradas imutáveis (v10).

Só entra no cache um arquivo descrito pelo Gateway como artifact concluído ({storage_path, artifact_id,
cache_key, kind}). A chave é sha256(storage_path|artifact_id|cache_key): artifact_id é único por produção
e o caminho já começa com <owner>/<project>/<run>/, então não há reuso entre produções nem entre donos.

Garantias:
- Escrita atômica: download em <key>.part.<pid>.<thread> e os.replace() para <key>.bin só após
  validar tamanho (Content-Length) e calcular sha256. Um arquivo incompleto nunca é visível como hit.
- Um único download por chave: lock por chave (threading) + flock de arquivo (vários processos).
- Entrega por hardlink para a pasta temporária do job (fallback: cópia). Remover do cache não quebra
  um job em andamento.
- Validação no hit: tamanho em disco == tamanho registrado no .json; senão descarta e baixa de novo.
- Limpeza: TTL (CACHE_TTL_HOURS) + limite de espaço (CACHE_MAX_MB, LRU por atime/mtime); restos .part
  de execuções anteriores são removidos no início.
"""
import fcntl
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

from worker import metrics

CACHE_ENABLED = os.getenv("CACHE_ENABLED", "1") not in ("0", "false", "no")
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/tmp/fabrica-cache"))
CACHE_MAX_MB = float(os.getenv("CACHE_MAX_MB", "3000"))
CACHE_TTL_HOURS = float(os.getenv("CACHE_TTL_HOURS", "12"))
# Tipos de artifact imutáveis que valem a pena guardar (Bases normalizadas, TRIM, mattes, etc.).
# v15 (I/J): renders finais também entram — THUMBNAIL e ZIP consomem o render logo em seguida e
# passam a pegar o arquivo já em disco (hardlink) em vez de baixar de novo (até ~300 downloads a menos).
CACHEABLE_KINDS = set(os.getenv(
    "CACHE_KINDS", "NORMALIZE,TRIM,ASSET_MATTE,UGC_MATTE,BACKGROUND_REMOVE,VOICE_CONVERT,MIX,"
                   "RENDER_CLEAN,RENDER_SPLIT,RENDER_REACT").split(","))

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def key_for(ref: dict) -> str | None:
    if not CACHE_ENABLED or not isinstance(ref, dict):
        return None
    sp, aid = ref.get("storage_path"), ref.get("artifact_id")
    if not sp or not aid or ref.get("kind") not in CACHEABLE_KINDS:
        return None
    return hashlib.sha256(f"{sp}|{aid}|{ref.get('cache_key') or ''}".encode()).hexdigest()


def _lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _paths(key):
    d = CACHE_DIR / key[:2]
    return d / f"{key}.bin", d / f"{key}.json", d / f"{key}.lock"


def _valid(bin_p: Path, meta_p: Path) -> bool:
    try:
        meta = json.loads(meta_p.read_text())
        return bin_p.stat().st_size == meta["size"] > 0
    except (OSError, ValueError, KeyError):
        return False


def _deliver(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        os.link(src, dest)
    except OSError:
        shutil.copyfile(src, dest)
    try:
        os.utime(src)  # marca uso recente (LRU)
    except OSError:
        pass
    return dest


def fetch(ref: dict, dest: Path, downloader) -> Path:
    """downloader(storage_path, part_path) -> (bytes_written, expected_or_None). Retorna dest."""
    key = key_for(ref)
    if not key:
        downloader(ref["storage_path"], dest, None)
        return dest
    bin_p, meta_p, lock_p = _paths(key)
    bin_p.parent.mkdir(parents=True, exist_ok=True)
    with _lock(key), open(lock_p, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            if bin_p.exists() and _valid(bin_p, meta_p):
                metrics.add("cache_hit", 1)
                return _deliver(bin_p, dest)
            for p in (bin_p, meta_p):
                p.unlink(missing_ok=True)
            metrics.add("cache_miss", 1)
            part = bin_p.with_name(f"{key}.part.{os.getpid()}.{threading.get_ident()}")
            try:
                downloader(ref["storage_path"], part, None)
                size = part.stat().st_size
                if size <= 0:
                    raise RuntimeError(f"Arquivo vazio no download: {ref['storage_path']}")
                h = hashlib.sha256()
                with open(part, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                os.replace(part, bin_p)
                tmp_meta = meta_p.with_suffix(".json.tmp")
                tmp_meta.write_text(json.dumps({"size": size, "sha256": h.hexdigest(), "kind": ref.get("kind"),
                                                "artifact_id": ref.get("artifact_id"), "created": time.time()}))
                os.replace(tmp_meta, meta_p)
            finally:
                part.unlink(missing_ok=True)
            out = _deliver(bin_p, dest)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    prune()
    return out


_prune_guard = threading.Lock()


def startup_cleanup():
    """Remove restos .part/.tmp de execuções interrompidas e entradas inválidas."""
    if not CACHE_DIR.exists():
        return
    for p in CACHE_DIR.rglob("*"):
        if p.is_file() and (".part." in p.name or p.name.endswith(".tmp")):
            p.unlink(missing_ok=True)
    for b in CACHE_DIR.rglob("*.bin"):
        if not _valid(b, b.with_suffix(".json")):
            b.unlink(missing_ok=True)
            b.with_suffix(".json").unlink(missing_ok=True)
    prune()


def prune():
    if not CACHE_DIR.exists() or not _prune_guard.acquire(blocking=False):
        return
    try:
        now = time.time()
        entries = []
        for b in CACHE_DIR.rglob("*.bin"):
            try:
                st = b.stat()
            except OSError:
                continue
            last = max(st.st_atime, st.st_mtime)
            if now - last > CACHE_TTL_HOURS * 3600:
                _drop(b)
            else:
                entries.append((last, st.st_size, b))
        total = sum(e[1] for e in entries)
        limit = CACHE_MAX_MB * 2**20
        for _, size, b in sorted(entries):  # mais antigo primeiro
            if total <= limit:
                break
            _drop(b)
            total -= size
    finally:
        _prune_guard.release()


def _drop(b: Path):
    # Hardlinks já entregues a jobs continuam válidos após o unlink.
    b.unlink(missing_ok=True)
    b.with_suffix(".json").unlink(missing_ok=True)
