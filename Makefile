# Nebula developer commands. Run from the repo root.

# --- Local infrastructure -----------------------------------------------------
db-up:            ## Start local Neo4j (requires Docker)
	docker compose up -d neo4j

db-down:          ## Stop local Neo4j
	docker compose down

# --- Backend (FastAPI + ADK agents) ------------------------------------------
install:          ## Install backend dependencies
	cd backend && uv sync

db-init:          ## Create Neo4j constraints + indexes (idempotent)
	cd backend && uv run python -m app.graph.schema

dev:              ## Run the API with reload on :8080
	cd backend && uv run uvicorn app.main:app --reload --port 8080

test:             ## Run backend tests
	cd backend && uv run pytest

lint:             ## Lint + format-check the backend
	cd backend && uv run ruff check . && uv run ruff format --check .

# --- Frontend (React + Vite) --------------------------------------------------
frontend-install: ## Install frontend dependencies
	cd frontend && npm install

frontend-dev:     ## Run the Vite dev server on :5173
	cd frontend && npm run dev

.PHONY: db-up db-down install db-init dev test lint frontend-install frontend-dev
