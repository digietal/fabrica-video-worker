"""Testes FAST_PIPELINE (Fase 1). Rodar: python -m unittest discover tests"""
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("WORKER_SECRET_TOKEN", "test-token")
from worker import fastpath, ffmpeg  # noqa: E402

DB, PAUSE = -35, 250


def sh(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, capture_output=True)


def raw(tmp, name, color="red", secs=1.5, rate=30, size="1080x1920", speech=None, audio_secs=None):
    """Vídeo sintético. speech=(ini, fim): tom audível só nesse trecho (resto silêncio)."""
    p = Path(tmp) / f"{name}_src.mp4"
    expr = f"if(between(t,{speech[0]},{speech[1]}),0.5*sin(2*PI*440*t),0)" if speech else "0.5*sin(2*PI*440*t)"
    expr = expr.replace(",", "\\,")
    sh("-f", "lavfi", "-i", f"color=c={color}:size={size}:rate={rate}:d={secs}",
       "-f", "lavfi", "-i", f"aevalsrc={expr}|{expr}:s=48000:d={audio_secs or secs}",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", str(p))
    return p


def norm(tmp, name, **kw):
    return ffmpeg.normalize(raw(tmp, name + "_raw", **kw), Path(tmp) / f"{name}.mp4")


def md5(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest()


def color_at(path, t):
    """Canal dominante (r/g/b) do frame no instante t."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=4:4", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    r, g, b = (sum(out[i::3]) for i in range(3))
    return "rgb"[[r, g, b].index(max(r, g, b))]


class Fast(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FAST_PIPELINE": "1"})
        self.env.start()
        self.t = tempfile.TemporaryDirectory()
        self.d = self.t.name

    def tearDown(self):
        self.env.stop()
        self.t.cleanup()

    # 1. Compatíveis -> stream copy, frames exatos, ordem preservada
    def test_compatible_concat_copy_and_order(self):
        parts = [norm(self.d, "h", color="red"), norm(self.d, "b", color="lime", secs=1.0), norm(self.d, "c", color="blue")]
        out, rep = ffmpeg.concat_fast(parts, Path(self.d) / "mix.mp4")
        self.assertEqual((rep["method"], rep["reencodes"]), ("concat_copy", 0))
        self.assertEqual(rep["frames"], sum(fastpath.frame_count(p) for p in parts))
        self.assertEqual([color_at(out, t) for t in (0.7, 2.0, 3.2)], ["r", "g", "b"])
        m = ffmpeg.probe(out)
        self.assertEqual((m["width"], m["height"], m["fps"], m["audio_codec"]), (1080, 1920, 30.0, "aac"))

    # 2. Incompatíveis -> legado
    def test_incompatible_falls_back_to_legacy(self):
        a = norm(self.d, "a")
        b = raw(self.d, "b_other", color="blue")  # 1080x1920/30fps, mas outro perfil H.264 e timebase
        b2 = Path(self.d) / "b_tb.mp4"
        sh("-i", str(b), "-c", "copy", "-video_track_timescale", "30000", str(b2))
        b = b2
        out, rep = ffmpeg.concat_fast([a, b], Path(self.d) / "mix.mp4")
        self.assertEqual(rep["method"], "legacy")
        self.assertIn("Incompatible", rep["fallback_reason"])
        self.assertEqual(ffmpeg.probe(out)["fps"], 30.0)

    # 3. Timestamps descontínuos
    def test_discontinuous_timestamps_rejected(self):
        p = Path(self.d) / "gap.mp4"
        sh("-f", "lavfi", "-i", "color=c=red:size=320x240:rate=30:d=1", "-vf", "setpts=N/30/TB+gte(N\\,15)*0.5/TB",
           "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "ultrafast", str(p))
        with self.assertRaises(fastpath.Incompatible):
            fastpath.check_timestamps(p)
        fastpath.check_timestamps(norm(self.d, "ok"))  # contínuo passa

    # 4. Divergência de duração vídeo x áudio
    def test_av_duration_mismatch_rejected(self):
        bad = Path(self.d) / "bad.mp4"
        sh("-f", "lavfi", "-i", "color=c=red:size=1080x1920:rate=30:d=1", "-f", "lavfi", "-i",
           "sine=r=48000:d=2", "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ar", "48000", str(bad))
        with self.assertRaises(fastpath.Incompatible) as e:
            fastpath.check_compatible([fastpath.stream_info(bad)])
        self.assertIn("duração", str(e.exception))
        _, rep = ffmpeg.concat_fast([norm(self.d, "ok"), bad], Path(self.d) / "mix.mp4")
        self.assertEqual(rep["method"], "legacy")

    # 5. Frames da máscara ≠ frames do vídeo
    def test_matte_frame_mismatch_falls_back(self):
        mattes = [norm(self.d, "m1", color="white", secs=1.0), norm(self.d, "m2", color="white", secs=1.0)]
        mattes = [self._gray(m) for m in mattes]
        mix = norm(self.d, "mix", secs=2.5)  # 75 frames ≠ 60
        keep = [(0.0, ffmpeg.probe(mix)["duration"])]
        out, rep = ffmpeg.ugc_matte_fast(mattes, keep, mix, Path(self.d) / "um.mp4")
        self.assertEqual(rep["method"], "legacy")
        self.assertIn("frames", rep["fallback_reason"])
        self.assertTrue(out.exists())

    def test_matte_concat_copy_when_frames_match(self):
        mattes = [self._gray(norm(self.d, f"m{i}", color="white", secs=1.0)) for i in range(2)]
        mix = norm(self.d, "mix", secs=2.0)
        out, rep = ffmpeg.ugc_matte_fast(mattes, [(0.0, ffmpeg.probe(mix)["duration"])], mix, Path(self.d) / "um.mp4")
        self.assertEqual((rep["method"], rep["frames"], rep["video_frames"]), ("concat_copy", 60, 60))
        self.assertIsNone(ffmpeg.probe(out)["audio_codec"])

    def test_matte_with_trim_cuts_uses_legacy(self):
        mattes = [self._gray(norm(self.d, "m", color="white", secs=2.0))]
        mix = norm(self.d, "mix", secs=2.0)
        _, rep = ffmpeg.ugc_matte_fast(mattes, [(0.0, 0.8), (1.2, 2.0)], mix, Path(self.d) / "um.mp4")
        self.assertEqual(rep["method"], "legacy")

    # 6. Falha durante a concatenação -> legado, sem lixo
    def test_concat_failure_falls_back(self):
        parts = [norm(self.d, "a"), norm(self.d, "b")]
        with mock.patch.object(fastpath, "_run", side_effect=fastpath.Incompatible("simulada")):
            out, rep = ffmpeg.concat_fast(parts, Path(self.d) / "mix.mp4")
        self.assertEqual(rep["method"], "legacy")
        self.assertIn("simulada", rep["fallback_reason"])
        self.assertFalse((Path(self.d) / "mix.concat.txt").exists())
        self.assertGreater(ffmpeg.probe(out)["duration"], 2.9)

    # 7. Flag desligada = legado puro (caminho rápido nunca é tocado)
    def test_flag_off_is_pure_legacy(self):
        parts = [norm(self.d, "a"), norm(self.d, "b")]
        with mock.patch.dict(os.environ, {"FAST_PIPELINE": "0"}), \
                mock.patch.object(fastpath, "concat_copy", side_effect=AssertionError("não deveria")), \
                mock.patch.object(fastpath, "copy_file", side_effect=AssertionError("não deveria")):
            _, rep = ffmpeg.concat_fast(parts, Path(self.d) / "mix.mp4")
            _, rep2 = ffmpeg.trim_fast(parts[0], Path(self.d) / "trim.mp4", DB, PAUSE)
            _, rep3 = ffmpeg.normalize_fast(raw(self.d, "e", speech=(0.6, 1.0)), Path(self.d) / "n.mp4", DB, PAUSE)
        self.assertEqual([r["method"] for r in (rep, rep2, rep3)], ["legacy"] * 3)

    # 8. Idempotência / retry: mesmo resultado byte a byte
    def test_idempotent_retry(self):
        parts = [norm(self.d, "a"), norm(self.d, "b", color="blue")]
        o1, _ = ffmpeg.concat_fast(parts, Path(self.d) / "m1.mp4")
        o2, _ = ffmpeg.concat_fast(parts, Path(self.d) / "m1.mp4")  # retry sobrescreve o mesmo destino
        o3, _ = ffmpeg.concat_fast(parts, Path(self.d) / "m3.mp4")
        self.assertEqual(md5(o2), md5(o3))
        self.assertEqual(o1, o2)

    # TRIM: sem cortes -> cópia; com pausa interna -> legado
    def test_trim_copy_only_without_cuts(self):
        full = norm(self.d, "full")
        _, rep = ffmpeg.trim_fast(full, Path(self.d) / "t1.mp4", DB, PAUSE)
        self.assertEqual((rep["method"], rep["reencodes"]), ("copy_no_cuts", 0))
        paused = ffmpeg.normalize(raw(self.d, "p", secs=2.0, speech=(0.0, 0.8)), Path(self.d) / "p.mp4")
        out, rep = ffmpeg.trim_fast(paused, Path(self.d) / "t2.mp4", DB, PAUSE)
        self.assertEqual(rep["method"], "legacy")
        self.assertLess(ffmpeg.probe(out)["duration"], 1.2)

    # Decupagem de bordas com margem + cadeia completa sem reencodificar MIX/TRIM
    def test_edge_trim_margin_and_chain(self):
        a, _ = ffmpeg.normalize_fast(raw(self.d, "a", secs=2.0, speech=(0.6, 1.4)), Path(self.d) / "a.mp4", DB, PAUSE)
        b, rb = ffmpeg.normalize_fast(raw(self.d, "b", color="blue", secs=2.0, speech=(0.5, 1.5)), Path(self.d) / "b.mp4", DB, PAUSE)
        self.assertEqual(rb["method"], "edge_trim")
        s, e = rb["edge_window"]
        self.assertLess(s, 0.5)  # margem antes da fala
        self.assertGreater(e, 1.5)  # margem depois da fala
        da = ffmpeg.probe(a)["duration"]
        self.assertGreater(da, 0.8 + 0.15)
        self.assertLess(da, 2.0)
        mix, rm = ffmpeg.concat_fast([a, b], Path(self.d) / "mix.mp4")
        self.assertEqual(rm["method"], "concat_copy")
        _, rt = ffmpeg.trim_fast(mix, Path(self.d) / "trim.mp4", DB, PAUSE)
        self.assertEqual(rt["method"], "copy_no_cuts")  # junção não vira pausa cortável

    def test_edge_window_rules(self):
        self.assertIsNone(fastpath.edge_window([(0.0, 2.0)], 2.0, PAUSE))
        s, e = fastpath.edge_window([(0.5, 1.5)], 2.0, PAUSE)
        self.assertLess((0.5 - s) + (e - 1.5), PAUSE / 1000)
        self.assertIsNone(fastpath.edge_window([(1.0, 1.05)], 2.0, PAUSE))  # sobraria quase nada

    def _gray(self, src):
        out = src.with_name(src.stem + "_gray.mp4")
        sh("-i", str(src), "-an", "-vf", "format=gray", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-r", "30", str(out))
        return out


if __name__ == "__main__":
    unittest.main()
