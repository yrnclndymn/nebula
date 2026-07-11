"""Application settings, loaded from environment / .env."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Neo4j — local Docker defaults; override with Aura credentials in prod.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "nebula-local-dev"

    # CORS origin for the Vite dev server (set to the prod SPA origin in prod).
    frontend_origin: str = "http://localhost:5173"

    # Prefix for user routes. Empty locally; "/api" in prod so the same-origin
    # Firebase Hosting rewrite (/api/** → Cloud Run, path passed through) matches.
    # /health, /jobs/run, and /jobs/schedule-tick stay at the root (Cloud Run,
    # Cloud Tasks, and Cloud Scheduler hit them direct).
    api_prefix: str = ""

    # Auth: off locally, on in prod. When on, every route except /health requires a
    # Firebase ID token whose (verified) email is in allowed_emails; /jobs/run
    # requires a Cloud Tasks OIDC token from tasks_service_account.
    require_auth: bool = False
    allowed_emails: str = ""  # comma-separated

    # Gemini (google-genai reads GEMINI_API_KEY / GOOGLE_API_KEY from the env).
    # flash-lite tier: cheap, fast, and less demand-throttled than 2.5-flash.
    gemini_model: str = "gemini-3.1-flash-lite"
    # The enrichment agent does tool use + reasoning. gemini-2.5-flash is a fuller
    # model but its free tier is only 20 req/day; flash-lite has far more headroom
    # and handles this tool loop fine. Override with AGENT_MODEL.
    agent_model: str = "gemini-3.1-flash-lite"

    # Crawl cache: reuse a page/client snapshot if it's younger than this. Company
    # site copy changes slowly, so weeks is fine.
    cache_ttl_days: int = 21

    # Long jobs (propose / back-fill). "local" runs them inline (dev, single
    # long-lived process); "cloudtasks" enqueues to Cloud Tasks so they survive
    # Cloud Run scale-to-zero. The cloudtasks fields are only read in that mode.
    job_mode: str = "local"
    cloud_tasks_queue: str = "nebula-jobs"
    cloud_tasks_location: str = "europe-west2"
    gcp_project: str = ""
    service_url: str = ""  # this Cloud Run service's base URL (for the task target)
    tasks_service_account: str = ""  # SA whose OIDC token authorizes /jobs/run

    # Per-run budget caps, keyed by job type (see app/budget.py). Cost guardrails
    # for scheduled/ambient jobs, enforced in the tool layer — NOT by prompting.
    # Each dimension is a hard ceiling per run; a null (None) means uncapped. A
    # job type absent here runs unlimited; a job's payload can override its caps
    # (a "budget" dict merged on top). Override the whole map via the
    # JOB_BUDGETS env var as JSON.
    job_budgets: dict[str, dict[str, int | None]] = {
        "backfill": {
            "max_pages": 60,
            "max_searches": 0,
            "max_llm_calls": 40,
            "max_companies": 25,
        },
    }


settings = Settings()


def ensure_gemini_env() -> None:
    """Pin ADK/genai to the AI Studio API key path (not Vertex). ADK already reads
    GEMINI_API_KEY / GOOGLE_API_KEY from the env. Call before running an agent."""
    import os

    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
