"""Handlers das etapas posteriores. Entradas de dependência via worker.contracts (ver docs/LACUNAS.md)."""
import tempfile
from pathlib import Path

from worker import baseprep, fastpath, ffmpeg, matting, storage
from worker.contracts import require_input, require_list, require_ref
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
        out, fp = ffmpeg.concat_fast(parts, Path(tmp) / "mix.mp4")
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    return _complete(job, "MIX", out_path, {**meta, "pipeline": fp, "ugc_hash": ugc, "ugc_code": p.get("ugc_code")})


def process_trim(job):
    p = job.get("payload") or {}
    mix = require_input(job, "mix")
    out_path = f"{storage.run_dir(mix)}/trim/{p['ugc_hash']}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_trim_{job['id']}_") as tmp:
        src = storage.download(mix, Path(tmp) / "mix.mp4")
        out, fp = ffmpeg.trim_fast(src, Path(tmp) / "trim.mp4", TRIM_SILENCE_DB, TRIM_MIN_PAUSE_MS)
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    return _complete(job, "TRIM", out_path, {**meta, "pipeline": fp, "ugc_hash": p["ugc_hash"],
                                             "silence_db": TRIM_SILENCE_DB, "min_pause_ms": TRIM_MIN_PAUSE_MS})


def process_background_remove(job):
    """Matte real (RVM). Sucesso só com arquivo real no Storage; qualquer falha -> /fail, sem artifact."""
    p = job.get("payload") or {}
    trim = require_input(job, "trim")
    out_path = f"{storage.run_dir(trim)}/matte/{p['ugc_hash']}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_bg_{job['id']}_") as tmp:
        src = storage.download(trim, Path(tmp) / "trim.mp4")
        out, info = matting.remove_background(src, Path(tmp) / "matte.mp4")
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    return _complete(job, "BACKGROUND_REMOVE", out_path, {**meta, **info, "ugc_hash": p["ugc_hash"]})


def process_asset_matte(job):
    """ASSET_MATTE: RVM real no asset normalizado (Gancho/Corpo/CTA; Bases nunca). 1 job por asset."""
    p = job.get("payload") or {}
    asset_id = p.get("asset_id")
    if not asset_id:
        raise RuntimeError(f"ASSET_MATTE {job['id']} sem asset_id")
    norm = require_input(job, "normalized")
    dilate = max(0, int(p.get("matte_expand") or 0))  # v18: expansão da máscara (mãos/produtos)
    out_path = f"{storage.run_dir(norm)}/matte/assets/{asset_id}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_amatte_{job['id']}_") as tmp:
        src = storage.download(norm, Path(tmp) / "norm.mp4")
        out, info = matting.remove_background(src, Path(tmp) / "matte.mp4", label="ASSET_MATTE", dilate_px=dilate)
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    return _complete(job, "ASSET_MATTE", out_path, {**meta, **info, "asset_id": asset_id, "asset_kind": p.get("kind")})


def process_ugc_matte(job):
    """UGC_MATTE: concat dos ASSET_MATTE (ordem do MIX) + mesmos cortes do TRIM (recalculados do MIX). Sem IA."""
    p = job.get("payload") or {}
    ugc = p.get("ugc_hash")
    if not ugc:
        raise RuntimeError(f"UGC_MATTE {job['id']} sem ugc_hash")
    keys = ["hook", "body1"] + (["body2"] if p.get("body2_asset_id") else []) + ["cta"]
    paths = [require_input(job, k) for k in keys]
    mix = require_input(job, "mix")
    out_path = f"{storage.run_dir(mix)}/matte/ugc/{ugc}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_umatte_{job['id']}_") as tmp:
        mix_local = storage.download(mix, Path(tmp) / "mix.mp4")
        keep = ffmpeg.silence_keep(mix_local, TRIM_SILENCE_DB, TRIM_MIN_PAUSE_MS)
        parts = [storage.download(sp, Path(tmp) / f"{i}_{k}.mp4") for i, (k, sp) in enumerate(zip(keys, paths))]
        out, fp = ffmpeg.ugc_matte_fast(parts, keep, mix_local, Path(tmp) / "ugc_matte.mp4")
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    return _complete(job, "UGC_MATTE", out_path, {**meta, "pipeline": fp, "ugc_hash": ugc, "segments": len(keep),
                                                 "source_mattes": paths})


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
    """v15 (D): com FAST_PIPELINE=1, se o artifact do TRIM já está no formato final, o LIMPO REUTILIZA
    o próprio artifact do TRIM (result.artifact_id) — sem download, sem remux, sem upload, sem novo
    arquivo no Storage. O job RENDER_CLEAN continua existindo e conclui normalmente; /complete liga
    production_items.artifact_id ao artifact do TRIM (mesmo owner + mesma produção, validado no Gateway).
    Qualquer dúvida (sem artifact_id, formato diferente, falha de leitura) -> caminho legado."""
    if fastpath.enabled():
        try:
            ref = require_ref(job, "trim")
            if ref and ref.get("artifact_id"):
                with tempfile.TemporaryDirectory(prefix=f"fabrica_clean_{job['id']}_") as tmp:
                    local = storage.download(ref["storage_path"], Path(tmp) / "trim.mp4")
                    meta = ffmpeg.probe(local)
                if ffmpeg.matches_final(meta):
                    fastpath.record("RENDER_CLEAN", "reuse_trim", None, 0)
                    return complete_job(job["id"], {"artifact_id": ref["artifact_id"],
                                                    "metadata": {**meta, "pipeline": {"fast_pipeline": True,
                                                                                      "method": "reuse_trim",
                                                                                      "reencodes": 0}}})
                fastpath.record("RENDER_CLEAN", "legacy", "TRIM fora do formato final", 1)
        except Exception as e:
            fastpath.record("RENDER_CLEAN", "legacy", f"{type(e).__name__}: {str(e)[:200]}", 1)
    return _render(job, "RENDER_CLEAN", lambda tmp, ugc, out: ffmpeg.render_clean(ugc, out))


def process_render_split(job):
    base = require_input(job, "base")
    ref = require_ref(job, "base")
    style = ((job.get("payload") or {}).get("split_style") or "EXPAND").upper()  # v18

    def build(tmp, ugc, out):
        prepared = baseprep.prepared_base(ref, "SPLIT", storage.download) if ref else None
        # Com Base preparada, o arquivo bruto não é mais necessário neste render (1 download a menos).
        raw = None if prepared else storage.download(base, tmp / "base.mp4")
        return ffmpeg.render_split(ugc, raw or prepared, out, prepared=prepared, style=style)

    return _render(job, "RENDER_SPLIT", build)


def process_render_react(job):
    base, matte = require_input(job, "base"), require_input(job, "matte")
    ref = require_ref(job, "base")
    p = job.get("payload") or {}
    # v18: margem inferior configurável; sem o campo (produções antigas) mantém 24px.
    margin = int(p["react_bottom_margin"]) if p.get("react_bottom_margin") is not None else 24

    def build(tmp, ugc, out):
        prepared = baseprep.prepared_base(ref, "REACT", storage.download) if ref else None
        raw = None if prepared else storage.download(base, tmp / "base.mp4")
        return ffmpeg.render_react(raw or prepared, storage.download(matte, tmp / "matte.mp4"), ugc, out,
                                   prepared=prepared, bottom_margin=margin)

    return _render(job, "RENDER_REACT", build)


PREVIEW_SECONDS = 4.0  # v18: trecho curto, MESMO pipeline do render final


def process_preview(job):
    """PREVIEW (v18): trecho de ~4s de UM UGC em um formato (SPLIT/REACT), para aprovação visual
    antes da produção completa. Não tem production_item_id nem toca em production_items;
    o artifact kind='PREVIEW' é lido pela tela da produção."""
    p = job.get("payload") or {}
    fmt = (p.get("format") or "").upper()
    if fmt not in ("SPLIT", "REACT"):
        raise RuntimeError(f"PREVIEW {job['id']} com formato inválido: {fmt!r}")
    trim = require_input(job, "trim")
    base = require_input(job, "base")
    code = p.get("creative_code") or f"PREVIEW-{fmt}"
    out_path = f"{storage.run_dir(trim)}/previews/{code}_{job['id'][:8]}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_preview_{job['id']}_") as tmp:
        ugc = storage.download(trim, Path(tmp) / "trim.mp4")
        raw_base = storage.download(base, Path(tmp) / "base.mp4")
        out = Path(tmp) / "preview.mp4"
        if fmt == "SPLIT":
            ffmpeg.render_split(ugc, raw_base, out, style=(p.get("split_style") or "EXPAND").upper(),
                                max_dur=PREVIEW_SECONDS)
        else:
            matte = require_input(job, "matte")
            margin = int(p["react_bottom_margin"]) if p.get("react_bottom_margin") is not None else 24
            ffmpeg.render_react(raw_base, storage.download(matte, Path(tmp) / "matte.mp4"), ugc, out,
                                bottom_margin=margin, max_dur=PREVIEW_SECONDS)
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    return _complete(job, "PREVIEW", out_path, {**meta, "creative_code": code, "format": fmt,
                                                "preview": True, "seconds": PREVIEW_SECONDS,
                                                "split_style": p.get("split_style"),
                                                "react_bottom_margin": p.get("react_bottom_margin")})


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
