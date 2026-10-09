"""Ponto único onde o Worker lê entradas que dependem de artifacts de jobs anteriores.

O Gateway publicado hoje NÃO entrega a localização desses artifacts (só enriquece INGEST/NORMALIZE),
e o Worker não tem acesso ao banco. Enquanto isso, os handlers dependentes falham com erro controlado
(GatewayContractGap) em vez de fingir processamento. Ver docs/LACUNAS.md para o enriquecimento proposto.
"""


class GatewayContractGap(RuntimeError):
    pass


def require_input(job: dict, key: str, optional: bool = False) -> str | None:
    inputs = (job.get("payload") or {}).get("inputs") or {}
    value = inputs.get(key)
    if isinstance(value, dict):  # Gateway entrega {storage_path, file_name, mime_type, artifact_id, cache_key, kind}
        value = value.get("storage_path")
    if value or optional:
        return value or None
    raise GatewayContractGap(
        f"{job['type']} {job['id']}: o Gateway não informou payload.inputs.{key} "
        f"(storage_path do artifact de dependência). Etapa bloqueada até o /claim enriquecer este tipo de job."
    )


def require_list(job: dict, key: str) -> list:
    inputs = (job.get("payload") or {}).get("inputs") or {}
    value = inputs.get(key)
    if isinstance(value, list) and value:
        return value
    raise GatewayContractGap(
        f"{job['type']} {job['id']}: o Gateway não informou payload.inputs.{key}. Etapa bloqueada."
    )
