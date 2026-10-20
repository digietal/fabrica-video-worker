"""v16: perfil por etapa de TELA DIVIDIDA, REACT e remoção de fundo (RVM real).

Uso: python tools/profile_renders_v16.py --ugc U.mp4 --base B.mp4 --mask M.mp4 [--rvm modelo.onnx]
Mede: decodificação, filtros (saída nula), render completo com RENDER_OPT=0 e 1, pico de RAM do FFmpeg,
SSIM entre os dois modos e quadros/s do RVM. Sem --rvm a remoção de fundo NÃO é medida (avisado na saída)."""
import argparse, os, re, resource, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import ffmpeg as F  # noqa: E402


def timed(fn):
    s = time.time(); fn(); return round(time.time() - s, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ugc", required=True); ap.add_argument("--base", required=True); ap.add_argument("--mask", required=True)
    ap.add_argument("--rvm"); ap.add_argument("--out", default="/tmp/profile_v16")
    a = ap.parse_args(); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    u, b, m = Path(a.ugc), Path(a.base), Path(a.mask); T = F.T
    run = lambda c: subprocess.run(c, check=True, capture_output=True)
    print("decodificação UGC+Base+máscara:", timed(lambda: run(["ffmpeg", "-threads", T, "-i", str(b), "-threads", T, "-i", str(m), "-threads", T, "-i", str(u), "-map", "0:v", "-map", "1:v", "-map", "2:v", "-f", "null", "-"])), "s")
    for flag in ("0", "1"):
        os.environ["RENDER_OPT"] = flag
        print(f"TELA DIVIDIDA RENDER_OPT={flag}:", timed(lambda: F.render_split(u, b, out / f"split_{flag}.mp4")), "s")
        print(f"REACT RENDER_OPT={flag}:", timed(lambda: F.render_react(b, m, u, out / f"react_{flag}.mp4")), "s")
    print("pico RAM FFmpeg (MB):", resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024)
    for n in ("split", "react"):
        r = subprocess.run(["ffmpeg", "-i", str(out / f"{n}_0.mp4"), "-i", str(out / f"{n}_1.mp4"), "-lavfi", "ssim", "-f", "null", "-"], capture_output=True, text=True).stderr
        print(f"SSIM {n} (0 vs 1):", re.findall(r"All:\S+", r))
    if not a.rvm:
        print("REMOÇÃO DE FUNDO: NÃO MEDIDA (passe --rvm com o modelo ONNX e um UGC com pessoa real).")
        return
    os.environ["RVM_MODEL_PATH"] = a.rvm
    import importlib, worker.config, worker.matting as MT
    importlib.reload(worker.config); importlib.reload(MT)
    s = time.time()
    try:
        _, meta = MT.remove_background(u, out / "matte.mp4", "PROFILE")
        dt = time.time() - s
        print(f"RVM: {meta['frames']} quadros em {dt:.1f}s = {meta['frames'] / dt:.1f} quadros/s; cobertura {meta['mean_coverage']}")
    except RuntimeError as e:
        print(f"RVM: {time.time() - s:.1f}s; resultado recusado pelo Worker: {e}")


if __name__ == "__main__":
    main()
