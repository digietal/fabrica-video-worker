import requests

from worker.config import GATEWAY_URL, WORKER_SECRET_TOKEN


def request(method: str, path: str, payload=None, timeout=30):
    url = f"{GATEWAY_URL}/{path.lstrip('/')}"

    headers = {
        "Authorization": f"Bearer {WORKER_SECRET_TOKEN}",
        "Accept": "application/json",
    }

    if payload is not None:
        headers["Content-Type"] = "application/json"

    response = requests.request(
        method,
        url,
        json=payload,
        headers=headers,
        timeout=timeout,
    )

    try:
        data = response.json()
    except ValueError:
        data = {
            "error": response.text or "Resposta inválida do Gateway"
        }

    if not response.ok:
        raise RuntimeError(
            f"Gateway {method} {path} retornou HTTP "
            f"{response.status_code}: {data}"
        )

    return data


def health():
    return request("GET", "/health")


def claim_next_job():
    data = request("POST", "/claim")
    return data.get("job")


def complete_job(job_id: str, result: dict | None = None):
    return request(
        "POST",
        "/complete",
        {
            "job_id": job_id,
            "result": result or {},
        },
    )


def fail_job(job_id: str, error: str):
    return request(
        "POST",
        "/fail",
        {
            "job_id": job_id,
            "error": error[:2000],
        },
    )


def storage_url(action: str, storage_path: str):
    return request(
        "POST",
        "/storage-url",
        {
            "action": action,
            "storage_path": storage_path,
        },
    )
