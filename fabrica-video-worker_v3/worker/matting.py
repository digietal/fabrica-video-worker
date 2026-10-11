"""BACKGROUND_REMOVE real: Robust Video Matting (ONNX, CPU) quadro a quadro, em streaming.

Entrada: UGC TRIM (mp4). Saída: máscara alfa (H.264, tons de cinza, 30 fps, mesma duração do TRIM).
Memória: só 1 quadro em RAM por vez (pipes FFmpeg) + estado recorrente do RVM; threads = FFMPEG_THREADS.
Falha (RuntimeError -> /fail) se o modelo faltar, o FFmpeg falhar, nenhum quadro for lido
ou nenhuma pessoa for detectada. Nunca gera arquivo falso.
"""
import subprocess
import tempfile
from pathlib import Path

from worker.config import BG_DOWNSAMPLE, BG_MIN_COVERAGE, BG_PROVIDER, BG_WIDTH, FFMPEG_THREADS, RVM_MODEL_PATH

FPS = 30


def remove_background(src: Path, out: Path) -> tuple[Path, dict]:
    if BG_PROVIDER != "rvm":
        raise RuntimeError(f"BACKGROUND_REMOVE: BG_PROVIDER='{BG_PROVIDER}' não suportado (use 'rvm').")
    if not Path(RVM_MODEL_PATH).exists():
        raise RuntimeError(f"BACKGROUND_REMOVE: modelo RVM não encontrado em {RVM_MODEL_PATH}.")
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
        if dec_rc != 0:
            dec_err.seek(0)
            raise RuntimeError(f"BACKGROUND_REMOVE: decodificação falhou: {dec_err.read()[-1000:].decode(errors='ignore')}")
        if enc_rc != 0:
            enc_err.seek(0)
            raise RuntimeError(f"BACKGROUND_REMOVE: gravação do matte falhou: {enc_err.read()[-1000:].decode(errors='ignore')}")
    if frames == 0:
        raise RuntimeError("BACKGROUND_REMOVE: nenhum quadro lido do UGC TRIM.")
    if max_cov < BG_MIN_COVERAGE:
        raise RuntimeError("BACKGROUND_REMOVE: nenhuma pessoa detectada no vídeo do Creator. Nada foi salvo.")
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("BACKGROUND_REMOVE: matte não foi gerado.")
    return out, {"provider": "rvm_mobilenetv3", "matte_width": w, "matte_height": h, "frames": frames,
                 "mean_coverage": round(coverage_sum / frames, 4), "downsample_ratio": BG_DOWNSAMPLE}
