"""Application settings, loaded from environment / .env."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Neo4j — local Docker defaults; override with Aura credentials in prod.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "nebula-local-dev"

    # CORS origin for the Vite dev server.
    frontend_origin: str = "http://localhost:5173"

    # Gemini (google-genai reads GEMINI_API_KEY / GOOGLE_API_KEY from the env).
    # flash-lite tier: cheap, fast, and less demand-throttled than 2.5-flash.
    gemini_model: str = "gemini-3.1-flash-lite"
    # The enrichment agent does tool use + reasoning. gemini-2.5-flash is a fuller
    # model but its free tier is only 20 req/day; flash-lite has far more headroom
    # and handles this tool loop fine. Override with AGENT_MODEL.
    agent_model: str = "gemini-3.1-flash-lite"


settings = Settings()


def ensure_gemini_env() -> None:
    """Pin ADK/genai to the AI Studio API key path (not Vertex). ADK already reads
    GEMINI_API_KEY / GOOGLE_API_KEY from the env. Call before running an agent."""
    import os

    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
