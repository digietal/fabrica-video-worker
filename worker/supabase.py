from supabase import create_client, Client

from worker.config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY


supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
)
