"""BACKGROUND_REMOVE real: Robust Video Matting (ONNX, CPU) quadro a quadro, em streaming.

Entrada: UGC TRIM (mp4). Saída: máscara alfa (H.264, tons de cinza, 30 fps, mesma duração do TRIM).
Memória: só 1 quadro em RAM por vez (pipes FFmpeg) + estado recorrente do RVM; threads = FFMPEG_THREADS.
Falha (RuntimeError -> /fail) se o modelo faltar, o FFmpeg falhar, nenhum quadro for lido
ou nenhuma pessoa for detectada. Nunca gera arquivo falso.
"""
import gc
import subprocess
import tempfile
from pathlib import Path

from worker.config import BG_DOWNSAMPLE, BG_MIN_COVERAGE, BG_PROVIDER, BG_WIDTH, FFMPEG_THREADS, RVM_MODEL_PATH

FPS = 30


def _dilate(alpha, px: int):
    """v18: dilatação morfológica da máscara (vizinhança 8, 1px por iteração, NumPy puro).
    Preserva dedos, mãos, embalagens e produtos que o RVM corta nas bordas. px=0 desliga."""
    import numpy as np
    a = alpha
    for _ in range(max(0, int(px))):
        p = np.pad(a, 1, mode="edge")
        a = np.maximum.reduce([p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:],
                               p[1:-1, :-2], p[1:-1, 1:-1], p[1:-1, 2:],
                               p[2:, :-2], p[2:, 1:-1], p[2:, 2:]])
    return a


def remove_background(src: Path, out: Path, label: str = "BACKGROUND_REMOVE", dilate_px: int = 0) -> tuple[Path, dict]:
    if BG_PROVIDER != "rvm":
        raise RuntimeError(f"{label}: BG_PROVIDER='{BG_PROVIDER}' não suportado (use 'rvm').")
    if not Path(RVM_MODEL_PATH).exists():
        raise RuntimeError(f"{label}: modelo RVM não encontrado em {RVM_MODEL_PATH}.")
    import numpy as np
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = FFMPEG_THREADS
    opts.inter_op_num_threads = 1
    sess = ort.InferenceSession(RVM_MODEL_PATH, opts, providers=["CPUExecutionProvider"])

    w = BG_WIDTH // 2 * 2
    h = (w * 16 // 9) // 2 * 2
    frame_bytes = w * h * 3
    T = str(FFMPEG_THREADS)
    with tempfile.TemporaryFile() as dec_err, tempfile.TemporaryFile() as enc_err:
        dec = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-threads", T, "-i", str(src), "-filter_threads", "1",
             "-vf", f"fps={FPS},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, stderr=dec_err)
        enc = subprocess.Popen(
            ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-r", str(FPS),
             "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
             "-threads", T, "-x264-params", f"threads={T}:lookahead-threads=1:rc-lookahead=10",
             "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE, stderr=enc_err)
        outs = img = pha = buf = None
        rec = [np.zeros((1, 1, 1, 1), np.float32)] * 4
        ds = np.array([BG_DOWNSAMPLE], np.float32)
        frames, coverage_sum, max_cov = 0, 0.0, 0.0
        try:
            while True:
                buf = dec.stdout.read(frame_bytes)
                if not buf or len(buf) < frame_bytes:
                    break
                img = np.frombuffer(buf, np.uint8).reshape(h, w, 3).transpose(2, 0, 1)[None].astype(np.float32) / 255.0
                outs = sess.run(None, {"src": img, "r1i": rec[0], "r2i": rec[1], "r3i": rec[2], "r4i": rec[3],
                                       "downsample_ratio": ds})
                pha = outs[1][0, 0]
                rec = outs[2:6]
                if dilate_px > 0:
                    pha = _dilate(pha, dilate_px)
                cov = float(pha.mean())
                coverage_sum += cov
                max_cov = max(max_cov, cov)
                enc.stdin.write((np.clip(pha, 0, 1) * 255).astype(np.uint8).tobytes())
                frames += 1
        finally:
            if enc.stdin:
                enc.stdin.close()
            dec.stdout.close()
            dec_rc, enc_rc = dec.wait(), enc.wait()
            # Libera sessão ONNX, estado recorrente e buffers NumPy antes do próximo job.
            sess = rec = outs = img = pha = buf = None  # noqa: F841
            del sess, rec, outs, img, pha, buf
            gc.collect()
        if dec_rc != 0:
            dec_err.seek(0)
            raise RuntimeError(f"{label}: decodificação falhou: {dec_err.read()[-1000:].decode(errors='ignore')}")
        if enc_rc != 0:
            enc_err.seek(0)
            raise RuntimeError(f"{label}: gravação do matte falhou: {enc_err.read()[-1000:].decode(errors='ignore')}")
    if frames == 0:
        raise RuntimeError(f"{label}: nenhum quadro lido do UGC TRIM.")
    if max_cov < BG_MIN_COVERAGE:
        raise RuntimeError(f"{label}: nenhuma pessoa detectada no vídeo do Creator. Nada foi salvo.")
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"{label}: matte não foi gerado.")
    return out, {"provider": "rvm_mobilenetv3", "matte_width": w, "matte_height": h, "frames": frames,
                 "mean_coverage": round(coverage_sum / frames, 4), "downsample_ratio": BG_DOWNSAMPLE,
                 "dilate_px": int(dilate_px)}
