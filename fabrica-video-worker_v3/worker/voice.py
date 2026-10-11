"""VOICE_CONVERT: ElevenLabs Speech-to-Speech sobre o asset NORMALIZADO original.

Extrai o áudio, converte (STS, não TTS), remuxa com o vídeo original (stream de vídeo copiado:
mesma imagem, resolução, fps e quantidade de frames — o ASSET_MATTE continua alinhado).
"""
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from worker import ffmpeg, storage
from worker.contracts import require_input
from worker.gateway import complete_job

API = "https://api.elevenlabs.io/v1/speech-to-speech/{voice_id}"
OUTPUT_FORMAT = "mp3_44100_192"   # = VOICE_SETTINGS.output_format (src/lib/factory/voice.ts)
TIMEOUT = int(os.getenv("ELEVENLABS_TIMEOUT", "300"))
MAX_ATTEMPTS = 3
# Tolerância de duração: diferença pequena é corrigida com pad de silêncio / corte suave no fim.
MAX_DRIFT_ABS = float(os.getenv("VOICE_MAX_DRIFT_S", "0.6"))
MAX_DRIFT_REL = 0.05


class VoiceError(RuntimeError):
    pass


def _key() -> str:
    k = os.getenv("ELEVENLABS_API_KEY")
    if not k:
        raise VoiceError("ELEVENLABS_API_KEY não configurada no Worker")
    return k


def _explain(r: requests.Response) -> str:
    try:
        d = r.json().get("detail")
        status = (d or {}).get("status") if isinstance(d, dict) else None
        msg = (d or {}).get("message") if isinstance(d, dict) else str(d)
    except ValueError:
        status, msg = None, (r.text or "")[:300]
    if r.status_code == 401:
        return "ElevenLabs: chave API inválida (401)"
    if status in ("quota_exceeded", "insufficient_credits") or r.status_code == 402:
        return f"ElevenLabs: créditos/saldo insuficientes ({msg})"
    if r.status_code == 404:
        return "ElevenLabs: voz não encontrada na conta (404)"
    if r.status_code == 429:
        return f"ElevenLabs: limite de uso/rate limit (429) {msg or ''}".strip()
    return f"ElevenLabs HTTP {r.status_code}: {msg}"


def convert(audio: Path, voice_id: str, model_id: str, out: Path) -> Path:
    """Chamada real com retentativa limitada (429/5xx/rede). Nunca loga a chave."""
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with audio.open("rb") as fh:
                r = requests.post(API.format(voice_id=voice_id), params={"output_format": OUTPUT_FORMAT},
                                  headers={"xi-api-key": _key(), "Accept": "audio/mpeg"},
                                  data={"model_id": model_id, "remove_background_noise": "false"},
                                  files={"audio": (audio.name, fh, "audio/wav")}, timeout=TIMEOUT)
        except requests.RequestException as e:
            last = f"ElevenLabs: falha de rede ({type(e).__name__})"
            time.sleep(min(30, 5 * attempt))
            continue
        if r.ok and r.content:
            out.write_bytes(r.content)
            return out
        last = _explain(r)
        if r.status_code == 429 or r.status_code >= 500:
            wait = r.headers.get("retry-after")
            time.sleep(min(60, float(wait) if wait and wait.replace(".", "").isdigit() else 5 * attempt))
            continue
        raise VoiceError(last)  # 4xx definitivo: sem retentativa
    raise VoiceError(f"{last} — após {MAX_ATTEMPTS} tentativas")


def _duration(p: Path) -> float:
    return ffmpeg.probe(p)["duration"]


def _video_duration(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
                        "-of", "csv=p=0", str(p)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return _duration(p)


def remux(video: Path, voice: Path, out: Path) -> tuple[Path, dict]:
    vdur, adur = _video_duration(video), _duration(voice)
    drift = adur - vdur
    if abs(drift) > max(MAX_DRIFT_ABS, vdur * MAX_DRIFT_REL):
        raise VoiceError(f"Áudio convertido fora de sincronia: vídeo {vdur:.2f}s, voz {adur:.2f}s (diferença {drift:+.2f}s)")
    # Sem time-stretch: pad de silêncio se curto; corte com fade de 80ms se longo. Duração final = vídeo.
    fade_st = max(0.0, vdur - 0.08)
    af = f"aresample=48000,apad,atrim=0:{vdur:.3f},afade=t=out:st={fade_st:.3f}:d=0.08"
    ffmpeg._run(["ffmpeg", "-y", "-i", str(video), "-i", str(voice), "-map", "0:v:0", "-map", "1:a:0",
                 "-c:v", "copy", "-af", af, "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                 "-t", f"{vdur:.3f}", "-movflags", "+faststart", str(out)], out)
    return out, {"video_duration": vdur, "voice_duration": adur, "drift_s": round(drift, 3)}


def process_voice_convert(job):
    p = job.get("payload") or {}
    asset_id, voice_id, model_id = p.get("asset_id"), p.get("voice_id"), p.get("model_id")
    if not asset_id or not voice_id or not model_id:
        raise VoiceError(f"VOICE_CONVERT {job['id']} com payload incompleto")
    if p.get("kind") == "base":
        raise VoiceError("Bases não recebem conversão de voz")
    norm = require_input(job, "normalized")
    out_path = f"{storage.run_dir(norm)}/voice/{asset_id}_{voice_id}.mp4"
    with tempfile.TemporaryDirectory(prefix=f"fabrica_voice_{job['id']}_") as tmp:
        t = Path(tmp)
        src = storage.download(norm, t / "norm.mp4")
        wav = t / "orig.wav"
        ffmpeg._run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(wav)], wav)
        conv = convert(wav, voice_id, model_id, t / "voice.mp3")
        out, sync = remux(src, conv, t / "out.mp4")
        meta = ffmpeg.probe(out)
        storage.upload(out, out_path, "video/mp4")
    return complete_job(job["id"], {"artifacts": [{"kind": "VOICE_CONVERT", "storage_path": out_path, "thumbnail_path": None,
                                                    "cache_key": job.get("cache_key"),
                                                    "metadata": {**meta, **sync, "asset_id": asset_id, "voice_id": voice_id,
                                                                 "voice_name": p.get("voice_name"), "model_id": model_id,
                                                                 "config_version": p.get("config_version"),
                                                                 "output_format": OUTPUT_FORMAT}}]})
