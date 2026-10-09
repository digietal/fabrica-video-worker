import json
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from worker.gateway import complete_job, storage_url


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
        raise RuntimeError(f"ffprobe falhou: {result.stderr}")

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

    return {
        "duration": float(data.get("format", {}).get("duration", 0)),
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "video_fps": video.get("r_frame_rate") if video else None,
    }


def download_signed_url(url: str, destination: Path):
    request = Request(
        url,
        headers={
            "User-Agent": "fabrica-video-worker/1.0"
        },
    )

    with urlopen(request, timeout=60) as response:
        destination.write_bytes(response.read())


def process_ingest(job: dict):
    payload = job.get("payload") or {}

    asset_id = payload.get("asset_id")
    storage_path = payload.get("storage_path")

    if not asset_id:
        raise RuntimeError(
            f"INGEST sem asset_id. Job: {job['id']}"
        )

    if not storage_path:
        raise RuntimeError(
            f"INGEST sem storage_path. Job: {job['id']}"
        )

    signed = storage_url(
        "download",
        storage_path,
    )

    url = signed.get("url")

    if not url:
        raise RuntimeError(
            "Gateway não retornou URL assinada de download."
        )

    file_name = (
        payload.get("file_name")
        or Path(storage_path).name
        or f"{asset_id}.mp4"
    )

    with tempfile.TemporaryDirectory(
        prefix=f"fabrica_ingest_{job['id']}_"
    ) as temp_dir:

        input_path = Path(temp_dir) / file_name

        print(
            f"📥 Baixando asset autorizado: "
            f"{storage_path}"
        )

        download_signed_url(
            url,
            input_path,
        )

        print(
            f"🔎 Executando ffprobe em {input_path}"
        )

        metadata = probe_video(
            str(input_path)
        )

    result = {
        "artifacts": [
            {
                "kind": "INGEST",
                "storage_path": storage_path,
                "thumbnail_path": None,
                "cache_key": job.get("cache_key"),
                "metadata": metadata,
            }
        ],
        "metadata": {
            "asset_id": asset_id,
            "probe": metadata,
        },
    }

    response = complete_job(
        job["id"],
        result,
    )

    print(
        f"✅ INGEST concluído via Gateway: "
        f"{response}"
    )

    return response
