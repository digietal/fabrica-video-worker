"""Testes v10: concorrência, isolamento de falhas, reinício e cache. Rodar: python -m unittest discover tests"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("WORKER_SECRET_TOKEN", "test-token")
_CACHE_TMP = tempfile.mkdtemp()
os.environ["CACHE_DIR"] = _CACHE_TMP

from worker import cache, main, metrics  # noqa: E402
from worker.gateway import GatewayUnavailable  # noqa: E402


class FakeQueue:
    """Simula claim_next_job(): atômico, uma vez por job, respeitando dependências COMPLETED."""

    def __init__(self, jobs, deps=None):
        self.pending = list(jobs)
        self.deps = deps or {}
        self.completed, self.failed, self.claims = set(), set(), []
        self.lock = threading.Lock()

    def claim(self, _state):
        with self.lock:
            for j in self.pending:
                if all(d in self.completed for d in self.deps.get(j["id"], [])):
                    self.pending.remove(j)
                    self.claims.append(j["id"])
                    return j
            return None

    def done(self, jid):
        with self.lock:
            self.completed.add(jid)

    def fail(self, jid, _err):
        with self.lock:
            self.failed.add(jid)


def job(i, t="RENDER_CLEAN"):
    return {"id": f"j{i}", "type": t, "production_run_id": "run1", "attempts": 1, "payload": {}}


def run(queue, dispatch, concurrency=2, heavy=1, mode="once", max_jobs=100):
    r = main.Runner(dispatch_fn=dispatch, fail_fn=queue.fail, concurrency=concurrency, heavy=heavy)
    stop = threading.Event()
    claimed = main.run_loop(r, queue.claim, mode, max_jobs, stop, idle_sleep=0.05, mem_ok=lambda: True)
    r.shutdown()
    return claimed


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_independent_jobs_and_no_double_processing(self):
        q = FakeQueue([job(i) for i in range(6)])
        running, peak, seen = [0], [0], []
        lk = threading.Lock()

        def d(j):
            with lk:
                running[0] += 1; peak[0] = max(peak[0], running[0]); seen.append(j["id"])
            time.sleep(0.2)
            with lk:
                running[0] -= 1
            q.done(j["id"])

        t0 = time.monotonic()
        run(q, d, concurrency=3)
        self.assertEqual(sorted(seen), sorted(set(seen)))  # nenhum job duas vezes
        self.assertEqual(len(seen), 6)
        self.assertEqual(peak[0], 3)  # nunca acima do limite, e de fato em paralelo
        self.assertLess(time.monotonic() - t0, 1.0)  # 6×0,2 s sequencial = 1,2 s

    def test_dependencies_respected(self):
        q = FakeQueue([job("trim", "TRIM"), job("r1"), job("r2")], deps={"jr1": ["jtrim"], "jr2": ["jtrim"]})
        order = []

        def d(j):
            if j["id"] != "jtrim":
                self.assertIn("jtrim", q.completed)
            time.sleep(0.05); order.append(j["id"]); q.done(j["id"])

        # mode=loop com parada quando tudo concluir
        r = main.Runner(dispatch_fn=d, fail_fn=q.fail, concurrency=2, heavy=1)
        stop = threading.Event()
        th = threading.Thread(target=main.run_loop, args=(r, q.claim, "loop", 0, stop),
                              kwargs={"idle_sleep": 0.02, "mem_ok": lambda: True})
        th.start()
        deadline = time.time() + 3
        while len(q.completed) < 3 and time.time() < deadline:
            time.sleep(0.02)
        stop.set(); th.join(); r.shutdown()
        self.assertEqual(order[0], "jtrim")
        self.assertEqual(len(order), 3)

    def test_failure_does_not_block_independent_jobs(self):
        q = FakeQueue([job(i) for i in range(4)])

        def d(j):
            if j["id"] == "j1":
                raise RuntimeError("ffmpeg quebrou")
            q.done(j["id"])

        run(q, d)
        self.assertEqual(q.failed, {"j1"})
        self.assertEqual(q.completed, {"j0", "j2", "j3"})

    def test_comm_failure_not_marked_failed(self):
        q = FakeQueue([job(0)])

        def d(j):
            raise GatewayUnavailable("sem resposta do /complete")

        run(q, d)
        self.assertEqual(q.failed, set())  # fica para o lease de 60 min, nunca FAILED

    def test_heavy_jobs_serialized(self):
        q = FakeQueue([job(i, "RENDER_REACT") for i in range(3)] + [job(9)])
        heavy_now, heavy_peak, lk = [0], [0], threading.Lock()

        def d(j):
            if j["type"] == "RENDER_REACT":
                with lk:
                    heavy_now[0] += 1; heavy_peak[0] = max(heavy_peak[0], heavy_now[0])
                time.sleep(0.1)
                with lk:
                    heavy_now[0] -= 1
            q.done(j["id"])

        run(q, d, concurrency=3, heavy=1)
        self.assertEqual(heavy_peak[0], 1)
        self.assertEqual(len(q.completed), 4)

    def test_max_jobs_is_total_not_concurrency(self):
        q = FakeQueue([job(i) for i in range(10)])
        claimed = run(q, lambda j: q.done(j["id"]), concurrency=2, max_jobs=5)
        self.assertEqual(claimed, 5)
        self.assertEqual(len(q.pending), 5)

    def test_low_memory_pauses_claim(self):
        q = FakeQueue([job(0)])
        r = main.Runner(dispatch_fn=lambda j: q.done(j["id"]), fail_fn=q.fail, concurrency=2, heavy=1)
        stop = threading.Event()
        th = threading.Thread(target=main.run_loop, args=(r, q.claim, "loop", 0, stop),
                              kwargs={"mem_ok": lambda: False})
        th.start(); time.sleep(0.5); stop.set(); th.join(); r.shutdown()
        self.assertEqual(q.claims, [])  # nada reivindicado sem memória livre

    def test_restart_stop_waits_active_and_claims_nothing_new(self):
        q = FakeQueue([job(i) for i in range(5)])
        r = main.Runner(dispatch_fn=lambda j: (time.sleep(0.3), q.done(j["id"])), fail_fn=q.fail,
                        concurrency=2, heavy=1)
        stop = threading.Event()
        th = threading.Thread(target=main.run_loop, args=(r, q.claim, "loop", 0, stop),
                              kwargs={"idle_sleep": 0.02, "mem_ok": lambda: True})
        th.start(); time.sleep(0.1); stop.set(); th.join(); r.shutdown()
        self.assertEqual(len(q.claims), 2)
        self.assertEqual(q.completed, set(q.claims))  # ativos terminaram; o resto segue na fila


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.calls = []
        self.lock = threading.Lock()

    def ref(self, aid="a1", kind="NORMALIZE"):
        return {"storage_path": f"o/p/run1/normalized/{aid}.mp4", "artifact_id": aid, "cache_key": "k", "kind": kind}

    def downloader(self, content=b"video-bytes", delay=0.0, fail=False):
        def dl(sp, dst, _):
            with self.lock:
                self.calls.append(sp)
            time.sleep(delay)
            if fail:
                Path(dst).write_bytes(b"parcial")
                raise RuntimeError("rede caiu")
            Path(dst).write_bytes(content)
            return dst
        return dl

    def test_concurrent_same_key_downloads_once(self):
        ref = self.ref("conc")
        outs = []
        ths = [threading.Thread(target=lambda i=i: outs.append(
            cache.fetch(ref, self.tmp / f"o{i}.mp4", self.downloader(delay=0.1)))) for i in range(4)]
        [t.start() for t in ths]; [t.join() for t in ths]
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(Path(o).read_bytes() == b"video-bytes" for o in outs))

    def test_incomplete_download_never_cached(self):
        ref = self.ref("broken")
        with self.assertRaises(RuntimeError):
            cache.fetch(ref, self.tmp / "x.mp4", self.downloader(fail=True))
        key = cache.key_for(ref)
        bin_p, meta_p, _ = cache._paths(key)
        self.assertFalse(bin_p.exists())
        self.assertFalse(any(".part." in p.name for p in bin_p.parent.iterdir()))
        out = cache.fetch(ref, self.tmp / "y.mp4", self.downloader())
        self.assertEqual(Path(out).read_bytes(), b"video-bytes")

    def test_corrupted_entry_is_redownloaded(self):
        ref = self.ref("corrupt")
        cache.fetch(ref, self.tmp / "a.mp4", self.downloader())
        bin_p, _, _ = cache._paths(cache.key_for(ref))
        bin_p.write_bytes(b"x")  # tamanho diferente do registrado
        cache.fetch(ref, self.tmp / "b.mp4", self.downloader(content=b"video-bytes"))
        self.assertEqual(len(self.calls), 2)

    def test_different_artifact_or_run_never_shares_entry(self):
        a, b = self.ref("same"), dict(self.ref("same"), artifact_id="other")
        c = dict(self.ref("same"), storage_path="o/p/run2/normalized/same.mp4")
        self.assertEqual(len({cache.key_for(a), cache.key_for(b), cache.key_for(c)}), 3)

    def test_uncacheable_kinds_and_refs(self):
        self.assertIsNone(cache.key_for({"storage_path": "x", "kind": "NORMALIZE"}))  # sem artifact_id
        # v15: renders finais passaram a ser cacheáveis (THUMBNAIL/ZIP reutilizam o arquivo local).
        self.assertIsNotNone(cache.key_for(dict(self.ref(), kind="RENDER_CLEAN")))
        self.assertIsNone(cache.key_for(dict(self.ref(), kind="ZIP")))  # ZIP continua fora do cache

    def test_hit_survives_eviction(self):
        ref = self.ref("evict")
        out = cache.fetch(ref, self.tmp / "e.mp4", self.downloader())
        cache._drop(cache._paths(cache.key_for(ref))[0])
        self.assertEqual(Path(out).read_bytes(), b"video-bytes")  # hardlink do job continua válido

    def test_startup_cleanup_removes_partials(self):
        ref = self.ref("part")
        bin_p, _, _ = cache._paths(cache.key_for(ref))
        bin_p.parent.mkdir(parents=True, exist_ok=True)
        stale = bin_p.with_name(bin_p.stem + ".part.1.2"); stale.write_bytes(b"x")
        orphan = bin_p; orphan.write_bytes(b"sem-meta")
        cache.startup_cleanup()
        self.assertFalse(stale.exists()); self.assertFalse(orphan.exists())

    def test_size_limit_prunes_oldest(self):
        old_max, old_dir = cache.CACHE_MAX_MB, cache.CACHE_DIR
        try:
            cache.CACHE_DIR = Path(tempfile.mkdtemp())  # cache isolado
            cache.CACHE_MAX_MB = 0.00002  # ~20 bytes: cabe 1 arquivo de 11
            r1, r2 = self.ref("p1"), self.ref("p2")
            cache.fetch(r1, self.tmp / "1.mp4", self.downloader())
            p1 = cache._paths(cache.key_for(r1))[0]
            os.utime(p1, (1, 1))
            cache.fetch(r2, self.tmp / "2.mp4", self.downloader())
            self.assertFalse(p1.exists())
        finally:
            cache.CACHE_MAX_MB, cache.CACHE_DIR = old_max, old_dir

    def test_metrics_count_hits(self):
        metrics.begin({"id": "j"})
        ref = self.ref("m")
        cache.fetch(ref, self.tmp / "m1.mp4", self.downloader())
        cache.fetch(ref, self.tmp / "m2.mp4", self.downloader())
        m = metrics.snapshot(); metrics.end()
        self.assertEqual((m["cache_miss"], m["cache_hit"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
