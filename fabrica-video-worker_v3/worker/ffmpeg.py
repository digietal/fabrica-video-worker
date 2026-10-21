"""Helpers FFmpeg centralizados. Todos capturam stdout/stderr e falham com erro útil."""
import json
import subprocess
import zipfile
from pathlib import Path

import os

from worker.config import FFMPEG_THREADS


def render_opt() -> bool:
    """v16: grafos de TELA DIVIDIDA/REACT enxutos (RENDER_OPT=1). Padrão 0 = grafo v15 idêntico."""
    return os.getenv("RENDER_OPT", "0").strip() == "1"

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


def normalize(src: Path, out: Path, edge: tuple[float, float] | None = None) -> Path:
    """1080x1920, 30fps, H.264, AAC 48kHz estéreo, MP4. Gera silêncio se não houver áudio.
    edge=(início, fim) (só FAST_PIPELINE): decupa as bordas na MESMA codificação (sem custo extra).
    Vídeo e áudio recebem exatamente a mesma janela (trim/atrim) -> sincronia preservada."""
    if has_audio(src) and edge:
        a, b = edge
        fc = (f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[t];" + _fit("t", "v") +
              f";[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a]")
        cmd = ["ffmpeg", "-y", "-i", str(src), *FT, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    elif has_audio(src):
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


def matches_final(meta: dict) -> bool:
    """True quando o arquivo já está exatamente no formato final (saída do TRIM): 1080x1920, 30fps, H.264, AAC 48k estéreo."""
    return (meta.get("width") == W and meta.get("height") == H and meta.get("video_codec") == "h264"
            and meta.get("fps") is not None and abs(meta["fps"] - FPS) < 0.01 and meta.get("audio_codec") == "aac"
            and str(meta.get("audio_sample_rate")) == "48000" and meta.get("audio_channels") == 2)


def render_clean(ugc: Path, out: Path) -> Path:
    """LIMPO = UGC após TRIM. v11: se o TRIM já está no formato final, copia os streams (sem recodificar)."""
    if matches_final(probe(ugc)):
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


def render_split(ugc: Path, base: Path, out: Path, prepared: Path | None = None,
                 style: str = "EXPAND", max_dur: float | None = None) -> Path:
    """SPLIT_50_50: Base em cima (1080x960, cover+crop), Avatar/UGC embaixo (1080x960).
    v18 — dois estilos visuais para a metade inferior (sem deformar, áudio só do UGC, duração do UGC):
    - EXPAND (padrão, visual v14): avatar ocupa a largura toda (cover), corte vertical ancorado
      perto do topo (15%) para preservar cabeça/olhos.
    - BLUR (visual v12): avatar em contain (nada é cortado) centralizado, laterais preenchidas
      com o próprio UGC desfocado.
    v15: `prepared` (BASE_PREP) é a Base JÁ em 1080x960/30fps — o grafo pula scale/crop da Base.
    v18: `max_dur` limita a duração da saída (prévias curtas com o MESMO pipeline)."""
    hh = H // 2
    top = f"[1:v]fps={FPS},setsar=1[top]" if prepared else \
        f"[1:v]scale={W}:{hh}:force_original_aspect_ratio=increase,crop={W}:{hh},setsar=1,fps={FPS}[top]"
    opt = render_opt()
    if opt and not prepared:
        # v16: fps ANTES do scale (Base 60 fps não escala quadros que serão descartados; saída igual).
        top = f"[1:v]fps={FPS},scale={W}:{hh}:force_original_aspect_ratio=increase,crop={W}:{hh},setsar=1[top]"
    if style == "BLUR":
        bot = (f"[0:v]fps={FPS},split=2[fa][fb];"
               f"[fa]scale={W}:{hh}:force_original_aspect_ratio=decrease,setsar=1[fg];"
               f"[fb]scale={W}:{hh}:force_original_aspect_ratio=increase,crop={W}:{hh},gblur=sigma=24,setsar=1[bgf];"
               f"[bgf][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[bot]")
    else:  # EXPAND (padrão)
        bot = (f"[0:v]fps={FPS},scale={W}:{hh}:force_original_aspect_ratio=increase,"
               f"crop={W}:{hh}:(iw-{W})/2:(ih-{hh})*0.15,setsar=1,format=yuv420p[bot]")
    fc = top + ";" + bot + f";[top][bot]vstack=inputs=2:shortest=1[v]"
    D = ["-threads", T] if opt else []  # v16: decoders limitados (menos RAM com 2 entradas 1080p)
    limit = ["-t", f"{max_dur:.3f}"] if max_dur else []
    _run(["ffmpeg", "-y", *D, "-i", str(ugc), *D, "-stream_loop", "-1", "-i", str(prepared or base), *FT,
          "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0", "-shortest", *limit] + _enc_audio_copy(ugc) + [str(out)], out)
    return out


def render_react(base: Path, mask: Path, ugc: Path, out: Path, prepared: Path | None = None,
                 bottom_margin: int = 24, max_dur: float | None = None) -> Path:
    """REACT_DEFAULT: Base ocupa o canvas 1080x1920; Creator (UGC TRIM) recortado pelo matte real do
    BACKGROUND_REMOVE no canto inferior direito, 60% da largura; áudio só do UGC.
    Composição via maskedmerge em yuv420p (sem RGBA/alphamerge/overlay): Creator e máscara recebem o
    MESMO scale/crop/pad para 1080x1920; fundo da máscara preto (= Base). A máscara é montada em
    yuv420p com Y em resolução cheia e U/V em meia resolução derivados da mesma máscara.
    v15: `prepared` (BASE_PREP) é a Base JÁ em 1080x1920/30fps — o grafo pula scale/crop da Base.
    v18: `bottom_margin` configurável por produção (padrão 24 = comportamento antigo; 0 = avatar
    encostado na borda inferior). O recorte do Creator é ancorado no TOPO (crop y=0) para nunca
    cortar cabeça/olhos quando a proporção difere. `max_dur` limita a duração (prévias)."""
    cw, ch = int(W * 0.6) // 2 * 2, int(H * 0.6) // 2 * 2
    # Canto inferior direito com margem inferior configurável (valores pares p/ yuv420p).
    m = max(0, int(bottom_margin))
    x, y = max(0, W - cw - m) // 2 * 2, max(0, H - ch - m) // 2 * 2
    fit = f"fps={FPS},scale={cw}:{ch}:force_original_aspect_ratio=increase,crop={cw}:{ch}:(iw-{cw})/2:0,setsar=1"
    pad = f"pad={W}:{H}:{x}:{y}:color=black"
    bg = f"[0:v]fps={FPS},setsar=1,format=yuv420p[bg]" if prepared else _fit("0:v", "b0") + ";[b0]format=yuv420p[bg]"
    if render_opt():
        # v16: maskedmerge só na região do Creator (cw x ch) em vez do quadro 1080x1920 inteiro;
        # fora dela a máscara era 0 (= Base). Sem pad em tela cheia; overlay opaco yuv420p (sem RGBA).
        bg = bg[:-4] + ",split=2[bg][bgc]"
        fc = (bg + ";"
              f"[bgc]crop={cw}:{ch}:{x}:{y}[bgr];"
              f"[2:v]{fit},format=yuv420p[fg];"
              f"[1:v]{fit},format=gray,split=2[my][mc];"
              f"[mc]scale={cw // 2}:{ch // 2}:flags=bilinear,split=2[mu][mv];"
              "[my][mu][mv]mergeplanes=0x001020:yuv420p[m];"
              "[bgr][fg][m]maskedmerge[comp];"
              f"[bg][comp]overlay=x={x}:y={y}:shortest=1:eof_action=endall:format=yuv420[v]")
    else:
        fc = (bg + ";"
              f"[2:v]{fit},format=yuv420p,{pad}[fg];"
              f"[1:v]{fit},format=gray,{pad},split=2[my][mc];"
              f"[mc]scale={W // 2}:{H // 2}:flags=bilinear,split=2[mu][mv];"
              "[my][mu][mv]mergeplanes=0x001020:yuv420p[m];"
              "[bg][fg][m]maskedmerge[v]")
    # Decoders limitados (sem isso cada entrada abre 1 thread/frame por núcleo do host -> pico de RAM).
    D = ["-threads", T]
    limit = ["-t", f"{max_dur:.3f}"] if max_dur else []
    _run(["ffmpeg", "-y", *D, "-stream_loop", "-1", "-i", str(base), *D, "-i", str(mask), *D, "-i", str(ugc),
          *FT, "-filter_complex", fc, "-map", "[v]", "-map", "2:a:0", "-shortest",
          "-max_muxing_queue_size", "1024", *limit] + _enc_audio_copy(ugc) + [str(out)], out)
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


# ---------------- FAST_PIPELINE (Fase 1) ----------------
# Cada função devolve (arquivo, relatório). Com a flag desligada ou qualquer condição insegura, executa
# exatamente o método legado acima.

def _legacy(out: Path, reason: str | None):
    for extra in (out, out.with_suffix(".concat.txt")):
        extra.unlink(missing_ok=True)
    return reason


def normalize_fast(src: Path, out: Path, silence_db: float, min_pause_ms: int):
    from worker import fastpath
    if not fastpath.enabled():
        return normalize(src, out), {"method": "legacy", "reencodes": 1}
    edge, reason = None, None
    try:
        if has_audio(src):
            edge = fastpath.edge_window(silence_keep(src, silence_db, min_pause_ms), probe(src)["duration"], min_pause_ms)
    except Exception as e:  # análise de silêncio falhou -> sem decupagem de borda
        reason = f"edge: {e}"[:300]
    normalize(src, out, edge)
    return out, fastpath.record("NORMALIZE", "edge_trim" if edge else "legacy", reason, 1,
                                edge_window=list(edge) if edge else None)


def concat_fast(parts: list[Path], out: Path):
    from worker import fastpath
    reason = "FAST_PIPELINE=0"
    if fastpath.enabled():
        try:
            rep = fastpath.concat_copy(parts, out, need_audio=True)
            return out, fastpath.record("MIX", "concat_copy", None, 0, parts=len(parts), **rep)
        except Exception as e:
            reason = _legacy(out, f"{type(e).__name__}: {e}"[:300])
    concat(parts, out)
    return out, (fastpath.record("MIX", "legacy", reason, 1, parts=len(parts)) if fastpath.enabled()
                 else {"method": "legacy", "reencodes": 1})


def trim_fast(src: Path, out: Path, silence_db: float, min_pause_ms: int):
    from worker import fastpath
    reason = "FAST_PIPELINE=0"
    if fastpath.enabled():
        try:
            keep = silence_keep(src, silence_db, min_pause_ms)
            if not fastpath.is_full(keep, probe(src)["duration"]):
                raise fastpath.Incompatible(f"TRIM cortaria {len(keep)} trecho(s)")
            rep = fastpath.copy_file(src, out, need_audio=True)
            return out, fastpath.record("TRIM", "copy_no_cuts", None, 0, **rep)
        except Exception as e:
            reason = _legacy(out, f"{type(e).__name__}: {e}"[:300])
    trim_silence(src, out, silence_db, min_pause_ms)
    return out, (fastpath.record("TRIM", "legacy", reason, 1) if fastpath.enabled() else {"method": "legacy", "reencodes": 1})


def ugc_matte_fast(mattes: list[Path], keep: list[tuple[float, float]], mix: Path, out: Path):
    """Concat por cópia das máscaras SÓ quando o TRIM não corta nada (TRIM = MIX) e a contagem de frames da
    máscara bate exatamente com a do vídeo usado pelo REACT."""
    from worker import fastpath
    reason = "FAST_PIPELINE=0"
    if fastpath.enabled():
        try:
            if not fastpath.is_full(keep, probe(mix)["duration"]):
                raise fastpath.Incompatible(f"TRIM corta {len(keep)} trecho(s): máscara precisa dos mesmos cortes")
            rep = fastpath.concat_copy(mattes, out, need_audio=False)
            target = fastpath.frame_count(mix)
            if rep["frames"] != target:
                raise fastpath.Incompatible(f"máscara {rep['frames']} frames ≠ vídeo {target}")
            return out, fastpath.record("UGC_MATTE", "concat_copy", None, 0, video_frames=target, **rep)
        except Exception as e:
            reason = _legacy(out, f"{type(e).__name__}: {e}"[:300])
    ugc_matte(mattes, keep, out)
    return out, (fastpath.record("UGC_MATTE", "legacy", reason, 1) if fastpath.enabled() else {"method": "legacy", "reencodes": 1})
