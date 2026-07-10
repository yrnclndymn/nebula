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

import:           ## Import a sheet CSV. Usage: make import CSV=data/x.csv TOPIC="SAP ecosystem"
	cd backend && uv run python -m app.importer.csv_import $(CSV) $(if $(TOPIC),--topic "$(TOPIC)",)

enrich:           ## Research one company via the ADK agent. NAME= WEBSITE= [TOPIC=]
	cd backend && uv run python -m app.agents.enrichment.enrich "$(NAME)" "$(WEBSITE)" $(if $(TOPIC),"$(TOPIC)",)

eval:             ## Evaluate the enrichment agent (LLM-as-Judge). ARGS=--grade-only|--limit=N
	cd backend && uv run python -m evals.run_eval $(ARGS)

chat:             ## Chat with the research assistant. ARGS="a question" for one-shot
	cd backend && uv run python -m app.agents.assistant.chat $(ARGS)

dev:              ## Run the API with reload on :8080 (PORT= to override for a parallel worktree)
	cd backend && uv run uvicorn app.main:app --reload --port $(if $(PORT),$(PORT),8080)

test:             ## Run backend tests
	cd backend && uv run pytest

lint:             ## Lint + format-check the backend
	cd backend && uv run ruff check . && uv run ruff format --check .

# --- Frontend (React + Vite) --------------------------------------------------
frontend-install: ## Install frontend dependencies
	cd frontend && npm install

frontend-dev:     ## Run the Vite dev server on :5173 (PORT= to override for a parallel worktree)
	cd frontend && npm run dev -- --port $(if $(PORT),$(PORT),5173)

# --- Parallel sessions --------------------------------------------------------
# Each Claude session should run in its OWN git worktree: a separate working
# directory sharing this repo's history, so concurrent sessions don't clobber
# each other's files or move each other's branch/HEAD. This target creates one.
worktree:         ## Isolated worktree + branch for a parallel session. NAME=<slug> [BASE=main]
	@test -n "$(NAME)" || { echo 'usage: make worktree NAME=<slug> [BASE=main]'; exit 1; }
	@set -e; dir="../nebula-$(NAME)"; base="$(if $(BASE),$(BASE),main)"; \
	test ! -e "$$dir" || { echo "error: $$dir already exists — remove it (git worktree remove $$dir) or pick another NAME"; exit 1; }; \
	if git show-ref --quiet "refs/heads/$(NAME)"; then echo "error: branch '$(NAME)' already exists — 'git branch -D $(NAME)' or pick another NAME"; exit 1; fi; \
	git fetch --quiet origin || true; \
	git -c branch.autoSetupMerge=false worktree add -b "$(NAME)" "$$dir" "origin/$$base" \
		|| { git worktree remove --force "$$dir" 2>/dev/null || true; \
		     git branch -D "$(NAME)" 2>/dev/null || true; exit 1; }; \
	[ -f .claude/settings.local.json ] && mkdir -p "$$dir/.claude" \
		&& cp .claude/settings.local.json "$$dir/.claude/" || true; \
	[ -f backend/.env ] && cp backend/.env "$$dir/backend/.env" || true; \
	echo "→ installing backend deps (uv sync)…"; (cd "$$dir/backend" && uv sync); \
	echo "→ installing frontend deps (npm install)…"; (cd "$$dir/frontend" && npm install); \
	echo ""; \
	echo "✓ worktree ready: $$dir  (branch '$(NAME)' off origin/$$base)"; \
	echo "  start a parallel session:  cd $$dir && claude"; \
	echo "  non-clashing dev ports:    make dev PORT=8081  |  make frontend-dev PORT=5174"; \
	echo "  remove when merged:        git worktree remove $$dir && git branch -d $(NAME)"

.PHONY: db-up db-down install db-init import enrich eval chat dev test lint frontend-install frontend-dev worktree
