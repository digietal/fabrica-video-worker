import os
import time

from worker.gateway import claim_next_job, fail_job, health
from worker.ingest import process_ingest


WORKER_MODE = os.getenv("WORKER_MODE", "health").lower()
MAX_JOBS = int(os.getenv("MAX_JOBS", "1"))


def process_job(job: dict):
    job_type = job["type"]

    print(
        f"🎬 Processando job "
        f"{job['id']} | {job_type}"
    )

    if job_type == "INGEST":
        process_ingest(job)
        return

    raise RuntimeError(
        f"Tipo de job ainda não implementado: {job_type}"
    )


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

                fail_job(
                    job["id"],
                    str(error),
                )

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

                fail_job(
                    job["id"],
                    str(error),
                )

    raise RuntimeError(
        f"WORKER_MODE inválido: {WORKER_MODE}"
    )


if __name__ == "__main__":
    main()
