"""Handlers das etapas posteriores. Entradas de dependência via worker.contracts (ver docs/LACUNAS.md)."""
import tempfile
from pathlib import Path

from worker import ffmpeg, storage
from worker.contracts import GatewayContractGap, require_input, require_list
from worker.gateway import complete_job

# Parâmetros de TRIM definidos em src/lib/factory/produce.ts (cache_key do TRIM).
TRIM_SILENCE_DB = -35
TRIM_MIN_PAUSE_MS = 250


def _complete(job, kind, path, meta, thumb=None):
    return complete_job(job["id"], {"artifacts": [{"kind": kind, "storage_path": path, "thumbnail_path": thumb,
                                                    "cache_key": job.get("cache_key"), "metadata": meta}]})


def process_mix(job):
    p = job.get("payload") or {}
    ugc = p.get("ugc_hash")
    if not ugc or not p.get("hook_asset_id") or not p.get("body1_asset_id") or not p.get("cta_asset_id"):
        raise RuntimeError(f"MIX {job['id']} com payload incompleto")
    # Ordem do engine: Gancho -> Corpo 1 -> Corpo 2 (se houver) -> CTA.
    keys = ["hook", "body1"] + (["body2"] if p.get("body2_asset_id") else []) + ["cta"]
    paths = [require_input(job, k) for k in keys]
    out_path = f"{storage.run_dir(paths[0])}/mix/{ugc}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_mix_{job['id']}_") as tmp:
        parts = [storage.download(sp, Path(tmp) / f"{i}_{k}.mp4") for i, (k, sp) in enumerate(zip(keys, paths))]
        out = ffmpeg.concat(parts, Path(tmp) / "mix.mp4")
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    return _complete(job, "MIX", out_path, {**meta, "ugc_hash": ugc, "ugc_code": p.get("ugc_code")})


def process_trim(job):
    p = job.get("payload") or {}
    mix = require_input(job, "mix")
    out_path = f"{storage.run_dir(mix)}/trim/{p['ugc_hash']}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_trim_{job['id']}_") as tmp:
        src = storage.download(mix, Path(tmp) / "mix.mp4")
        out = ffmpeg.trim_silence(src, Path(tmp) / "trim.mp4", TRIM_SILENCE_DB, TRIM_MIN_PAUSE_MS)
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    return _complete(job, "TRIM", out_path, {**meta, "ugc_hash": p["ugc_hash"],
                                             "silence_db": TRIM_SILENCE_DB, "min_pause_ms": TRIM_MIN_PAUSE_MS})


class BackgroundRemovalProvider:
    """Ponto de integração isolado. Nenhum provider está definido no produto."""
    def remove(self, src: Path, out: Path) -> Path:
        raise GatewayContractGap("BACKGROUND_REMOVE: nenhum provider de remoção de fundo configurado. "
                                 "Nada foi processado.")


BG_PROVIDER = BackgroundRemovalProvider()


def process_background_remove(job):
    p = job.get("payload") or {}
    trim = require_input(job, "trim")
    out_path = f"{storage.run_dir(trim)}/matte/{p['ugc_hash']}.mov"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_bg_{job['id']}_") as tmp:
        src = storage.download(trim, Path(tmp) / "trim.mp4")
        out = BG_PROVIDER.remove(src, Path(tmp) / "matte.mov")  # falha controlada até haver provider
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/quicktime")
    return _complete(job, "BACKGROUND_REMOVE", out_path, {**meta, "ugc_hash": p["ugc_hash"]})


def _render(job, kind, build):
    if not job.get("production_item_id"):
        raise RuntimeError(f"{kind} {job['id']} sem production_item_id")
    trim = require_input(job, "trim")
    code = (job.get("payload") or {}).get("creative_code") or job["production_item_id"]
    out_path = f"{storage.run_dir(trim)}/renders/{code}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_{kind.lower()}_{job['id']}_") as tmp:
        out = build(Path(tmp), storage.download(trim, Path(tmp) / "trim.mp4"), Path(tmp) / "render.mp4")
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    # /complete liga production_items.artifact_id para jobs RENDER_*.
    return _complete(job, kind, out_path, {**meta, "creative_code": code})


def process_render_clean(job):
    return _render(job, "RENDER_CLEAN", lambda tmp, ugc, out: ffmpeg.render_clean(ugc, out))


def process_render_split(job):
    base = require_input(job, "base")
    return _render(job, "RENDER_SPLIT", lambda tmp, ugc, out: ffmpeg.render_split(
        ugc, storage.download(base, tmp / "base.mp4"), out))


def process_render_react(job):
    base, matte = require_input(job, "base"), require_input(job, "matte")
    return _render(job, "RENDER_REACT", lambda tmp, ugc, out: ffmpeg.render_react(
        storage.download(base, tmp / "base.mp4"), storage.download(matte, tmp / "matte.mov"), ugc, out))


def process_thumbnail(job):
    render = require_input(job, "render")
    # Lacuna: /complete não atualiza thumbnail_path de um artifact existente; registra novo artifact THUMBNAIL.
    out_path = render.rsplit(".", 1)[0] + ".jpg"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_thumb_{job['id']}_") as tmp:
        out = ffmpeg.thumbnail(storage.download(render, Path(tmp) / "r.mp4"), Path(tmp) / "t.jpg")
        storage.upload(out, out_path, "image/jpeg")
    return _complete(job, "THUMBNAIL", out_path, {"render_storage_path": render}, thumb=out_path)


def process_zip(job):
    if not job.get("block_id"):
        raise RuntimeError(f"ZIP {job['id']} sem block_id")
    files = require_list(job, "files")  # [{storage_path, file_name}] em block_items.position
    folder, zip_name = require_input(job, "folder"), require_input(job, "zip_name")
    out_path = f"{storage.run_dir(files[0]['storage_path'])}/zips/{zip_name}"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_zip_{job['id']}_") as tmp:
        local = [(storage.download(f["storage_path"], Path(tmp) / f"{i}.mp4"), f"{folder}/{f.get('final_file_name') or f['file_name']}")
                 for i, f in enumerate(files)]
        out = ffmpeg.make_zip(local, Path(tmp) / zip_name)
        size = out.stat().st_size
        storage.upload(out, out_path, "application/zip")
    # /complete liga blocks.zip_artifact_id para jobs ZIP.
    return _complete(job, "ZIP", out_path, {"files": len(files), "size_bytes": size,
                                            "block_number": (job.get("payload") or {}).get("block_number")})
