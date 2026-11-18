"""Loop do Worker v11: concorrência controlada.

- Um único laço faz o /claim (atômico no banco: claim_next_job() com SKIP LOCKED + dependências).
  Só reivindica quando há vaga livre E memória suficiente; jobs nunca ficam reivindicados "na fila local".
- WORKER_CONCURRENCY = jobs simultâneos neste processo (padrão 2, teto WORKER_CONCURRENCY_MAX=4).
- HEAVY_CONCURRENCY = quantos jobs pesados (recorte RVM e REACT) rodam ao mesmo tempo (padrão 1).
- MAX_JOBS = total de jobs a reivindicar em WORKER_MODE=once (NÃO é concorrência).
- Falha de um job nunca interrompe os demais; SIGTERM: para de reivindicar e espera os ativos.
"""
import os
import random
import signal
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from worker import cache, metrics
from worker.config import FFMPEG_THREADS
from worker.gateway import GatewayError, GatewayUnavailable, claim_next_job, fail_job, health
from worker.jobs import dispatch

WORKER_MODE = os.getenv("WORKER_MODE", "health").lower()
MAX_JOBS = int(os.getenv("MAX_JOBS", "1"))
IDLE_SLEEP = float(os.getenv("IDLE_SLEEP_S", "5"))
OUTAGE_MAX_SLEEP = 120.0
CONCURRENCY_MAX = int(os.getenv("WORKER_CONCURRENCY_MAX", "4"))
CONCURRENCY = max(1, min(CONCURRENCY_MAX, int(os.getenv("WORKER_CONCURRENCY", "2"))))
HEAVY_CONCURRENCY = max(1, min(CONCURRENCY, int(os.getenv("HEAVY_CONCURRENCY", "1"))))
# Só reivindica um novo job se sobrar pelo menos isso de memória no contêiner (MB).
MIN_FREE_MEM_MB = float(os.getenv("MIN_FREE_MEM_MB", "280"))
# v11: jobs pesados (RVM/REACT) só começam com esta memória livre (o tipo só é conhecido após o claim).
HEAVY_MIN_FREE_MEM_MB = float(os.getenv("HEAVY_MIN_FREE_MEM_MB", "450"))
HEAVY_WAIT_RELEASE = os.getenv("HEAVY_WAIT_RELEASE", "0").strip() == "1"  # v16, padrão desligado
# v17: variação aleatória da espera ociosa para vários Workers não consultarem a fila em sincronia.
IDLE_JITTER = float(os.getenv("IDLE_JITTER", "0.3"))
HEARTBEAT_S = float(os.getenv("HEARTBEAT_S", "60"))
HEAVY_TYPES = {"ASSET_MATTE", "BACKGROUND_REMOVE", "RENDER_REACT"}


class Runner:
    def __init__(self, dispatch_fn=dispatch, fail_fn=fail_job, concurrency=CONCURRENCY, heavy=HEAVY_CONCURRENCY):
        self.dispatch, self.fail = dispatch_fn, fail_fn
        self.concurrency = concurrency
        self.slots = threading.BoundedSemaphore(concurrency)
        self.heavy = threading.BoundedSemaphore(heavy)
        # +1 thread: um job pesado esperando (HEAVY_WAIT_RELEASE) não pode bloquear o job leve que entrou na vaga.
        self.pool = ThreadPoolExecutor(max_workers=concurrency + 1, thread_name_prefix="job")
        self.heavy_waiting = 0
        self.active = 0
        self._lock = threading.Lock()
        self.active_ids: set[str] = set()

    def _safe_fail(self, job, error):
        try:
            self.fail(job["id"], str(error) or error.__class__.__name__)
        except Exception as fail_error:
            # Job fica PROCESSING; o lease (60 min) em claim_next_job() o recoloca na fila.
            print(f"⚠️ /fail não registrado para {job.get('id')}: {fail_error}", flush=True)

    def run_one(self, job: dict):
        """Executa um job isolando falhas: nunca derruba o processo nem os outros jobs."""
        heavy = job.get("type") in HEAVY_TYPES
        t_claim = time.monotonic()
        if heavy:
            # v16 (HEAVY_WAIT_RELEASE=1): se a vaga pesada está ocupada, devolve a vaga geral enquanto
            # espera, para jobs leves executáveis não ficarem parados. No máx. 1 job pesado esperando.
            released = False
            if HEAVY_WAIT_RELEASE and not self.heavy.acquire(blocking=False):
                with self._lock:
                    can_wait = self.heavy_waiting == 0
                    if can_wait:
                        self.heavy_waiting += 1
                if can_wait:
                    self.slots.release()
                    released = True
                    self.heavy.acquire()
                    self.slots.acquire()
                    with self._lock:
                        self.heavy_waiting -= 1
                else:
                    self.heavy.acquire()
            elif not HEAVY_WAIT_RELEASE:
                self.heavy.acquire()
            if released:
                metrics.log("heavy_slot_acquired", job_id=job.get("id"), released_slot_while_waiting=True)
            # Espera memória para o job pesado (máx. 10 min; depois roda mesmo assim — o lease protege).
            deadline = time.monotonic() + 600
            while not memory_ok(HEAVY_MIN_FREE_MEM_MB) and time.monotonic() < deadline:
                time.sleep(2.0)
        t0, start = time.monotonic(), time.time()
        metrics.begin(job)
        status, err = "COMPLETED", None
        try:
            self.dispatch(job)
        except GatewayUnavailable as e:
            # Falha de COMUNICAÇÃO: não marcar FAILED (resultado pode estar salvo); o lease recupera.
            status, err = "COMM_FAILED", str(e)
        except Exception as e:  # falha real de processamento ou erro permanente
            traceback.print_exc()
            status, err = "FAILED", str(e) or e.__class__.__name__
            self._safe_fail(job, e)
        finally:
            m = metrics.snapshot()
            metrics.end()
            if heavy:
                self.heavy.release()
            with self._lock:
                self.active -= 1
                self.active_ids.discard(job.get("id"))
                active = self.active
            metrics.log("job_end", run_id=job.get("production_run_id"), job_id=job.get("id"), type=job.get("type"),
                        status=status, attempts=job.get("attempts"), started_at=start, ended_at=time.time(),
                        duration_s=round(time.monotonic() - t0, 2), heavy_wait_s=round(t0 - t_claim, 2),
                        active_jobs=active, error=(err or "")[:300] or None, **m, **metrics.memory_mb(),
                        **metrics.cpu())
            self.slots.release()

    def submit(self, job: dict):
        """Chamar somente após acquire_slot()."""
        with self._lock:
            if job.get("id") in self.active_ids:
                # Defesa extra: o banco já impede, mas nunca rodar o mesmo job duas vezes neste processo.
                self.slots.release()
                print(f"⚠️ Job {job.get('id')} já está ativo neste Worker; ignorado", flush=True)
                return
            self.active += 1
            self.active_ids.add(job.get("id"))
            active = self.active
        metrics.log("job_start", run_id=job.get("production_run_id"), job_id=job.get("id"), type=job.get("type"),
                    attempts=job.get("attempts"), active_jobs=active, queue_wait_s=metrics.queue_wait_s(job))
        self.pool.submit(self.run_one, job)

    def acquire_slot(self, timeout=None) -> bool:
        return self.slots.acquire(timeout=timeout)

    def shutdown(self):
        self.pool.shutdown(wait=True)


def memory_ok(min_free=None) -> bool:
    free = metrics.memory_mb().get("mem_free_mb")
    return free is None or free >= (MIN_FREE_MEM_MB if min_free is None else min_free)


def claim_with_backoff(state: dict):
    """Claim protegido: indisponibilidade do Gateway gera espera controlada, sem crash."""
    try:
        job = claim_next_job()
        state["outage"] = 0
        return job
    except GatewayUnavailable as e:
        state["outage"] += 1
        wait = min(OUTAGE_MAX_SLEEP, 10 * 2 ** (state["outage"] - 1))
        print(f"🌐 Gateway indisponível no claim ({e}); aguardando {wait:.0f}s", flush=True)
        time.sleep(wait)
    except GatewayError as e:
        if e.status in (401, 403):
            print("🔒 Gateway recusou o token do Worker (401/403). Verifique WORKER_SECRET_TOKEN.", flush=True)
            time.sleep(300)
        else:
            print(f"⚠️ Erro permanente no claim: {e}", flush=True)
            time.sleep(30)
    return None


def run_loop(runner: Runner, claim_fn, mode: str, max_jobs: int, stop: threading.Event,
             idle_sleep=IDLE_SLEEP, mem_ok=memory_ok):
    """Reivindica um job por vaga livre. Retorna o número de jobs reivindicados."""
    state = {"outage": 0}
    claimed = 0
    low_mem_logged = False
    idle_polls = 0
    last_hb = time.monotonic()
    while not stop.is_set():
        if HEARTBEAT_S > 0 and time.monotonic() - last_hb >= HEARTBEAT_S:
            # v17: estado do Worker para medir saturação/ociosidade da frota.
            metrics.log("worker_heartbeat", active_jobs=runner.active, capacity=runner.concurrency,
                        heavy_waiting=runner.heavy_waiting, idle_polls=idle_polls, claimed=claimed,
                        **metrics.memory_mb(), **metrics.cpu())
            last_hb, idle_polls = time.monotonic(), 0
        if mode == "once" and claimed >= max_jobs:
            print(f"🛑 Limite MAX_JOBS={max_jobs} atingido. Jobs reivindicados: {claimed}", flush=True)
            break
        if not runner.acquire_slot(timeout=1.0):
            continue
        if stop.is_set():  # sinal chegou enquanto esperava vaga: não reivindicar mais nada
            runner.slots.release()
            break
        if not mem_ok():
            runner.slots.release()
            if not low_mem_logged:
                metrics.log("claim_paused_low_memory", min_free_mb=MIN_FREE_MEM_MB, active_jobs=runner.active,
                            **metrics.memory_mb())
                low_mem_logged = True
            stop.wait(2.0)
            continue
        low_mem_logged = False
        job = claim_fn(state)
        if not job:
            runner.slots.release()
            if state["outage"] == 0:
                if mode == "once" and runner.active == 0:
                    print(f"⏳ Nenhum job disponível. Reivindicados: {claimed}", flush=True)
                    break
                idle_polls += 1
                stop.wait(jittered(idle_sleep))
            continue
        claimed += 1
        runner.submit(job)
    return claimed


def jittered(base: float, jitter: float | None = None) -> float:
    j = IDLE_JITTER if jitter is None else jitter
    return max(0.0, base * (1 + random.uniform(-j, j)))


def idle_forever():
    while True:
        time.sleep(60)


def main():
    print(f"🚀 Fábrica Video Worker v18 | WORKER_ID={metrics.WORKER_ID} | WORKER_MODE={WORKER_MODE} | MAX_JOBS={MAX_JOBS} | "
          f"WORKER_CONCURRENCY={CONCURRENCY} | HEAVY_CONCURRENCY={HEAVY_CONCURRENCY} | "
          f"FFMPEG_THREADS={FFMPEG_THREADS} | MIN_FREE_MEM_MB={MIN_FREE_MEM_MB:.0f} | HEAVY_MIN_FREE_MEM_MB={HEAVY_MIN_FREE_MEM_MB:.0f} | "
          f"CACHE={'on' if cache.CACHE_ENABLED else 'off'} {cache.CACHE_DIR} max={cache.CACHE_MAX_MB:.0f}MB",
          flush=True)
    metrics.log("worker_start", **metrics.memory_mb(), **metrics.cpu())
    while True:
        try:
            print(f"🌐 Gateway: {health()}", flush=True)
            break
        except Exception as e:
            print(f"❌ Gateway indisponível no início: {e}; nova tentativa em 30s", flush=True)
            time.sleep(30)

    if WORKER_MODE == "health":
        print("🟢 Modo health: nenhum job será reivindicado.", flush=True)
        idle_forever()
    if WORKER_MODE not in ("once", "loop"):
        raise RuntimeError(f"WORKER_MODE inválido: {WORKER_MODE}")

    cache.startup_cleanup()
    stop = threading.Event()

    def _term(signum, _frame):
        print(f"🛑 Sinal {signum}: parando de reivindicar; aguardando jobs ativos", flush=True)
        stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    runner = Runner()
    claimed = run_loop(runner, claim_with_backoff, WORKER_MODE, MAX_JOBS, stop)
    runner.shutdown()
    print(f"🏁 Jobs reivindicados: {claimed}", flush=True)
    if not stop.is_set():
        idle_forever()


if __name__ == "__main__":
    main()
