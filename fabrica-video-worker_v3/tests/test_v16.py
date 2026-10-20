import os, threading, time, unittest
from unittest import mock


class HeavyWaitReleaseTest(unittest.TestCase):
    def _runner(self, flag):
        with mock.patch.dict(os.environ, {"HEAVY_WAIT_RELEASE": flag}):
            import importlib, worker.main as M
            M = importlib.reload(M)
        return M

    def _scenario(self, flag):
        M = self._runner(flag)
        started, gate = [], threading.Event()
        def dispatch(job):
            started.append((job["id"], time.monotonic()))
            if job["type"] == "RENDER_REACT":
                gate.wait(2)
        with mock.patch.object(M, "memory_ok", return_value=True), mock.patch.object(M.metrics, "log"):
            r = M.Runner(dispatch_fn=dispatch, fail_fn=lambda *a: None, concurrency=2, heavy=1)
            for jid, typ in (("h1", "RENDER_REACT"), ("h2", "RENDER_REACT")):
                r.acquire_slot(); r.submit({"id": jid, "type": typ})
            time.sleep(0.2)
            got = r.acquire_slot(timeout=0.3)  # há vaga para um job leve enquanto h2 espera?
            if got:
                r.submit({"id": "light", "type": "MIX"})
            time.sleep(0.2)
            ids = [s[0] for s in started]
            gate.set(); r.pool.shutdown(wait=True)
        return got, ids

    def test_flag_off_keeps_v15_behavior(self):
        got, ids = self._scenario("0")
        self.assertFalse(got)
        self.assertNotIn("light", ids)

    def test_flag_on_light_job_runs_while_heavy_waits(self):
        got, ids = self._scenario("1")
        self.assertTrue(got)
        self.assertIn("light", ids)
        self.assertNotIn("h2", ids[:2])  # h2 nunca roda junto com h1 (HEAVY_CONCURRENCY respeitado)


class RenderOptGraphTest(unittest.TestCase):
    def test_default_off(self):
        from worker import ffmpeg as F
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RENDER_OPT", None)
            self.assertFalse(F.render_opt())

    def test_react_opt_graph_region_merge(self):
        from worker import ffmpeg as F
        cmds = []
        with mock.patch.dict(os.environ, {"RENDER_OPT": "1"}), \
             mock.patch.object(F, "_run", lambda c, o=None: cmds.append(c)), \
             mock.patch.object(F, "_enc_audio_copy", lambda u: ["-c:a", "copy"]):
            from pathlib import Path
            F.render_react(Path("b"), Path("m"), Path("u"), Path("o"))
        fc = cmds[0][cmds[0].index("-filter_complex") + 1]
        self.assertIn("overlay=x=408:y=744:shortest=1", fc)
        self.assertNotIn("pad=", fc)
        self.assertNotIn("rgba", fc)


if __name__ == "__main__":
    unittest.main()
