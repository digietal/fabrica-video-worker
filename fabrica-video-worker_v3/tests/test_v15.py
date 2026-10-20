"""Testes da rodada v15: cache key do modo rápido (H), reutilização do LIMPO (D),
BASE_PREP (G) e cache de renders (I/J). Rodar: python -m unittest discover tests"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("WORKER_SECRET_TOKEN", "test-token")
from worker import baseprep, cache, ffmpeg, pipeline  # noqa: E402
from worker.normalize import process_normalize  # noqa: E402


def raw(tmp, name, secs=1.0, size="1080x1920", audio=True):
    import subprocess
    p = Path(tmp) / f"{name}.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=red:size={size}:rate=30:d={secs}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={secs}", "-ac", "2"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"] + \
           (["-c:a", "aac", "-ar", "48000"] if audio else []) + [str(p)]
    subprocess.run(cmd, check=True, capture_output=True)
    return p


class CacheKeyMode(unittest.TestCase):
    """H: NORMALIZE com decupagem de borda reporta cache_key com sufixo do modo."""

    def test_edge_trim_suffixes_cache_key(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"FAST_PIPELINE": "1"}), \
                mock.patch("worker.normalize.storage") as st, \
                mock.patch("worker.normalize.complete_job") as cp:
            st.run_dir.return_value = "o/p/r"
            st.download.side_effect = lambda sp, dest: (Path(raw(d, "src")).rename(dest), dest)[1]
            st.upload.return_value = "o/p/r/normalized/a1.mp4"
            # Áudio com fala só no meio -> edge_trim ativo
            import subprocess
            src = Path(d) / "speech.mp4"
            e = "if(between(t\\,0.6\\,1.4)\\,0.5*sin(2*PI*440*t)\\,0)"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                            "color=c=red:size=1080x1920:rate=30:d=2", "-f", "lavfi", "-i",
                            f"aevalsrc={e}|{e}:s=48000:d=2", "-c:v", "libx264", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", str(src)], check=True)
            st.download.side_effect = lambda sp, dest: (Path(dest).write_bytes(src.read_bytes()), dest)[1]
            job = {"id": "j1", "cache_key": "abc123",
                   "payload": {"asset_id": "a1", "storage_path": "o/p/r/hook/a1.mp4"}}
            process_normalize(job)
            sent = cp.call_args[0][1]["artifacts"][0]
            self.assertEqual(sent["cache_key"], "abc123|fast1")
            self.assertEqual(sent["metadata"]["pipeline"]["method"], "edge_trim")

    def test_legacy_keeps_cache_key(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"FAST_PIPELINE": "0"}), \
                mock.patch("worker.normalize.storage") as st, \
                mock.patch("worker.normalize.complete_job") as cp:
            st.run_dir.return_value = "o/p/r"
            src = raw(d, "plain")
            st.download.side_effect = lambda sp, dest: (Path(dest).write_bytes(src.read_bytes()), dest)[1]
            st.upload.return_value = "x"
            process_normalize({"id": "j2", "cache_key": "abc123",
                               "payload": {"asset_id": "a1", "storage_path": "o/p/r/hook/a1.mp4"}})
            self.assertEqual(cp.call_args[0][1]["artifacts"][0]["cache_key"], "abc123")


class CleanReuse(unittest.TestCase):
    """D: RENDER_CLEAN reutiliza o artifact do TRIM quando ele já está no formato final."""

    def _job(self, trim_path):
        return {"id": "j3", "production_item_id": "pi1", "cache_key": "k",
                "payload": {"creative_code": "C1", "inputs": {
                    "trim": {"storage_path": "o/p/r/trim/u1.mp4", "artifact_id": "art-trim-1",
                             "cache_key": "ck", "kind": "TRIM"}}}}

    def test_reuses_trim_artifact(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"FAST_PIPELINE": "1"}), \
                mock.patch("worker.pipeline.storage") as st, \
                mock.patch("worker.pipeline.complete_job") as cp:
            final = raw(d, "final")  # já 1080x1920/30/h264/aac48k estéreo? raw gera isso
            final2 = ffmpeg.normalize(final, Path(d) / "norm.mp4")  # garante formato final exato
            st.download.side_effect = lambda sp, dest: (Path(dest).write_bytes(final2.read_bytes()), dest)[1]
            pipeline.process_render_clean(self._job(final2))
            result = cp.call_args[0][1]
            self.assertEqual(result.get("artifact_id"), "art-trim-1")
            self.assertNotIn("artifacts", result)
            st.upload.assert_not_called()

    def test_falls_back_when_not_final(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"FAST_PIPELINE": "1"}), \
                mock.patch("worker.pipeline.storage") as st, \
                mock.patch("worker.pipeline.complete_job") as cp:
            odd = raw(d, "odd", size="640x480")  # fora do formato final
            st.run_dir.return_value = "o/p/r"
            st.download.side_effect = lambda sp, dest: (Path(dest).write_bytes(odd.read_bytes()), dest)[1]
            st.upload.return_value = "o/p/r/renders/C1.mp4"
            pipeline.process_render_clean(self._job(odd))
            result = cp.call_args[0][1]
            self.assertIn("artifacts", result)  # caminho legado: novo artifact enviado
            st.upload.assert_called_once()

    def test_flag_off_is_legacy(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"FAST_PIPELINE": "0"}), \
                mock.patch("worker.pipeline.storage") as st, \
                mock.patch("worker.pipeline.complete_job") as cp:
            final2 = ffmpeg.normalize(raw(d, "f"), Path(d) / "n.mp4")
            st.run_dir.return_value = "o/p/r"
            st.download.side_effect = lambda sp, dest: (Path(dest).write_bytes(final2.read_bytes()), dest)[1]
            st.upload.return_value = "o/p/r/renders/C1.mp4"
            pipeline.process_render_clean(self._job(final2))
            self.assertIn("artifacts", cp.call_args[0][1])


class BasePrep(unittest.TestCase):
    """G: Base preparada uma vez por formato; flag desligada = None; reuso sem novo preparo."""

    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASE_PREP": "1", "CACHE_DIR": self.t.name})
        self.env.start()
        import importlib
        importlib.reload(cache)
        importlib.reload(baseprep)

    def tearDown(self):
        self.env.stop()
        self.t.cleanup()
        import importlib
        importlib.reload(cache)
        importlib.reload(baseprep)

    def test_prepare_once_reuse_after(self):
        base = raw(self.t.name, "base", secs=1.0, audio=False)
        ref = {"storage_path": "o/p/r/base/b1.mp4", "artifact_id": "art-b1", "cache_key": "ck"}
        calls = []

        def download(sp, dest):
            calls.append(sp)
            Path(dest).write_bytes(base.read_bytes())
            return dest

        p1 = baseprep.prepared_base(ref, "SPLIT", download)
        self.assertIsNotNone(p1 and p1.exists())
        m = ffmpeg.probe(p1)
        self.assertEqual((m["width"], m["height"], m["fps"]), (1080, 960, 30.0))
        self.assertIsNone(m["audio_codec"])
        p2 = baseprep.prepared_base(ref, "SPLIT", download)
        self.assertEqual(p1, p2)
        self.assertEqual(len(calls), 1)  # segunda vez: sem download nem preparo

    def test_formats_are_separate(self):
        base = raw(self.t.name, "base", secs=1.0, audio=False)
        ref = {"storage_path": "o/p/r/base/b1.mp4", "artifact_id": "art-b1", "cache_key": "ck"}
        dl = lambda sp, dest: (Path(dest).write_bytes(base.read_bytes()), dest)[1]
        s = baseprep.prepared_base(ref, "SPLIT", dl)
        r = baseprep.prepared_base(ref, "REACT", dl)
        self.assertNotEqual(s, r)
        self.assertEqual((ffmpeg.probe(r)["width"], ffmpeg.probe(r)["height"]), (1080, 1920))

    def test_flag_off_returns_none(self):
        with mock.patch.dict(os.environ, {"BASE_PREP": "0"}):
            self.assertIsNone(baseprep.prepared_base(
                {"storage_path": "x", "artifact_id": "y"}, "SPLIT", lambda *a: None))

    def test_render_split_with_prepared_matches_layout(self):
        ugc = ffmpeg.normalize(raw(self.t.name, "u", secs=1.0), Path(self.t.name) / "un.mp4")
        base = raw(self.t.name, "b", secs=1.0, audio=False)
        ref = {"storage_path": "o/p/r/base/b1.mp4", "artifact_id": "art-b1", "cache_key": "ck"}
        prep = baseprep.prepared_base(ref, "SPLIT", lambda sp, dest: (Path(dest).write_bytes(base.read_bytes()), dest)[1])
        out = Path(self.t.name) / "split.mp4"
        ffmpeg.render_split(ugc, base, out, prepared=prep)
        m = ffmpeg.probe(out)
        self.assertEqual((m["width"], m["height"], m["fps"], m["audio_codec"]), (1080, 1920, 30.0, "aac"))

    def test_render_react_with_prepared(self):
        ugc = ffmpeg.normalize(raw(self.t.name, "u", secs=1.0), Path(self.t.name) / "un.mp4")
        base = raw(self.t.name, "b", secs=1.0, audio=False)
        import subprocess
        mask = Path(self.t.name) / "mask.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(ugc), "-an", "-vf", "format=gray",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "30",
                        str(mask)], check=True)
        ref = {"storage_path": "o/p/r/base/b1.mp4", "artifact_id": "art-b1", "cache_key": "ck"}
        prep = baseprep.prepared_base(ref, "REACT", lambda sp, dest: (Path(dest).write_bytes(base.read_bytes()), dest)[1])
        out = Path(self.t.name) / "react.mp4"
        ffmpeg.render_react(base, mask, ugc, out, prepared=prep)
        m = ffmpeg.probe(out)
        self.assertEqual((m["width"], m["height"], m["audio_codec"]), (1080, 1920, "aac"))


class RenderCache(unittest.TestCase):
    """I/J: renders finais são cacheáveis (THUMBNAIL/ZIP evitam novos downloads)."""

    def test_render_kinds_cacheable(self):
        for k in ("RENDER_CLEAN", "RENDER_SPLIT", "RENDER_REACT"):
            ref = {"storage_path": f"o/p/r/renders/{k}.mp4", "artifact_id": "a", "cache_key": "c", "kind": k}
            self.assertIsNotNone(cache.key_for(ref))


if __name__ == "__main__":
    unittest.main()
