import tempfile
from pathlib import Path

from worker import ffmpeg, storage
from worker.gateway import complete_job


def process_normalize(job: dict):
    p = job.get("payload") or {}
    asset_id, src_path = p.get("asset_id"), p.get("storage_path")
    if not asset_id or not src_path:
        raise RuntimeError(f"NORMALIZE {job['id']} sem asset_id/storage_path no payload")
    out_path = f"{storage.run_dir(src_path)}/normalized/{asset_id}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_norm_{job['id']}_") as tmp:
        src = storage.download(src_path, Path(tmp) / "src")
        from worker.pipeline import TRIM_MIN_PAUSE_MS, TRIM_SILENCE_DB
        out, fp = ffmpeg.normalize_fast(src, Path(tmp) / "out.mp4", TRIM_SILENCE_DB, TRIM_MIN_PAUSE_MS)  # original nunca é alterado
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    # v15 (H): asset decupado pelo modo rápido NÃO pode ser reutilizado como se fosse legado.
    # O cache_key reportado carrega o sufixo do modo, senão uma produção futura com FAST_PIPELINE=0
    # poderia herdar silenciosamente o asset já sem as bordas (mesmo storage_path + mesma cache_key).
    ck = job.get("cache_key")
    if ck and fp.get("method") == "edge_trim":
        ck = f"{ck}|fast1"
    return complete_job(job["id"], {"artifacts": [{"kind": "NORMALIZE", "storage_path": out_path, "thumbnail_path": None,
                                                    "cache_key": ck,
                                                    "metadata": {**meta, "pipeline": fp, "asset_id": asset_id, "source_storage_path": src_path}}]})
