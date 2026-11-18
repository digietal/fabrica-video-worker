"""FAST_PIPELINE (Fase 1): caminhos sem reencodificação com validação rigorosa e fallback legado.

Regras:
- Nada aqui é usado quando FAST_PIPELINE=0 (padrão).
- Toda saída por stream copy é VALIDADA depois de gerada (frames, durações, timestamps). Se a validação
  falhar, o chamador descarta o arquivo e executa o método legado. Nenhum erro do caminho rápido
  derruba o job sozinho.
"""
import json
import os
import subprocess
from pathlib import Path

from worker import metrics

FPS = 30
FRAME = 1 / FPS
AAC_FRAME = 1024 / 48000


def enabled() -> bool:
    return os.getenv("FAST_PIPELINE", "0").strip() == "1"


def _ms(name: str, default: int) -> float:
    try:
        return max(0, int(os.getenv(name, str(default)))) / 1000
    except ValueError:
        return default / 1000


class Incompatible(Exception):
    """Condição de segurança não atendida -> usar o pipeline legado."""


def _probe_json(args: list[str]) -> dict:
    res = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json"], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise Incompatible(f"ffprobe falhou: {res.stderr[-300:]}")
    return json.loads(res.stdout or "{}")


def stream_info(path: Path) -> dict:
    """Parâmetros relevantes para concat por cópia + contagem real de frames/pacotes."""
    d = _probe_json(["-count_packets", "-show_entries",
                     "stream=index,codec_type,codec_name,profile,level,width,height,pix_fmt,sample_aspect_ratio,"
                     "r_frame_rate,avg_frame_rate,time_base,sample_rate,channels,channel_layout,start_time,duration,"
                     "nb_read_packets,field_order,color_range,color_space,extradata_size:format=duration",
                     str(path)])
    st = d.get("streams", [])
    v = [s for s in st if s.get("codec_type") == "video"]
    a = [s for s in st if s.get("codec_type") == "audio"]
    if len(v) != 1 or len(a) > 1:
        raise Incompatible(f"{path.name}: esperado 1 vídeo e ≤1 áudio (v={len(v)} a={len(a)})")
    return {"v": v[0], "a": a[0] if a else None, "duration": float(d.get("format", {}).get("duration") or 0)}


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


VIDEO_KEYS = ("codec_name", "profile", "width", "height", "pix_fmt", "r_frame_rate", "time_base",
              "field_order", "color_range", "color_space")
AUDIO_KEYS = ("codec_name", "profile", "sample_rate", "channels", "channel_layout", "time_base")


def check_compatible(infos: list[dict], need_audio: bool = True) -> None:
    """Levanta Incompatible se os arquivos não puderem ser concatenados com segurança por -c copy."""
    if not infos:
        raise Incompatible("sem partes")
    ref = infos[0]
    for i, info in enumerate(infos):
        v, a = info["v"], info["a"]
        if _f(v.get("nb_read_packets")) <= 0:
            raise Incompatible(f"parte {i}: vídeo sem frames")
        if v.get("r_frame_rate") != f"{FPS}/1":
            raise Incompatible(f"parte {i}: fps {v.get('r_frame_rate')} ≠ {FPS}/1")
        for k in VIDEO_KEYS:
            if v.get(k) != ref["v"].get(k):
                raise Incompatible(f"parte {i}: vídeo {k} {v.get(k)} ≠ {ref['v'].get(k)}")
        sar = v.get("sample_aspect_ratio") or "1:1"
        if sar not in ("1:1", "0:1", "N/A"):
            raise Incompatible(f"parte {i}: SAR {sar}")
        if abs(_f(v.get("start_time"))) > FRAME / 2:
            raise Incompatible(f"parte {i}: vídeo começa em {v.get('start_time')}")
        if need_audio:
            if a is None or ref["a"] is None:
                raise Incompatible(f"parte {i}: sem áudio")
            for k in AUDIO_KEYS:
                if a.get(k) != ref["a"].get(k):
                    raise Incompatible(f"parte {i}: áudio {k} {a.get(k)} ≠ {ref['a'].get(k)}")
            vd, ad = _f(v.get("duration")), _f(a.get("duration"))
            # Áudio e vídeo de cada parte precisam terminar juntos (≤ 1 frame + 1 frame AAC), senão o concat
            # empurra a parte seguinte e cria buraco/dessincronia.
            if abs(vd - ad) > FRAME + AAC_FRAME:
                raise Incompatible(f"parte {i}: duração vídeo {vd:.3f}s ≠ áudio {ad:.3f}s")
        elif a is not None:
            raise Incompatible(f"parte {i}: máscara com áudio")


def packet_times(path: Path, stream: str) -> list[float]:
    d = _probe_json(["-select_streams", stream, "-show_entries", "packet=pts_time,dts_time", str(path)])
    out = []
    for p in d.get("packets", []):
        t = p.get("dts_time")
        if t in (None, "N/A"):
            t = p.get("pts_time")
        out.append(_f(t, float("nan")))
    return out


def check_timestamps(path: Path, stream: str = "v:0", step: float = FRAME) -> None:
    """DTS estritamente crescentes, sem buracos > 1,5 passo, início em 0."""
    ts = packet_times(path, stream)
    if not ts:
        raise Incompatible(f"{path.name}: sem pacotes em {stream}")
    if any(t != t for t in ts):
        raise Incompatible(f"{path.name}: timestamp ausente")
    for prev, cur in zip(ts, ts[1:]):
        if cur <= prev:
            raise Incompatible(f"{path.name}: timestamp não crescente ({prev:.4f} → {cur:.4f})")
        if abs((cur - prev) - step) > 0.002:  # grade exata de 1/30 s: nenhum buraco nem jitter
            raise Incompatible(f"{path.name}: timestamp descontínuo ({prev:.4f} → {cur:.4f})")
    if abs(min(ts) - ts[0]) > 1e-6 and min(ts) < -step * 3:
        raise Incompatible(f"{path.name}: timestamps negativos")


def _run(cmd: list[str], out: Path) -> None:
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise Incompatible(f"ffmpeg copy falhou (code {res.returncode}): {res.stderr[-400:]}")


def concat_copy(parts: list[Path], out: Path, need_audio: bool = True) -> dict:
    """Concat demuxer + -c copy, com validação antes e depois. Retorna relatório; Incompatible em falha."""
    infos = [stream_info(p) for p in parts]
    check_compatible(infos, need_audio)
    for p in parts:
        check_timestamps(p)
    # Vídeo: concat demuxer por cópia, cada parte com duração = frames/30 exatos (grade contínua; sem
    # isso o priming do AAC empurra a parte seguinte ~21 ms e cria jitter/dessincronia acumulada).
    # Áudio: cada parte ajustada à duração do SEU vídeo (atrim+apad) e concatenada -> sincronia por junção.
    # O áudio é reencodado (custo desprezível frente ao vídeo); o vídeo NÃO é reencodado.
    durs = [int(_f(i["v"]["nb_read_packets"])) / FPS for i in infos]
    lst = out.with_suffix(".concat.txt")
    lst.write_text("".join("file '" + str(p.resolve()).replace("'", "'\\''") + f"'\ninpoint 0\nduration {d:.6f}\n"
                           for p, d in zip(parts, durs)))
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst)]
    if need_audio:
        cmd += [x for p in parts for x in ("-i", str(p))]
        fc = "".join(f"[{k + 1}:a]atrim=0:{d:.6f},asetpts=PTS-STARTPTS,apad=whole_dur={d:.6f}[a{k}];"
                     for k, d in enumerate(durs))
        fc += "".join(f"[a{k}]" for k in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]"
        cmd += ["-filter_complex", fc, "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-map", "0:v:0", "-an", "-c:v", "copy"]
    # -copyts: preserva os timestamps do demuxer (sem ele o muxer desloca o vídeo ~21 ms pelo priming do AAC).
    _run(cmd + ["-copyts", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-movflags", "+faststart", str(out)], out)
    lst.unlink(missing_ok=True)
    expected_frames = int(sum(_f(i["v"]["nb_read_packets"]) for i in infos))
    rep = validate_output(out, expected_frames, need_audio, sum(durs), len(parts))
    rep["audio_reencoded"] = need_audio
    return rep


def validate_output(out: Path, expected_frames: int, need_audio: bool, expected_dur: float, n_parts: int = 1) -> dict:
    info = stream_info(out)
    frames = int(_f(info["v"].get("nb_read_packets")))
    if frames != expected_frames:
        raise Incompatible(f"saída com {frames} frames, esperado {expected_frames}")
    check_timestamps(out)
    vd = _f(info["v"].get("duration")) or frames * FRAME
    if abs(_f(info["v"].get("start_time"))) > 0.002:
        raise Incompatible(f"saída começa em {info['v'].get('start_time')}s (deslocamento de sincronia)")
    if info["v"].get("r_frame_rate") != f"{FPS}/1":
        raise Incompatible(f"saída com fps {info['v'].get('r_frame_rate')}")
    if abs(vd - expected_dur) > FRAME:
        raise Incompatible(f"duração de vídeo {vd:.3f}s ≠ esperado {expected_dur:.3f}s")
    ad = None
    if need_audio:
        if info["a"] is None:
            raise Incompatible("saída sem áudio")
        ad = _f(info["a"].get("duration"))
        if abs(vd - ad) > FRAME + AAC_FRAME:
            raise Incompatible(f"saída: vídeo {vd:.3f}s ≠ áudio {ad:.3f}s (dessincronia)")
    return {"frames": frames, "video_duration": round(vd, 4), "audio_duration": None if ad is None else round(ad, 4)}


def frame_count(path: Path) -> int:
    return int(_f(stream_info(path)["v"].get("nb_read_packets")))


def copy_file(src: Path, out: Path, need_audio: bool = True) -> dict:
    """Remux por cópia (TRIM sem cortes = MIX). Valida frames e sincronia."""
    info = stream_info(src)
    check_compatible([info], need_audio)
    check_timestamps(src)
    maps = ["-map", "0:v:0"] + (["-map", "0:a:0"] if need_audio else ["-an"])
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), *maps, "-c", "copy", "-fflags", "+bitexact",
          "-movflags", "+faststart", str(out)], out)
    return validate_output(out, int(_f(info["v"]["nb_read_packets"])), need_audio, _f(info["v"].get("duration")))


def is_full(keep: list[tuple[float, float]], duration: float, tol: float = FRAME) -> bool:
    """True quando o TRIM legado não cortaria nada (um único intervalo cobrindo o arquivo inteiro)."""
    return len(keep) == 1 and keep[0][0] <= tol and keep[0][1] >= duration - tol


def edge_window(keep: list[tuple[float, float]], duration: float, min_pause_ms: int) -> tuple[float, float] | None:
    """Decupagem SÓ das bordas do asset (silêncio inicial/final), com margem para preservar fonemas.
    A soma das margens fica abaixo da pausa mínima do TRIM: na junção de dois assets a pausa resultante
    não é detectada como silêncio, então o TRIM legado não precisa cortar ali.
    Retorna (início, fim) ou None quando não há borda relevante a remover."""
    if not keep or duration <= 0:
        return None
    lead_m, tail_m = _ms("FAST_EDGE_LEAD_MS", 100), _ms("FAST_EDGE_TAIL_MS", 120)
    cap = (min_pause_ms / 1000) * 0.9
    if lead_m + tail_m > cap:  # nunca deixa a junção virar uma pausa cortável
        scale = cap / (lead_m + tail_m)
        lead_m, tail_m = lead_m * scale, tail_m * scale
    start = max(0.0, keep[0][0] - lead_m)
    end = min(duration, keep[-1][1] + tail_m)
    if end - start < 0.3:  # sobraria quase nada: não arriscar
        return None
    if start < 0.05 and duration - end < 0.05:
        return None
    return round(start, 3), round(end, 3)


def record(job_type: str, method: str, reason: str | None = None, reencodes: int = 0, **extra) -> dict:
    rec = {"fast_pipeline": enabled(), "method": method, "reencodes": reencodes, **({"fallback_reason": reason} if reason else {}), **extra}
    try:
        m = getattr(metrics._local, "m", None)
        if m is not None:
            m["pipeline_method"] = method
            m["reencodes"] = m.get("reencodes", 0) + reencodes
            if reason:
                m["fallback_reason"] = reason[:200]
    except Exception:  # métricas nunca derrubam job
        pass
    metrics.log("fast_pipeline", job_type=job_type, **rec)
    return rec
