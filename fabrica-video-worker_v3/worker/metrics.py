"""Métricas por job (thread-local) + leitura de CPU/memória do contêiner. Nunca registra tokens/URLs assinadas."""
import json
import os
import threading
import time

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
    free = (limit - used) if (limit is not None and used is not None) else None
    r = lambda v: None if v is None else round(v)
    return {"mem_used_mb": r(used), "mem_limit_mb": r(limit), "mem_free_mb": r(free)}


def cpu() -> dict:
    try:
        return {"load1": round(os.getloadavg()[0], 2), "cpus": os.cpu_count()}
    except OSError:
        return {}


def log(event: str, **fields):
    rec = {"event": event, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    print("📊 " + json.dumps(rec, ensure_ascii=False, default=str), flush=True)
