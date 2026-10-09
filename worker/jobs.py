from worker.supabase import supabase


def claim_next_job():
    """
    Pega o próximo job QUEUED de forma segura.

    A função SQL/RPC no Supabase será responsável por:
    - selecionar o job mais antigo;
    - bloquear a linha;
    - mudar o status para PROCESSING;
    - incrementar attempts;
    - devolver o job.
    """

    response = supabase.rpc("claim_next_job").execute()

    if not response.data:
        return None

    if isinstance(response.data, list):
        return response.data[0] if response.data else None

    return response.data
