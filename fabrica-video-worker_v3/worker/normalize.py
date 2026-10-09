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
        out = ffmpeg.normalize(src, Path(tmp) / "out.mp4")  # original nunca é alterado
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    return complete_job(job["id"], {"artifacts": [{"kind": "NORMALIZE", "storage_path": out_path, "thumbnail_path": None,
                                                    "cache_key": job.get("cache_key"),
                                                    "metadata": {**meta, "asset_id": asset_id, "source_storage_path": src_path}}]})
