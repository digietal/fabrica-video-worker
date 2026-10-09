import json
import subprocess
import tempfile
from pathlib import Path

from worker.supabase import supabase


def get_asset(asset_id: str):
    response = (
        supabase
        .table("assets")
        .select(
            "id, owner_id, project_id, kind, code, file_name, "
            "storage_path, mime_type, source_hash"
        )
        .eq("id", asset_id)
        .single()
        .execute()
    )

    if not response.data:
        raise RuntimeError(f"Asset não encontrado: {asset_id}")

    return response.data


def probe_video(file_path: str):
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=width,height,codec_name,codec_type,r_frame_rate",
        "-of",
        "json",
        file_path,
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe falhou:\n{result.stderr}"
        )

    data = json.loads(result.stdout)

    streams = data.get("streams", [])
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )

    metadata = {
        "duration": float(
            data.get("format", {}).get("duration", 0)
        ),
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "video_fps": (
            video.get("r_frame_rate")
            if video
            else None
        ),
    }

    return metadata


def download_asset(storage_path: str, destination: Path):
    data = (
        supabase
        .storage
        .from_("fabrica")
        .download(storage_path)
    )

    destination.write_bytes(data)


def process_ingest(job: dict):
    payload = job.get("payload") or {}
    asset_id = payload.get("asset_id")

    if not asset_id:
        raise RuntimeError(
            f"INGEST sem asset_id. Job: {job['id']}"
        )

    asset = get_asset(asset_id)

    with tempfile.TemporaryDirectory(
        prefix=f"fabrica_ingest_{job['id']}_"
    ) as temp_dir:

        input_path = Path(temp_dir) / asset["file_name"]

        print(
            f"📥 Baixando asset {asset['id']} "
            f"de fabrica/{asset['storage_path']}"
        )

        download_asset(
            asset["storage_path"],
            input_path,
        )

        print(f"🔎 Executando ffprobe em {input_path}")

        metadata = probe_video(str(input_path))

    artifact = {
        "owner_id": job["owner_id"],
        "production_run_id": job["production_run_id"],
        "kind": "INGEST",
        "cache_key": job["cache_key"],
        "storage_path": asset["storage_path"],
        "thumbnail_path": None,
        "metadata": metadata,
    }

    response = (
        supabase
        .table("artifacts")
        .insert(artifact)
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Não foi possível criar o artifact de INGEST."
        )

    print(
        f"✅ INGEST concluído: job={job['id']} "
        f"artifact={response.data[0]['id']}"
    )

    return response.data[0]
