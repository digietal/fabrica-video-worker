"""Helpers FFmpeg centralizados. Todos capturam stdout/stderr e falham com erro útil."""
import json
import subprocess
import zipfile
from pathlib import Path

from worker.config import FFMPEG_THREADS

W, H, FPS = 1080, 1920, 30

ENC = [
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "20",
    "-pix_fmt", "yuv420p",
    "-r", str(FPS),
    "-c:a", "aac",
    "-b:a", "160k",
    "-ar", "48000",
    "-ac", "2",
    "-movflags", "+faststart",
    "-threads", str(FFMPEG_THREADS),
    "-x264-params",
    f"threads={FFMPEG_THREADS}:lookahead-threads=1:rc-lookahead=10",
]

FT = ["-filter_threads", "1"]


def _run(cmd: list[str], output: Path | None = None) -> None:
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} falhou (code {res.returncode}): {res.stderr[-1500:]}"
        )
    if output is not None and (
        not output.exists() or output.stat().st_size == 0
    ):
        raise RuntimeError(
            f"{cmd[0]} terminou sem gerar {output.name}"
        )


def probe(path: Path) -> dict:
    res = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=width,height,codec_name,codec_type,r_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if res.returncode != 0:
        raise RuntimeError(f"ffprobe falhou: {res.stderr[-1000:]}")

    d = json.loads(res.stdout)
    st = d.get("streams", [])

    v = next(
        (s for s in st if s.get("codec_type") == "video"),
        {},
    ) or {}

    a = next(
        (s for s in st if s.get("codec_type") == "audio"),
        {},
    ) or {}

    fps = None

    if v.get("r_frame_rate"):
        n, _, den = v["r_frame_rate"].partition("/")
        fps = (
            round(float(n) / float(den or 1), 3)
            if float(den or 1)
            else None
        )

    return {
        "duration": float(
            d.get("format", {}).get("duration", 0) or 0
        ),
        "width": v.get("width"),
        "height": v.get("height"),
        "fps": fps,
        "video_fps": v.get("r_frame_rate"),
        "video_codec": v.get("codec_name"),
        "audio_codec": a.get("codec_name"),
        "audio_sample_rate": a.get("sample_rate"),
        "audio_channels": a.get("channels"),
    }


def has_audio(path: Path) -> bool:
    return probe(path).get("audio_codec") is not None


def _fit(
    label_in: str,
    label_out: str,
    w: int = W,
    h: int = H,
) -> str:
    # Preenche o quadro sem deformar (scale cover + crop central).
    return (
        f"[{label_in}]fps={FPS},"
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},"
        f"setsar=1[{label_out}]"
    )


def normalize(src: Path, out: Path) -> Path:
    """1080x1920, 30fps, H.264, AAC 48kHz estéreo, MP4. Gera silêncio se não houver áudio."""

    if has_audio(src):
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            *FT,
            "-filter_complex",
            _fit("0:v", "v"),
            "-map",
            "[v]",
            "-map",
            "0:a:0",
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            *FT,
            "-filter_complex",
            _fit("0:v", "v"),
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-shortest",
        ]

    _run(cmd + ENC + [str(out)], out)
    return out


def concat(parts: list[Path], out: Path) -> Path:
    """Concatena na ordem recebida (entradas já normalizadas)."""

    inputs = sum(
        (["-i", str(p)] for p in parts),
        [],
    )

    chain = "".join(
        f"[{i}:v][{i}:a]"
        for i in range(len(parts))
    )

    fc = (
        f"{chain}"
        f"concat=n={len(parts)}:v=1:a=1[v][a]"
    )

    _run(
        [
            "ffmpeg",
            "-y",
            *inputs,
            *FT,
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "[a]",
        ]
        + ENC
        + [str(out)],
        out,
    )

    return out


def trim_silence(
    src: Path,
    out: Path,
    silence_db: float,
    min_pause_ms: int,
) -> Path:
    """Remove pausas > min_pause_ms abaixo de silence_db."""

    sec = min_pause_ms / 1000

    res = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(src),
            "-af",
            f"silencedetect=noise={silence_db}dB:d={sec}",
            "-f",
            "null",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if res.returncode != 0:
        raise RuntimeError(
            f"silencedetect falhou: {res.stderr[-1000:]}"
        )

    dur = probe(src)["duration"]

    starts, ends = [], []

    for line in res.stderr.splitlines():
        if "silence_start:" in line:
            starts.append(
                float(
                    line.split("silence_start:")[1]
                    .split()[0]
                )
            )
        elif "silence_end:" in line:
            ends.append(
                float(
                    line.split("silence_end:")[1]
                    .split()[0]
                )
            )

    keep, cur = [], 0.0

    for s, e in zip(
        starts,
        ends + [dur] * (len(starts) - len(ends)),
    ):
        if s > cur:
            keep.append((cur, max(cur, s)))
        cur = e

    if cur < dur:
        keep.append((cur, dur))

    keep = [
        (a, b)
        for a, b in keep
        if b - a > 0.05
    ] or [(0.0, dur)]

    segs = "".join(
        f"[0:v]trim={a:.3f}:{b:.3f},"
        f"setpts=PTS-STARTPTS[v{i}];"
        f"[0:a]atrim={a:.3f}:{b:.3f},"
        f"asetpts=PTS-STARTPTS[a{i}];"
        for i, (a, b) in enumerate(keep)
    )

    fc = (
        segs
        + "".join(
            f"[v{i}][a{i}]"
            for i in range(len(keep))
        )
        + f"concat=n={len(keep)}:v=1:a=1[v][a]"
    )

    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            *FT,
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "[a]",
        ]
        + ENC
        + [str(out)],
        out,
    )

    return out


def render_clean(ugc: Path, out: Path) -> Path:
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(ugc),
            *FT,
            "-filter_complex",
            _fit("0:v", "v"),
            "-map",
            "[v]",
            "-map",
            "0:a:0",
        ]
        + ENC
        + [str(out)],
        out,
    )

    return out


def render_split(
    ugc: Path,
    base: Path,
    out: Path,
) -> Path:
    """SPLIT_50_50: UGC em cima, Base embaixo, crop sem deformar, áudio só do UGC, duração do UGC."""

    hh = H // 2

    fc = (
        f"[0:v]scale={W}:{hh}:force_original_aspect_ratio=increase,"
        f"crop={W}:{hh},setsar=1,fps={FPS}[top];"
        f"[1:v]scale={W}:{hh}:force_original_aspect_ratio=increase,"
        f"crop={W}:{hh},setsar=1,fps={FPS}[bot];"
        f"[top][bot]vstack=inputs=2[v]"
    )

    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(ugc),
            "-stream_loop",
            "-1",
            "-i",
            str(base),
            *FT,
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "0:a:0",
            "-shortest",
        ]
        + ENC
        + [str(out)],
        out,
    )

    return out


def render_react(
    base: Path,
    creator_matte: Path,
    ugc_audio: Path,
    out: Path,
) -> Path:
    """REACT_DEFAULT: base no canvas, creator recortado no inferior central, áudio só do UGC."""

    cw = int(W * 0.6)

    fc = (
        _fit("0:v", "bg")
        + f";[1:v]scale={cw}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:H-h:format=auto[v]"
    )

    _run(
        [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(base),
            "-i",
            str(creator_matte),
            "-i",
            str(ugc_audio),
            *FT,
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "2:a:0",
            "-shortest",
        ]
        + ENC
        + [str(out)],
        out,
    )

    return out


def thumbnail(video: Path, out: Path) -> Path:
    dur = probe(video)["duration"] or 0

    t = f"{max(0.0, min(dur * 0.25, dur - 0.1)):.3f}"

    _run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            t,
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(out),
        ],
        out,
    )

    return out


def make_zip(
    files: list[tuple[Path, str]],
    out: Path,
) -> Path:
    """files = [(arquivo_local, nome_dentro_do_zip)]. MP4 já comprimido: ZIP_STORED."""

    with zipfile.ZipFile(
        out,
        "w",
        zipfile.ZIP_STORED,
    ) as z:
        for local, arc in files:
            z.write(local, arc)

    return out


def run_ffmpeg(
    input_path: str,
    output_path: str,
):
    """Compatibilidade com a versão anterior."""

    render_clean(
        Path(input_path),
        Path(output_path),
    )

    return output_path
