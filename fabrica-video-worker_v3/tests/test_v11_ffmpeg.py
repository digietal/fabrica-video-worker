"""Testes v11: LIMPO por stream copy, áudio copiado em SPLIT/REACT, memória sem page cache."""
import os, tempfile, unittest, subprocess
from pathlib import Path
from unittest import mock
os.environ.setdefault("WORKER_SECRET_TOKEN", "test-token")
from worker import ffmpeg, metrics, main  # noqa: E402


def _src(tmp, w=1080, h=1920, secs=2):
    raw = Path(tmp) / "raw.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=30", "-f", "lavfi",
                    "-i", "sine=f=440:r=48000", "-t", str(secs), "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", str(raw)], check=True, capture_output=True)
    trim = Path(tmp) / "trim.mp4"
    return ffmpeg.normalize(raw, trim)


class V11(unittest.TestCase):
    def test_clean_stream_copy_identical_format(self):
        with tempfile.TemporaryDirectory() as t:
            trim = _src(t)
            with mock.patch.object(ffmpeg, "ENC", ["SHOULD_NOT_BE_USED"]):
                out = ffmpeg.render_clean(trim, Path(t) / "clean.mp4")  # copy path never uses ENC
            a, b = ffmpeg.probe(trim), ffmpeg.probe(out)
            for k in ("width", "height", "fps", "video_codec", "audio_codec", "audio_sample_rate", "audio_channels"):
                self.assertEqual(a[k], b[k], k)
            self.assertAlmostEqual(a["duration"], b["duration"], delta=0.05)

    def test_clean_falls_back_to_encode_when_not_final(self):
        with tempfile.TemporaryDirectory() as t:
            raw = Path(t) / "raw.mp4"
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=25", "-f", "lavfi",
                            "-i", "sine=r=44100", "-t", "1", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                            str(raw)], check=True, capture_output=True)
            m = ffmpeg.probe(ffmpeg.render_clean(raw, Path(t) / "c.mp4"))
            self.assertEqual((m["width"], m["height"], m["fps"], m["audio_sample_rate"]), (1080, 1920, 30.0, "48000"))

    def test_split_audio_copy(self):
        with tempfile.TemporaryDirectory() as t:
            trim = _src(t)
            self.assertIn("copy", ffmpeg._enc_audio_copy(trim))
            m = ffmpeg.probe(ffmpeg.render_split(trim, trim, Path(t) / "s.mp4"))
            self.assertEqual((m["width"], m["height"], m["audio_codec"], m["audio_channels"]), (1080, 1920, "aac", 2))
            self.assertAlmostEqual(m["duration"], ffmpeg.probe(trim)["duration"], delta=0.1)

    def test_memory_discounts_inactive_file(self):
        files = {"/sys/fs/cgroup/memory.current": str(900 * 2**20), "/sys/fs/cgroup/memory.max": str(1024 * 2**20),
                 "/sys/fs/cgroup/memory.stat": f"anon 1\ninactive_file {500 * 2**20}\n"}
        with mock.patch.object(metrics, "_read", side_effect=lambda p: files.get(p)):
            m = metrics.memory_mb()
        self.assertEqual((m["mem_used_mb"], m["mem_free_mb"]), (400, 624))

    def test_memory_thresholds(self):
        with mock.patch.object(metrics, "memory_mb", return_value={"mem_free_mb": 300}):
            self.assertTrue(main.memory_ok())
            self.assertFalse(main.memory_ok(main.HEAVY_MIN_FREE_MEM_MB))


if __name__ == "__main__":
    unittest.main()
