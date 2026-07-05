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


settings = Settings()
