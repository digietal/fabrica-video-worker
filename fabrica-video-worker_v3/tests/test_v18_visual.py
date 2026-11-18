"""Testes v18: estilos da TELA DIVIDIDA, margem do REACT, expansão da máscara e PREVIEW."""
import os, tempfile, unittest, subprocess
from pathlib import Path
from unittest import mock

os.environ.setdefault("WORKER_SECRET_TOKEN", "test-token")
from worker import ffmpeg, matting, pipeline  # noqa: E402


def _src(tmp, w=1080, h=1920, secs=2, name="raw"):
    raw = Path(tmp) / f"{name}.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=30", "-f", "lavfi",
                    "-i", "sine=f=440:r=48000", "-t", str(secs), "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", str(raw)], check=True, capture_output=True)
    return ffmpeg.normalize(raw, Path(tmp) / f"{name}_norm.mp4")


class SplitStylesTest(unittest.TestCase):
    def test_expand_default_keeps_v14_graph(self):
        cmds = []
        with mock.patch.object(ffmpeg, "_run", lambda c, o=None: cmds.append(c)), \
             mock.patch.object(ffmpeg, "_enc_audio_copy", lambda u: ["-c:a", "copy"]):
            ffmpeg.render_split(Path("u"), Path("b"), Path("o"))
        fc = cmds[0][cmds[0].index("-filter_complex") + 1]
        self.assertIn("(ih-960)*0.15", fc)       # corte ancorado perto do topo
        self.assertNotIn("gblur", fc)            # sem fundo desfocado
        self.assertIn("vstack=inputs=2", fc)

    def test_blur_style_uses_contain_plus_blurred_fill(self):
        cmds = []
        with mock.patch.object(ffmpeg, "_run", lambda c, o=None: cmds.append(c)), \
             mock.patch.object(ffmpeg, "_enc_audio_copy", lambda u: ["-c:a", "copy"]):
            ffmpeg.render_split(Path("u"), Path("b"), Path("o"), style="BLUR")
        fc = cmds[0][cmds[0].index("-filter_complex") + 1]
        self.assertIn("force_original_aspect_ratio=decrease", fc)  # avatar inteiro, sem corte
        self.assertIn("gblur=sigma=24", fc)                        # preenchimento desfocado
        self.assertIn("overlay=(W-w)/2:(H-h)/2", fc)               # centralizado

    def test_both_styles_render_valid_1080x1920(self):
        with tempfile.TemporaryDirectory() as t:
            trim = _src(t)
            for style in ("EXPAND", "BLUR"):
                m = ffmpeg.probe(ffmpeg.render_split(trim, trim, Path(t) / f"s_{style}.mp4", style=style))
                self.assertEqual((m["width"], m["height"], m["fps"], m["audio_codec"]),
                                 (1080, 1920, 30.0, "aac"), style)
                self.assertAlmostEqual(m["duration"], ffmpeg.probe(trim)["duration"], delta=0.1)


class ReactMarginTest(unittest.TestCase):
    def _graph(self, margin):
        cmds = []
        with mock.patch.object(ffmpeg, "_run", lambda c, o=None: cmds.append(c)), \
             mock.patch.object(ffmpeg, "_enc_audio_copy", lambda u: ["c:a", "copy"]):
            ffmpeg.render_react(Path("b"), Path("m"), Path("u"), Path("o"), bottom_margin=margin)
        return cmds[0][cmds[0].index("-filter_complex") + 1]

    def test_default_margin_keeps_old_position(self):
        self.assertIn("pad=1080:1920:408:744", self._graph(24))  # 1920-1152-24 = 744

    def test_zero_margin_flushes_bottom(self):
        self.assertIn("pad=1080:1920:432:768", self._graph(0))   # x=1080-648-0, y=1920-1152-0

    def test_crop_anchored_top_preserves_head(self):
        self.assertIn("crop=648:1152:(iw-648)/2:0", self._graph(0))


class MatteDilateTest(unittest.TestCase):
    def test_dilate_expands_mask(self):
        import numpy as np
        a = np.zeros((50, 50), np.float32)
        a[20:30, 20:30] = 1.0
        d = matting._dilate(a, 3)
        self.assertGreater(float(d.mean()), float(a.mean()))
        self.assertEqual(float(d[17, 25]), 1.0)   # 3px acima do bloco
        self.assertEqual(float(d[16, 25]), 0.0)   # 4px acima: fora

    def test_dilate_zero_is_noop(self):
        import numpy as np
        a = np.random.rand(20, 20).astype(np.float32)
        self.assertTrue(np.array_equal(matting._dilate(a, 0), a))


class PreviewTest(unittest.TestCase):
    def test_max_dur_limits_split_output(self):
        with tempfile.TemporaryDirectory() as t:
            trim = _src(t, secs=6)
            m = ffmpeg.probe(ffmpeg.render_split(trim, trim, Path(t) / "p.mp4", max_dur=4.0))
            self.assertLessEqual(m["duration"], 4.2)
            self.assertEqual((m["width"], m["height"]), (1080, 1920))

    def test_preview_rejects_invalid_format(self):
        with self.assertRaises(RuntimeError):
            pipeline.process_preview({"id": "x", "payload": {"format": "CLEAN"}})

    def test_preview_split_end_to_end(self):
        with tempfile.TemporaryDirectory() as t:
            trim = _src(t, secs=6)
            uploaded = {}
            job = {"id": "job1234567890", "production_run_id": "r", "payload": {
                "format": "SPLIT", "ugc_hash": "h", "creative_code": "G01-C01-CTA01-B01-PREVIEW-SPLIT",
                "split_style": "BLUR",
                "inputs": {"trim": {"storage_path": "o/p/r/trim/h.mp4"}, "base": {"storage_path": "o/p/r/norm/b.mp4"}}}}
            def fake_download(sp, dest):
                Path(dest).write_bytes(Path(trim).read_bytes())
                return dest
            with mock.patch.object(pipeline.storage, "download", side_effect=fake_download), \
                 mock.patch.object(pipeline.storage, "run_dir", return_value="o/p/r"), \
                 mock.patch.object(pipeline.storage, "upload", side_effect=lambda f, p, m: uploaded.setdefault(p, f)), \
                 mock.patch.object(pipeline, "complete_job", return_value={"ok": True}) as cj:
                pipeline.process_preview(job)
            self.assertEqual(cj.call_args[0][0], "job1234567890")
            art = cj.call_args[0][1]["artifacts"][0]
            self.assertEqual(art["kind"], "PREVIEW")
            self.assertIn("/previews/", art["storage_path"])
            self.assertEqual(art["metadata"]["format"], "SPLIT")
            self.assertTrue(art["metadata"]["preview"])


if __name__ == "__main__":
    unittest.main()
