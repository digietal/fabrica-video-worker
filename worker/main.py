import os
import time

from worker.gateway import claim_next_job, fail_job, health
from worker.jobs import dispatch


WORKER_MODE = os.getenv("WORKER_MODE", "health").lower()
MAX_JOBS = int(os.getenv("MAX_JOBS", "1"))


def process_job(job: dict):
    job_type = job["type"]

    print(
        f"🎬 Processando job "
        f"{job['id']} | {job_type}"
    )

    dispatch(job)


def safe_fail(job: dict, error: Exception):
    print(f"❌ Falha {job.get('type')} {job.get('id')}: {error}")
    try:
        fail_job(job["id"], str(error) or error.__class__.__name__)
    except Exception as fail_error:
        print(f"⚠️ Não foi possível registrar /fail para {job.get('id')}: {fail_error}")


def main():
    print("🚀 Fábrica Video Worker iniciado")
    print(f"🔧 WORKER_MODE={WORKER_MODE}")
    print(f"🔢 MAX_JOBS={MAX_JOBS}")

    try:
        gateway_status = health()
        print(f"🌐 Gateway: {gateway_status}")
    except Exception as error:
        print(f"❌ Gateway indisponível: {error}")

        while True:
            time.sleep(60)

    if WORKER_MODE == "health":
        print(
            "🟢 Modo health: "
            "nenhum job será reivindicado."
        )

        while True:
            time.sleep(60)

    if WORKER_MODE == "once":
        processed = 0

        while processed < MAX_JOBS:
            job = claim_next_job()

            if not job:
                print(
                    "⏳ Nenhum job disponível."
                )
                break

            try:
                process_job(job)
                processed += 1

            except Exception as error:
                print(
                    f"❌ Erro no job "
                    f"{job['id']}: {error}"
                )

                safe_fail(job, error)

                processed += 1

        print(
            f"🛑 Limite atingido. "
            f"Jobs processados: {processed}"
        )

        while True:
            time.sleep(60)

    if WORKER_MODE == "loop":
        while True:
            job = claim_next_job()

            if not job:
                print(
                    "⏳ Nenhum job disponível. "
                    "Aguardando..."
                )
                time.sleep(5)
                continue

            try:
                process_job(job)

            except Exception as error:
                print(
                    f"❌ Erro no job "
                    f"{job['id']}: {error}"
                )

                safe_fail(job, error)

    raise RuntimeError(
        f"WORKER_MODE inválido: {WORKER_MODE}"
    )


if __name__ == "__main__":
    main()
