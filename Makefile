# Nebula developer commands. Run from the repo root.

# --- Local infrastructure -----------------------------------------------------
db-up:            ## Start local Neo4j (requires Docker)
	docker compose up -d neo4j

db-ephemeral:     ## Throwaway Neo4j for THIS worktree (needs Docker) — prints the NEO4J_URI to export
	@docker info >/dev/null 2>&1 || { echo "error: docker daemon not running — graph tests will skip; CI is the arbiter"; exit 1; }
	@set -e; name="nebula-eph-$$(pwd | cksum | cut -d' ' -f1)"; \
	docker rm -f "$$name" >/dev/null 2>&1 || true; \
	docker run -d --rm --name "$$name" -e NEO4J_AUTH=neo4j/nebula-local-dev -p 127.0.0.1:0:7687 neo4j:5 >/dev/null; \
	port=$$(docker port "$$name" 7687/tcp | head -1 | awk -F: '{print $$NF}'); \
	printf 'waiting for bolt on :%s ' "$$port"; \
	for i in $$(seq 1 45); do \
	  docker exec "$$name" cypher-shell -u neo4j -p nebula-local-dev "RETURN 1" >/dev/null 2>&1 && break; \
	  printf '.'; sleep 2; \
	done; echo " up"; \
	echo "export NEO4J_URI=bolt://localhost:$$port"; \
	echo "run:  NEO4J_URI=bolt://localhost:$$port make test"; \
	echo "stop: docker rm -f $$name"

db-down:          ## Stop local Neo4j
	docker compose down

# --- Backend (FastAPI + ADK agents) ------------------------------------------
install:          ## Install backend dependencies
	cd backend && uv sync

db-init:          ## Create Neo4j constraints + indexes (idempotent)
	cd backend && uv run python -m app.graph.schema

import:           ## Import a sheet CSV. Usage: make import CSV=data/x.csv TOPIC="SAP ecosystem"
	cd backend && uv run python -m app.importer.csv_import $(CSV) $(if $(TOPIC),--topic "$(TOPIC)",)

normalize-linkedin: ## Canonicalise stored LinkedIn URLs (uk.->www., trailing slash). ARGS=--dry-run
	cd backend && uv run python scripts/normalize_linkedin.py $(ARGS)

migrate-person-identity: ## Re-key Person on LinkedIn URL (dry-run by default). ARGS=--commit to apply
	cd backend && uv run python -m app.graph.person_identity $(ARGS)

discover-leader-linkedin: ## Discover leaders' LinkedIn profiles (reviewable dry-run). ARGS="--commit --limit N --company NAME"
	cd backend && uv run python -m app.graph.person_discovery $(ARGS)

repair-mojibake:  ## Repair UTF-8-as-Latin-1 mojibake in stored Signal titles/summaries (dry-run). ARGS=--commit
	cd backend && uv run python -m app.graph.repair_mojibake $(ARGS)

enrich:           ## Research one company via the ADK agent. NAME= WEBSITE= [TOPIC=]
	cd backend && uv run python -m app.agents.enrichment.enrich "$(NAME)" "$(WEBSITE)" $(if $(TOPIC),"$(TOPIC)",)

eval:             ## Evaluate the enrichment agent (LLM-as-Judge). ARGS=--grade-only|--limit=N
	cd backend && uv run python -m evals.run_eval $(ARGS)

chat:             ## Chat with the research assistant. ARGS="a question" for one-shot
	cd backend && uv run python -m app.agents.assistant.chat $(ARGS)

dev:              ## Run the API with reload on :8080 (PORT= to override for a parallel worktree)
	cd backend && uv run uvicorn app.main:app --reload --port $(if $(PORT),$(PORT),8080)

schedule-tick:    ## Fire the scheduler tick locally (Cloud Scheduler's job in prod). PORT= to match make dev
	curl -fsS -X POST http://localhost:$(if $(PORT),$(PORT),8080)/jobs/schedule-tick && echo

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

.PHONY: db-up db-ephemeral db-down install db-init import normalize-linkedin migrate-person-identity discover-leader-linkedin repair-mojibake enrich eval chat dev schedule-tick test lint frontend-install frontend-dev worktree

# --- Wave sidecar -------------------------------------------------------------
# Live progress view of a running wave (#107). wave_status.py snapshots every
# feat/* branch (worktrees + PRs) into scripts/wave-status.json; wave_status.html
# polls it. See scripts/wave_status.py for the JSON schema + a two-command run.
wave-status:      ## One wave snapshot -> scripts/wave-status.json
	python3 scripts/wave_status.py

wave-watch:       ## Snapshot the wave every 15s (Ctrl-C to stop). Serve with: (cd scripts && python3 -m http.server)
	@echo "snapshotting wave every 15s → scripts/wave-status.json  (Ctrl-C to stop)"
	@echo "view: run 'cd scripts && python3 -m http.server' then open wave_status.html"
	@while true; do python3 scripts/wave_status.py || true; sleep 15; done

.PHONY: wave-status wave-watch
