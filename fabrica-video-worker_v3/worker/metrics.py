"""Métricas por job (thread-local) + leitura de CPU/memória do contêiner. Nunca registra tokens/URLs assinadas."""
import json
import os
import threading
import time
import socket
import uuid

# v17: identidade do Worker na frota (vários contêineres independentes).
HOST = socket.gethostname()
WORKER_ID = os.getenv("WORKER_ID") or os.getenv("RAILWAY_REPLICA_ID") or f"{HOST}-{uuid.uuid4().hex[:6]}"

_local = threading.local()


def begin(job: dict):
    _local.m = {"download_s": 0.0, "download_bytes": 0, "upload_s": 0.0, "upload_bytes": 0,
                "cache_hit": 0, "cache_miss": 0}
    _local.job = job


def current_job():
    return getattr(_local, "job", None)


def add(key: str, value):
    m = getattr(_local, "m", None)
    if m is not None:
        m[key] = m.get(key, 0) + value


def snapshot() -> dict:
    m = dict(getattr(_local, "m", None) or {})
    for k in ("download_s", "upload_s"):
        if k in m:
            m[k] = round(m[k], 2)
    return m


def end():
    _local.m = None
    _local.job = None


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def memory_mb() -> dict:
    """Memória do contêiner (cgroup v2/v1); fallback /proc/meminfo. Valores em MB ou None."""
    used = limit = None
    cur = _read("/sys/fs/cgroup/memory.current") or _read("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    mx = _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if cur and cur.isdigit():
        used = int(cur) / 2**20
    if mx and mx.isdigit() and int(mx) < 2**50:
        limit = int(mx) / 2**20
    if limit is None:
        info = _read("/proc/meminfo") or ""
        vals = {l.split(":")[0]: int(l.split()[1]) / 1024 for l in info.splitlines() if l.split()[1:2]}
        if "MemTotal" in vals:
            limit = vals["MemTotal"]
            if used is None and "MemAvailable" in vals:
                used = limit - vals["MemAvailable"]
    # v11: memory.current inclui page cache de arquivos (recuperável pelo kernel). Desconta inactive_file
    # para não pausar claims por falsa falta de memória enquanto o FFmpeg lê/grava vídeos.
    stat = _read("/sys/fs/cgroup/memory.stat") or _read("/sys/fs/cgroup/memory/memory.stat") or ""
    for line in stat.splitlines():
        k, _, v = line.partition(" ")
        if k in ("inactive_file", "total_inactive_file") and v.strip().isdigit() and used is not None:
            used = max(0.0, used - int(v) / 2**20)
            break
    free = (limit - used) if (limit is not None and used is not None) else None
    r = lambda v: None if v is None else round(v)
    return {"mem_used_mb": r(used), "mem_limit_mb": r(limit), "mem_free_mb": r(free)}


def cpu() -> dict:
    try:
        return {"load1": round(os.getloadavg()[0], 2), "cpus": os.cpu_count()}
    except OSError:
        return {}


def log(event: str, **fields):
    rec = {"event": event, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "worker_id": WORKER_ID, **fields}
    print("📊 " + json.dumps(rec, ensure_ascii=False, default=str), flush=True)


def queue_wait_s(job: dict, now: float | None = None):
    """v17: segundos entre a criação do job e o início no Worker (None se created_at ausente/ inválido)."""
    from datetime import datetime
    raw = (job or {}).get("created_at")
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return round(max(0.0, (now or time.time()) - t), 1)
