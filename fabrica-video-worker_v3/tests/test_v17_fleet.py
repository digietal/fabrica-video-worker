"""v17: vários Workers independentes disputando a mesma fila.

FakeDB reproduz a semântica de claim_next_job() (migrations 0002/0010): lock exclusivo (SKIP LOCKED),
só QUEUED elegíveis (dependências COMPLETED), attempts+1, lease expirado → QUEUED (<3) ou FAILED (>=3),
e /complete e /fail idempotentes. Cada Worker é um Runner real de worker.main com seu próprio pool.
"""
import os
import threading
import time
import unittest

os.environ.setdefault("WORKER_SECRET_TOKEN", "test")

from worker import main, metrics  # noqa: E402


class FakeDB:
    def __init__(self, lease_s=60.0):
        self.lock = threading.Lock()
        self.jobs, self.order = {}, []
        self.lease_s = lease_s
        self.executions = {}  # job_id -> nº de execuções reais
        self.claimed_by = {}

    def add(self, jid, deps=(), typ="RENDER_CLEAN"):
        self.jobs[jid] = {"id": jid, "type": typ, "status": "QUEUED", "attempts": 0, "deps": list(deps),
                          "updated": time.monotonic(), "production_run_id": "run1",
                          "created_at": "2026-10-15T00:00:00+00:00"}
        self.order.append(jid)

    def claim(self, worker):
        with self.lock:  # equivale a FOR UPDATE SKIP LOCKED + UPDATE atômico
            now = time.monotonic()
            for j in self.jobs.values():
                if j["status"] == "PROCESSING" and now - j["updated"] > self.lease_s:
                    j["status"] = "QUEUED" if j["attempts"] < 3 else "FAILED"
            for jid in self.order:
                j = self.jobs[jid]
                if j["status"] == "QUEUED" and all(self.jobs[d]["status"] == "COMPLETED" for d in j["deps"]):
                    j.update(status="PROCESSING", attempts=j["attempts"] + 1, updated=now)
                    self.claimed_by.setdefault(jid, []).append(worker)
                    return dict(j)
            return None

    def complete(self, jid):
        with self.lock:
            j = self.jobs[jid]
            if j["status"] == "COMPLETED":
                return 200  # idempotente
            for d in j["deps"]:
                assert self.jobs[d]["status"] == "COMPLETED", f"{jid} completou antes da dependência {d}"
            j["status"] = "COMPLETED"
            return 200

    def fail(self, jid, _err):
        with self.lock:
            if self.jobs[jid]["status"] != "COMPLETED":
                self.jobs[jid]["status"] = "FAILED"

    def count(self, st):
        return sum(1 for j in self.jobs.values() if j["status"] == st)


def run_fleet(db, n_workers, work_fn, concurrency=2, timeout=20):
    stop = threading.Event()
    runners, threads = [], []

    def worker_loop(name):
        def dispatch(job):
            with db.lock:
                db.executions[job["id"]] = db.executions.get(job["id"], 0) + 1
            work_fn(name, job)
            db.complete(job["id"])
        r = main.Runner(dispatch_fn=dispatch, fail_fn=db.fail, concurrency=concurrency, heavy=1)
        runners.append(r)
        main.run_loop(r, lambda _s: db.claim(name), "loop", 0, stop, idle_sleep=0.01, mem_ok=lambda: True)
        r.shutdown()

    for i in range(n_workers):
        t = threading.Thread(target=worker_loop, args=(f"w{i}",), daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db.lock:
            pending = sum(1 for j in db.jobs.values() if j["status"] in ("QUEUED", "PROCESSING"))
        if pending == 0:
            break
        time.sleep(0.02)
    stop.set()
    for t in threads:
        t.join(5)
    return runners


class FleetTests(unittest.TestCase):
    def test_no_duplicate_processing_across_workers(self):
        db = FakeDB()
        for i in range(200):
            db.add(f"j{i}")
        run_fleet(db, 6, lambda w, j: time.sleep(0.002))
        self.assertEqual(db.count("COMPLETED"), 200)
        self.assertTrue(all(v == 1 for v in db.executions.values()), "job executado mais de uma vez")
        self.assertGreater(len({w for ws in db.claimed_by.values() for w in ws}), 1, "frota não dividiu a fila")

    def test_dependencies_respected_with_many_workers(self):
        db = FakeDB()
        done_at = {}
        for u in range(20):
            db.add(f"mix{u}", typ="MIX")
            db.add(f"trim{u}", [f"mix{u}"], "TRIM")
            for f in ("CLEAN", "SPLIT", "REACT"):
                db.add(f"{f}{u}", [f"trim{u}"], f"RENDER_{f}")

        def work(_w, job):
            for d in db.jobs[job["id"]]["deps"]:
                self.assertIn(d, done_at, f"{job['id']} começou antes de {d}")
            time.sleep(0.003)
            done_at[job["id"]] = time.monotonic()
        run_fleet(db, 5, work)
        self.assertEqual(db.count("COMPLETED"), 100)

    def test_error_isolation_between_workers(self):
        db = FakeDB()
        for i in range(60):
            db.add(f"j{i}")

        def work(w, job):
            if job["id"] in ("j5", "j17"):
                raise RuntimeError("ffmpeg quebrou")
        run_fleet(db, 4, work)
        self.assertEqual(db.count("FAILED"), 2)
        self.assertEqual(db.count("COMPLETED"), 58)

    def test_abandoned_job_recovered_by_other_worker(self):
        db = FakeDB(lease_s=0.2)
        db.add("lost")
        self.assertIsNotNone(db.claim("crashed"))  # Worker morre sem /complete nem /fail
        time.sleep(0.25)
        run_fleet(db, 2, lambda w, j: None)
        self.assertEqual(db.jobs["lost"]["status"], "COMPLETED")
        self.assertEqual(db.jobs["lost"]["attempts"], 2)
        self.assertEqual(db.claimed_by["lost"][0], "crashed")
        self.assertNotEqual(db.claimed_by["lost"][1], "crashed")

    def test_lease_gives_up_after_three_attempts(self):
        db = FakeDB(lease_s=0.05)
        db.add("poison")
        for _ in range(3):
            self.assertIsNotNone(db.claim("x"))
            time.sleep(0.07)
        self.assertIsNone(db.claim("y"))
        self.assertEqual(db.jobs["poison"]["status"], "FAILED")

    def test_complete_and_fail_idempotent(self):
        db = FakeDB()
        db.add("a")
        db.claim("w")
        self.assertEqual(db.complete("a"), 200)
        self.assertEqual(db.complete("a"), 200)  # resposta perdida + reenvio
        db.fail("a", "tarde demais")
        self.assertEqual(db.jobs["a"]["status"], "COMPLETED")

    def test_concurrent_claims_never_share_a_job(self):
        db = FakeDB()
        for i in range(500):
            db.add(f"j{i}")
        got, lock = [], threading.Lock()

        def hammer(name):
            while True:
                j = db.claim(name)
                if not j:
                    return
                with lock:
                    got.append(j["id"])
        ts = [threading.Thread(target=hammer, args=(f"w{i}",)) for i in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(got), 500)
        self.assertEqual(len(set(got)), 500)


class ObservabilityTests(unittest.TestCase):
    def test_worker_id_in_every_log(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            metrics.log("x", a=1)
        self.assertIn(f'"worker_id": "{metrics.WORKER_ID}"', buf.getvalue())

    def test_queue_wait(self):
        self.assertEqual(metrics.queue_wait_s({"created_at": "1970-01-01T00:00:10Z"}, now=40.0), 30.0)
        self.assertIsNone(metrics.queue_wait_s({}))
        self.assertIsNone(metrics.queue_wait_s({"created_at": "lixo"}))

    def test_jitter_bounds(self):
        for _ in range(200):
            v = main.jittered(5.0, 0.3)
            self.assertTrue(3.5 <= v <= 6.5)
        self.assertEqual(main.jittered(5.0, 0.0), 5.0)


if __name__ == "__main__":
    unittest.main()
