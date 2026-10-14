"""Helpers FFmpeg centralizados. Todos capturam stdout/stderr e falham com erro útil."""
import json
import subprocess
import zipfile
from pathlib import Path

from worker.config import FFMPEG_THREADS

W, H, FPS = 1080, 1920, 30
T = str(FFMPEG_THREADS)
FT = ["-filter_threads", "1"]
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
       "-threads", T, "-x264-params", f"threads={T}:lookahead-threads=1:rc-lookahead=10",
       "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart"]


def _run(cmd: list[str], output: Path | None = None) -> None:
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"{cmd[0]} falhou (code {res.returncode}): {res.stderr[-1500:]}")
    if output is not None and (not output.exists() or output.stat().st_size == 0):
        raise RuntimeError(f"{cmd[0]} terminou sem gerar {output.name}")


def probe(path: Path) -> dict:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:stream=width,height,codec_name,codec_type,r_frame_rate,sample_rate,channels",
                          "-of", "json", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe falhou: {res.stderr[-1000:]}")
    d = json.loads(res.stdout)
    st = d.get("streams", [])
    v = next((s for s in st if s.get("codec_type") == "video"), {}) or {}
    a = next((s for s in st if s.get("codec_type") == "audio"), {}) or {}
    fps = None
    if v.get("r_frame_rate"):
        n, _, den = v["r_frame_rate"].partition("/")
        fps = round(float(n) / float(den or 1), 3) if float(den or 1) else None
    return {"duration": float(d.get("format", {}).get("duration", 0) or 0), "width": v.get("width"),
            "height": v.get("height"), "fps": fps, "video_fps": v.get("r_frame_rate"),
            "video_codec": v.get("codec_name"), "audio_codec": a.get("codec_name"),
            "audio_sample_rate": a.get("sample_rate"), "audio_channels": a.get("channels")}


def has_audio(path: Path) -> bool:
    return probe(path).get("audio_codec") is not None


def _fit(label_in: str, label_out: str, w: int = W, h: int = H) -> str:
    # Preenche o quadro sem deformar (scale cover + crop central).
    return (f"[{label_in}]fps={FPS},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"setsar=1[{label_out}]")


def normalize(src: Path, out: Path) -> Path:
    """1080x1920, 30fps, H.264, AAC 48kHz estéreo, MP4. Gera silêncio se não houver áudio."""
    if has_audio(src):
        cmd = ["ffmpeg", "-y", "-i", str(src), *FT, "-filter_complex", _fit("0:v", "v"), "-map", "[v]", "-map", "0:a:0"]
    else:
        cmd = ["ffmpeg", "-y", "-i", str(src), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
               *FT, "-filter_complex", _fit("0:v", "v"), "-map", "[v]", "-map", "1:a", "-shortest"]
    _run(cmd + ENC + [str(out)], out)
    return out


def concat(parts: list[Path], out: Path) -> Path:
    """Concatena na ordem recebida (entradas já normalizadas)."""
    inputs = sum((["-i", str(p)] for p in parts), [])
    chain = "".join(f"[{i}:v][{i}:a]" for i in range(len(parts)))
    fc = f"{chain}concat=n={len(parts)}:v=1:a=1[v][a]"
    _run(["ffmpeg", "-y", *inputs, *FT, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"] + ENC + [str(out)], out)
    return out


def silence_keep(src: Path, silence_db: float, min_pause_ms: int) -> list[tuple[float, float]]:
    """Intervalos mantidos pelo TRIM (silencedetect no áudio). Usado pelo TRIM e pelo UGC_MATTE (mesmos cortes)."""
    sec = min_pause_ms / 1000
    res = subprocess.run(["ffmpeg", "-i", str(src), "-af", f"silencedetect=noise={silence_db}dB:d={sec}", "-f", "null", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"silencedetect falhou: {res.stderr[-1000:]}")
    dur = probe(src)["duration"]
    starts, ends = [], []
    for line in res.stderr.splitlines():
        if "silence_start:" in line:
            starts.append(float(line.split("silence_start:")[1].split()[0]))
        elif "silence_end:" in line:
            ends.append(float(line.split("silence_end:")[1].split()[0]))
    keep, cur = [], 0.0
    for s, e in zip(starts, ends + [dur] * (len(starts) - len(ends))):
        if s > cur:
            keep.append((cur, max(cur, s)))
        cur = e
    if cur < dur:
        keep.append((cur, dur))
    return [(a, b) for a, b in keep if b - a > 0.05] or [(0.0, dur)]


def trim_silence(src: Path, out: Path, silence_db: float, min_pause_ms: int) -> Path:
    """Remove pausas > min_pause_ms abaixo de silence_db (valores vindos de produce.ts)."""
    keep = silence_keep(src, silence_db, min_pause_ms)
    segs = "".join(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}];"
                   f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}];" for i, (a, b) in enumerate(keep))
    fc = segs + "".join(f"[v{i}][a{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=1:a=1[v][a]"
    _run(["ffmpeg", "-y", "-i", str(src), *FT, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"] + ENC + [str(out)], out)
    return out


def ugc_matte(mattes: list[Path], keep: list[tuple[float, float]], out: Path) -> Path:
    """UGC_MATTE: concatena as máscaras dos assets (ordem do MIX) e aplica os MESMOS cortes do TRIM.
    Só vídeo, cinza, 30 fps; sem IA. Streaming via filtergraph."""
    D = ["-threads", T]
    inputs = [x for m in mattes for x in (*D, "-i", str(m))]
    cat = "".join(f"[{i}:v]fps={FPS},format=gray,setpts=PTS-STARTPTS[c{i}];" for i in range(len(mattes)))
    cat += "".join(f"[c{i}]" for i in range(len(mattes))) + f"concat=n={len(mattes)}:v=1:a=0,split={len(keep)}" \
        + "".join(f"[s{i}]" for i in range(len(keep))) + ";"
    segs = "".join(f"[s{i}]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}];" for i, (a, b) in enumerate(keep))
    fc = cat + segs + "".join(f"[v{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=1:a=0[v]"
    _run(["ffmpeg", "-y", *inputs, *FT, "-filter_complex", fc, "-map", "[v]", "-an",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS),
          "-threads", T, "-x264-params", f"threads={T}:lookahead-threads=1:rc-lookahead=10",
          "-movflags", "+faststart", str(out)], out)
    return out


def _matches_final(meta: dict) -> bool:
    """True quando o arquivo já está exatamente no formato final (saída do TRIM): 1080x1920, 30fps, H.264, AAC 48k estéreo."""
    return (meta.get("width") == W and meta.get("height") == H and meta.get("video_codec") == "h264"
            and meta.get("fps") is not None and abs(meta["fps"] - FPS) < 0.01 and meta.get("audio_codec") == "aac"
            and str(meta.get("audio_sample_rate")) == "48000" and meta.get("audio_channels") == 2)


def render_clean(ugc: Path, out: Path) -> Path:
    """LIMPO = UGC após TRIM. v11: se o TRIM já está no formato final, copia os streams (sem recodificar)."""
    if _matches_final(probe(ugc)):
        _run(["ffmpeg", "-y", "-i", str(ugc), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
              "-movflags", "+faststart", str(out)], out)
        return out
    _run(["ffmpeg", "-y", "-i", str(ugc), *FT, "-filter_complex", _fit("0:v", "v"), "-map", "[v]", "-map", "0:a:0"] + ENC + [str(out)], out)
    return out


def _enc_audio_copy(ugc: Path) -> list[str]:
    """ENC com áudio copiado quando o áudio do UGC já é AAC 48k estéreo (sempre, após TRIM)."""
    m = probe(ugc)
    if m.get("audio_codec") == "aac" and str(m.get("audio_sample_rate")) == "48000" and m.get("audio_channels") == 2:
        i = ENC.index("-c:a")
        return ENC[:i] + ["-c:a", "copy"] + [x for x in ENC[i + 2:] if x not in ("-b:a", "160k", "-ar", "48000", "-ac", "2")]
    return ENC


def render_split(ugc: Path, base: Path, out: Path) -> Path:
    """SPLIT_50_50 (v12): Base em cima (1080x960, cover+crop), Avatar/UGC embaixo (1080x960).
    O avatar NÃO é cortado: escala proporcional pela altura (contain) centralizada, com preenchimento
    lateral feito pelo próprio UGC desfocado (sem deformar, sem cortar olhos/testa/queixo).
    Áudio só do UGC, duração do UGC."""
    hh = H // 2
    fc = (f"[1:v]scale={W}:{hh}:force_original_aspect_ratio=increase,crop={W}:{hh},setsar=1,fps={FPS}[top];"
          f"[0:v]fps={FPS},setsar=1,split=2[uf][ub];"
          f"[ub]scale={W // 4}:{hh // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{hh // 4},"
          f"boxblur=8:2,scale={W}:{hh},setsar=1[ubg];"
          f"[uf]scale={W}:{hh}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1[ufg];"
          f"[ubg][ufg]overlay=(W-w)/2:(H-h)/2:shortest=1,format=yuv420p[bot];"
          f"[top][bot]vstack=inputs=2:shortest=1[v]")
    _run(["ffmpeg", "-y", "-i", str(ugc), "-stream_loop", "-1", "-i", str(base), *FT, "-filter_complex", fc,
          "-map", "[v]", "-map", "0:a:0", "-shortest"] + _enc_audio_copy(ugc) + [str(out)], out)
    return out


def render_react(base: Path, mask: Path, ugc: Path, out: Path) -> Path:
    """REACT_DEFAULT: Base ocupa o canvas 1080x1920; Creator (UGC TRIM) recortado pelo matte real do
    BACKGROUND_REMOVE no canto inferior direito, 60% da largura; áudio só do UGC.
    Composição via maskedmerge em yuv420p (sem RGBA/alphamerge/overlay): Creator e máscara recebem o
    MESMO scale/crop/pad para 1080x1920; fundo da máscara preto (= Base). A máscara é montada em
    yuv420p com Y em resolução cheia e U/V em meia resolução derivados da mesma máscara."""
    cw, ch = int(W * 0.6) // 2 * 2, int(H * 0.6) // 2 * 2
    # Canto inferior direito com margem segura (valores pares p/ yuv420p).
    m = 24
    x, y = max(0, W - cw - m) // 2 * 2, max(0, H - ch - m) // 2 * 2
    fit = f"fps={FPS},scale={cw}:{ch}:force_original_aspect_ratio=increase,crop={cw}:{ch},setsar=1"
    pad = f"pad={W}:{H}:{x}:{y}:color=black"
    fc = (_fit("0:v", "b0") + ";[b0]format=yuv420p[bg];"
          f"[2:v]{fit},format=yuv420p,{pad}[fg];"
          f"[1:v]{fit},format=gray,{pad},split=2[my][mc];"
          f"[mc]scale={W // 2}:{H // 2}:flags=bilinear,split=2[mu][mv];"
          "[my][mu][mv]mergeplanes=0x001020:yuv420p[m];"
          "[bg][fg][m]maskedmerge[v]")
    # Decoders limitados (sem isso cada entrada abre 1 thread/frame por núcleo do host -> pico de RAM).
    D = ["-threads", T]
    _run(["ffmpeg", "-y", *D, "-stream_loop", "-1", "-i", str(base), *D, "-i", str(mask), *D, "-i", str(ugc),
          *FT, "-filter_complex", fc, "-map", "[v]", "-map", "2:a:0", "-shortest",
          "-max_muxing_queue_size", "1024"] + _enc_audio_copy(ugc) + [str(out)], out)
    return out


def thumbnail(video: Path, out: Path) -> Path:
    dur = probe(video)["duration"] or 0
    t = f"{max(0.0, min(dur * 0.25, dur - 0.1)):.3f}"
    _run(["ffmpeg", "-y", "-ss", t, "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)], out)
    return out


def make_zip(files: list[tuple[Path, str]], out: Path) -> Path:
    """files = [(arquivo_local, nome_dentro_do_zip)]. MP4 já comprimido: ZIP_STORED."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for local, arc in files:
            z.write(local, arc)
    return out


def run_ffmpeg(input_path: str, output_path: str):
    """Compatibilidade com a versão anterior."""
    render_clean(Path(input_path), Path(output_path))
    return output_path
