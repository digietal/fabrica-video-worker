import time

from worker.jobs import claim_next_job
from worker.ingest import process_ingest
from worker.supabase import supabase


def mark_completed(job_id: str):
    (
        supabase
        .table("jobs")
        .update({
            "status": "COMPLETED",
            "error": None,
        })
        .eq("id", job_id)
        .execute()
    )


def mark_failed(job_id: str, error: str):
    (
        supabase
        .table("jobs")
        .update({
            "status": "FAILED",
            "error": error,
        })
        .eq("id", job_id)
        .execute()
    )


def process_job(job: dict):
    job_type = job["type"]

    print(
        f"🎬 Processando job "
        f"{job['id']} | {job_type}"
    )

    if job_type == "INGEST":
        process_ingest(job)
        mark_completed(job["id"])
        return

    raise RuntimeError(
        f"Tipo de job ainda não implementado: {job_type}"
    )


def main():
    print("🚀 Fábrica Video Worker iniciado")

    while True:
        job = claim_next_job()

        if not job:
            print("⏳ Nenhum job disponível. Aguardando...")
            time.sleep(5)
            continue

        try:
            process_job(job)

        except Exception as error:
            print(
                f"❌ Erro no job {job['id']}: {error}"
            )

            mark_failed(
                job["id"],
                str(error),
            )


if __name__ == "__main__":
    main()
