"""Benchmark controlado: pipeline legado (FAST_PIPELINE=0) x otimizado (FAST_PIPELINE=1).

Roda LOCALMENTE, sem banco, sem Gateway, sem Storage e sem tocar em produções existentes.
Amostra padrão: 5 Ganchos x 2 Corpos x 1 CTA = 10 UGCs; 2 Bases distribuídas (1 por UGC); 3 formatos = 30 vídeos.

Uso:
  WORKER_SECRET_TOKEN=x python tools/benchmark_fast_pipeline.py                 # assets sintéticos
  WORKER_SECRET_TOKEN=x python tools/benchmark_fast_pipeline.py --assets DIR     # assets reais:
      DIR/hook_*.mp4, DIR/body1_*.mp4, DIR/cta_*.mp4, DIR/base_*.mp4 e (opcional) DIR/matte_<nome>.mp4
      Sem matte real, o benchmark gera uma máscara sintética (o RVM NÃO é medido aqui).
Saída: JSON + tabela em --out (padrão /tmp/fabrica_bench).
"""
import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("WORKER_SECRET_TOKEN", "benchmark-local")
from worker import baseprep, fastpath, ffmpeg  # noqa: E402
import shutil  # noqa: E402

DB, PAUSE = -35, 250


def sh(*a):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *a], check=True, capture_output=True)


def synth(d: Path, secs: float):
    """Assets com silêncio nas bordas (como gravações reais) e fala contínua no meio."""
    d.mkdir(parents=True, exist_ok=True)
    spec = [("hook", i, c) for i, c in enumerate(["red", "orange", "yellow", "pink", "purple"])] + \
           [("body1", i, c) for i, c in enumerate(["green", "teal"])] + [("cta", 0, "blue")]
    for kind, i, c in spec:
        e = f"if(between(t\\,0.45\\,{secs - 0.5})\\,0.4*sin(2*PI*330*t)\\,0)"
        sh("-f", "lavfi", "-i", f"testsrc2=size=1280x2276:rate=30:d={secs}", "-f", "lavfi",
           "-i", f"aevalsrc={e}|{e}:s=48000:d={secs}", "-vf", f"drawbox=c={c}@0.5:t=fill",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(d / f"{kind}_{i}.mp4"))
    for i, c in enumerate(["gray", "white"]):
        sh("-f", "lavfi", "-i", f"testsrc=size=1080x1920:rate=30:d={secs * 2}", "-vf", f"drawbox=c={c}@0.4:t=fill",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(d / f"base_{i}.mp4"))


def timed(stats, stage, fn):
    t = time.perf_counter()
    try:
        r = fn()
    except Exception as e:  # registra erro de FFmpeg sem esconder
        stats["errors"].append(f"{stage}: {str(e)[:300]}")
        raise
    stats["stages"].setdefault(stage, 0.0)
    stats["stages"][stage] += time.perf_counter() - t
    return r


def run(mode: str, src: Path, out: Path) -> dict:
    os.environ["FAST_PIPELINE"] = "0" if mode == "legacy" else "1"
    os.environ["BASE_PREP"] = "1" if mode == "fast_baseprep" else "0"
    os.environ["CACHE_DIR"] = str(out / "cache")
    prep = {}

    def prepared(key, fmt):
        if not baseprep.enabled():
            return None
        if (key, fmt) not in prep:
            ref = {"storage_path": f"bench/{key}.mp4", "artifact_id": key, "cache_key": "bench"}
            prep[(key, fmt)] = timed(st, f"BASE_PREP_{fmt}", lambda: baseprep.prepared_base(
                ref, fmt, lambda sp, dst: (shutil.copyfile(norm[key], dst), Path(dst))[1]))
            st["reencodes"] += 1
        return prep[(key, fmt)]
    out.mkdir(parents=True, exist_ok=True)
    st = {"mode": mode, "stages": {}, "errors": [], "reencodes": 0, "methods": {}, "ugcs": [], "renders": 0}
    t0 = time.perf_counter()

    def note(stage, rep):
        st["reencodes"] += rep.get("reencodes", 0)
        st["methods"].setdefault(stage, {}).setdefault(rep.get("method", "?"), 0)
        st["methods"][stage][rep.get("method", "?")] += 1

    norm, mattes = {}, {}
    for f in sorted(src.glob("*.mp4")):
        kind = f.stem.split("_")[0]
        if kind not in ("hook", "body1", "cta", "base"):
            continue
        dst = out / f"norm_{f.stem}.mp4"
        if kind == "base":
            timed(st, "NORMALIZE", lambda: ffmpeg.normalize(f, dst)); st["reencodes"] += 1
        else:
            _, rep = timed(st, "NORMALIZE", lambda: ffmpeg.normalize_fast(f, dst, DB, PAUSE)); note("NORMALIZE", rep)
            real = src / f"matte_{f.stem}.mp4"
            m = out / f"matte_{f.stem}.mp4"
            if real.exists():
                m = real
            else:  # máscara sintética com os MESMOS frames do asset normalizado (RVM não medido)
                timed(st, "ASSET_MATTE(sintético)", lambda: sh("-i", str(dst), "-an", "-vf", "scale=540:960,format=gray",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", str(m)))
            mattes[f.stem] = m
        norm[f.stem] = dst
    hooks = sorted(k for k in norm if k.startswith("hook"))
    bodies = sorted(k for k in norm if k.startswith("body1"))
    ctas = sorted(k for k in norm if k.startswith("cta"))
    bases = sorted(k for k in norm if k.startswith("base"))
    n = 0
    for h in hooks:
        for b in bodies:
            for c in ctas:
                keys = [h, b, c]
                bkey = bases[n % len(bases)]
                base = norm[bkey]  # distribuição: 1 Base por UGC
                u = out / f"ugc{n:02d}"
                mix, rep = timed(st, "MIX", lambda: ffmpeg.concat_fast([norm[k] for k in keys], Path(f"{u}_mix.mp4"))); note("MIX", rep)
                trim, rep = timed(st, "TRIM", lambda: ffmpeg.trim_fast(mix, Path(f"{u}_trim.mp4"), DB, PAUSE)); note("TRIM", rep)
                keep = ffmpeg.silence_keep(mix, DB, PAUSE)
                um, rep = timed(st, "UGC_MATTE", lambda: ffmpeg.ugc_matte_fast([mattes[k] for k in keys], keep, mix,
                                                                              Path(f"{u}_umatte.mp4"))); note("UGC_MATTE", rep)
                if mode != "legacy" and timed(st, "RENDER_CLEAN", lambda: ffmpeg.matches_final(ffmpeg.probe(trim))):
                    st["methods"].setdefault("RENDER_CLEAN", {}).setdefault("reuse_trim", 0)
                    st["methods"]["RENDER_CLEAN"]["reuse_trim"] += 1  # LIMPO = artifact do TRIM
                else:
                    timed(st, "RENDER_CLEAN", lambda: ffmpeg.render_clean(trim, Path(f"{u}_clean.mp4")))
                ps, pr = prepared(bkey, "SPLIT"), prepared(bkey, "REACT")
                timed(st, "RENDER_SPLIT", lambda: ffmpeg.render_split(trim, base, Path(f"{u}_split.mp4"), prepared=ps)); st["reencodes"] += 1
                timed(st, "RENDER_REACT", lambda: ffmpeg.render_react(base, um, trim, Path(f"{u}_react.mp4"), prepared=pr)); st["reencodes"] += 1
                st["renders"] += 3
                info = fastpath.stream_info(trim)
                st["ugcs"].append({"ugc": n, "trim_frames": int(info["v"]["nb_read_packets"]),
                                   "matte_frames": fastpath.frame_count(um),
                                   "video_s": float(info["v"].get("duration") or 0),
                                   "audio_s": float((info["a"] or {}).get("duration") or 0)})
                n += 1
    st["total_s"] = round(time.perf_counter() - t0, 2)
    st["stages"] = {k: round(v, 2) for k, v in st["stages"].items()}
    st["peak_child_rss_mb"] = round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1)
    st["mismatched_matte"] = [u["ugc"] for u in st["ugcs"] if u["trim_frames"] != u["matte_frames"]]
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", type=Path)
    ap.add_argument("--out", type=Path, default=Path("/tmp/fabrica_bench"))
    ap.add_argument("--secs", type=float, default=3.0)
    a = ap.parse_args()
    src = a.assets or (a.out / "src")
    if not a.assets:
        synth(src, a.secs)
    res = [run(m, src, a.out / m) for m in ("legacy", "fast", "fast_baseprep")]
    (a.out / "result.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))
    stages = sorted({k for r in res for k in r["stages"]})
    print(f"{'etapa':<24}" + "".join(f"{r['mode']:>16}" for r in res))
    for k in stages + ["total_s", "reencodes", "renders", "peak_child_rss_mb"]:
        print(f"{k:<24}" + "".join(f"{(r['stages'].get(k, 0) if k in stages else r[k]):>16}" for r in res))
    for r in res:
        print(r["mode"], "métodos:", json.dumps(r["methods"], ensure_ascii=False), "| erros:", r["errors"],
              "| máscara≠vídeo:", r["mismatched_matte"])


if __name__ == "__main__":
    main()
