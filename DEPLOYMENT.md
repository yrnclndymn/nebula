# Deploying Nebula

Target: the existing **emergent-strategies** Firebase/GCP project. Private, near
$0/month idle, keys never leave the server.

## Decisions (locked)

- **Frontend** → Firebase Hosting at **`nebula.emergentstrategies.tech`**.
- **Backend** → Cloud Run, **scale-to-zero** (`min-instances=0`).
- **Graph** → **Neo4j Aura Free** (auto-pauses after ~3 days idle; resumes on next
  connection).
- **Auth** → Firebase Auth (Google), restricted to an email allowlist; enforced on
  the SPA *and* every API call.
- **Long jobs** (propose / back-fill) → **Cloud Tasks**, with job state in the graph
  (required because scale-to-zero kills in-process background tasks).
- **Secrets** → Google Secret Manager, injected into Cloud Run at runtime.
- **MCP** → stays a **local stdio** server pointed at prod Aura.

## Architecture

```
 Browser ──HTTPS──► Firebase Hosting (nebula.emergentstrategies.tech)
                      │  static SPA (Vite build)
                      │  rewrite  /api/**  ──► Cloud Run  nebula-api  (scale-to-zero)
                      │                           │  FastAPI + ADK agents
 Firebase Auth ◄──────┘ (Google sign-in,          │  verifies Firebase ID token
   ID token on every /api call)                   │  reads secrets from Secret Manager
                                                   ├─► Neo4j Aura  (neo4j+s://)
                                                   ├─► Gemini API  (key from Secret Mgr)
                                                   └─► Cloud Tasks ─┐ enqueue long jobs
                                                        │           │
                                                        ▼ (OIDC)    │
                                                   Cloud Run /jobs/run/{id}  ◄┘
                                                        (does research w/ CPU,
                                                         writes results to graph)

 Your laptop:  MCP stdio server ──► Aura (prod)   [personal, not exposed]
```

Same-origin (`/api/**` rewrite) means **no CORS** and one domain. Cloud Run is also
reachable at its `*.run.app` URL, but every route requires a valid token, so that's
fine.

## Privacy & auth

**Web app (two layers):**
1. SPA gates on **Firebase Google sign-in** (Firebase JS SDK) and sends the ID token
   as `Authorization: Bearer <token>` on every `/api` call. Point `API_BASE` at
   `/api` (same origin).
2. Backend FastAPI dependency verifies the token with `firebase-admin`
   (`auth.verify_id_token`) and checks the email against `ALLOWED_EMAILS`. Applies to
   all routes **except** `/health`. Invalid/absent → 401.

**Cloud Tasks callbacks** (`/jobs/run/*`) are called by Cloud Tasks, not the user, so
they use a **different** check: Cloud Tasks attaches an **OIDC token** (a dedicated
service account); the endpoint verifies the token's issuer/audience instead of a
Firebase token. So the auth dependency branches: `/jobs/*` → OIDC from the tasks SA;
everything else → Firebase user token.

**MCP:** local stdio process under your account, not network-exposed. "Auth" = it
runs on your machine with the Aura creds in a local `.env`. (Remote+OAuth is a future
option, not needed now.)

## Secrets — keys stay server-side

- Store in **Secret Manager**: `GEMINI_API_KEY`, `NEO4J_URI`, `NEO4J_USER`,
  `NEO4J_PASSWORD` (later `ANTHROPIC_API_KEY`, etc.).
- Cloud Run reads them via `--set-secrets` → they arrive as env vars; pydantic
  `Settings` already reads env. Never in the repo, the image, or the frontend.
- The **SPA only** carries the Firebase *web* config (apiKey etc.), which is **not
  secret** by design — auth is enforced by the token check. LLM/Neo4j keys never
  reach the browser.
- Cloud Run runs as a least-privilege **service account** (Secret Manager accessor +
  Cloud Tasks enqueuer). `.gitignore` already excludes `.env` and `*.csv`.

## Scale-to-zero + durable jobs (the one real refactor)

Scale-to-zero throttles CPU after the response and kills idle instances, so the
current "background `asyncio` task + in-memory `PROPOSALS`/`BACKFILLS`" design won't
survive. Reads / filters / chat / short ops are unaffected. The long jobs change to:

1. **State in the graph:** `(:Proposal {id,status,record,...})` and
   `(:BackfillJob {id,status,field,...})-[:HAS_ROW]->(:BackfillRow {...})`. Pollable
   from any instance, survives cold starts. (Replaces the module-level dicts in
   `proposals.py` / `backfill.py`.)
2. **Work runs in a Cloud Tasks-triggered request:**
   - `start` endpoint: create the job node (`pending`) + **enqueue a Cloud Task**
     targeting `/jobs/run/{id}`; return immediately.
   - `/jobs/run/{id}`: Cloud Tasks invokes it → CPU is allocated for that request
     (timeout up to 60 min) → does the research → writes rows/results to the graph.
   - Client polls `GET /proposals/{id}` / `GET /backfill/{id}` (reads the graph) —
     **UX unchanged** (spinner + progressive rows). Commit endpoints unchanged.
3. **Chat sessions** stay in-memory and ephemeral: a cold start resets *short-term*
   context; **long-term memory (`:Memory`) persists** in the graph. No refactor.

Cloud Tasks is free at this scale and keeps `min-instances=0`.

## Switchable LLM

- **Now:** Gemini model IDs are env vars (`GEMINI_MODEL`, `AGENT_MODEL`) — switch
  models without redeploying. Provider key in Secret Manager.
- **Now (Phase E, #8 — done):** provider-switching via **LiteLLM**. `app/llm.py` is
  the single seam: ADK agents call `llm.adk_model()` (a plain Gemini model string by
  default, `LiteLlm(model=…)` for any other provider), and the direct `google-genai`
  structured calls (extract / judge / tidy / field_extract / logos) go through
  `llm.generate()` (native genai for gemini, `litellm.acompletion` otherwise).
  Switch with two env vars, no code edits:
  - `LLM_PROVIDER` — `gemini` (default; unchanged native path) or a LiteLLM provider
    family, e.g. `anthropic`, `openai`, `azure`.
  - `LLM_MODEL` — pass-through model id used verbatim (e.g. `anthropic/claude-…`,
    `gpt-…`); REQUIRED for any non-gemini provider (rejected at startup otherwise);
    with both unset the `GEMINI_MODEL` / `AGENT_MODEL` defaults apply.
  The chosen provider's API key must be in the env — **each key wired into Secret
  Manager at deploy time** (ops task; not done here): `GEMINI_API_KEY` /
  `GOOGLE_API_KEY` for gemini, `ANTHROPIC_API_KEY` for anthropic, `OPENAI_API_KEY` for
  openai, etc. On a non-gemini provider no Gemini key is needed: the importer and
  tidy paths construct their `genai.Client` only on the gemini branch. Caveats: the
  litellm structured-output path (JSON-schema `response_format`) is implemented but
  unverified without a live non-gemini key; logo vision (multimodal) is gemini-only —
  on other providers `identify_logos` skips the vision batch with a warning (alt-text
  and text-mined client names still flow).

## Phased rollout

### Phase A — backend on Cloud Run + Aura + secrets
1. **Aura:** create a Free instance; note `neo4j+s://…`, user, password.
2. **Migrate data (dump/load — keeps the research we've already run):**
   - Dump the local Docker DB:
     `docker exec nebula-neo4j neo4j-admin database dump neo4j --to-path=/data` then
     `docker cp nebula-neo4j:/data/neo4j.dump ./neo4j.dump`.
   - Push to Aura: `neo4j-admin database upload neo4j --from-path=. --to-uri=<aura>`
     (or the Aura console's "Import database" → upload the `.dump`). One-time; re-run
     if you want to refresh prod from local.
3. **Secrets:**
   `printf '%s' "$KEY" | gcloud secrets create GEMINI_API_KEY --data-file=-` (repeat
   for NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD).
4. **Dockerfile** for `backend/` (uv base image → `uvicorn app.main:app`).
5. **Deploy:**
   ```
   gcloud run deploy nebula-api --source backend --region europe-west2 \
     --min-instances=0 --max-instances=2 \
     --set-secrets=GEMINI_API_KEY=GEMINI_API_KEY:latest,NEO4J_URI=NEO4J_URI:latest,\
NEO4J_USER=NEO4J_USER:latest,NEO4J_PASSWORD=NEO4J_PASSWORD:latest \
     --set-env-vars=GEMINI_MODEL=gemini-3.1-flash-lite,AGENT_MODEL=gemini-3.1-flash-lite \
     --service-account=nebula-run@emergent-strategies.iam.gserviceaccount.com \
     --project=emergent-strategies --allow-unauthenticated
   ```
   Grant the SA `roles/secretmanager.secretAccessor`. Verify `/health`.

### Phase B — durable jobs (Cloud Tasks) ✅ code done
- Job state is in the graph (`(:Job {id,type,status,dataJson})`, `app/graph/jobs.py`);
  `proposals.py` / `backfill.py` create+enqueue jobs and expose `run_*_job` runners;
  poll/commit read the graph. Verified locally (`job_mode=local` runs inline).
- **Remaining GCP wiring:**
  1. `gcloud tasks queues create nebula-jobs --location=europe-west2 --project=emergent-strategies`.
  2. Cloud Run env: `JOB_MODE=cloudtasks`, `GCP_PROJECT=emergent-strategies`,
     `CLOUD_TASKS_LOCATION=europe-west2`, `SERVICE_URL=<cloud run url>`,
     `TASKS_SERVICE_ACCOUNT=<tasks-invoker SA>`.
  3. Grant the Cloud Run SA `roles/cloudtasks.enqueuer`; give the tasks-invoker SA
     `roles/run.invoker`.
  4. **TODO:** verify the Cloud Tasks OIDC token in `POST /jobs/run/{id}` (folded
     into the Phase C auth dependency — that route uses OIDC, not the Firebase token).
- Note: `:Job` nodes persist; add a periodic cleanup (e.g. delete jobs older than N
  days) later if they pile up.

### Phase C — auth + Firebase Hosting (nebula subdomain)
**Code done** (behind flags; local dev unaffected):
- Backend `app/auth.py`: `verify_user` (Firebase ID token + `ALLOWED_EMAILS`) on the
  main router; `verify_task` (Cloud Tasks OIDC from `TASKS_SERVICE_ACCOUNT`) on
  `/jobs/run`; `/health` open. Both no-op when `REQUIRE_AUTH` is off.
- Frontend: `AuthGate` (Google sign-in gate) + ID token attached to every `/api`
  call — active only when `VITE_AUTH_ENABLED=true`.

**Deploy wiring:**
1. Firebase console: enable the **Google** sign-in provider.
2. Cloud Run env: `REQUIRE_AUTH=true`, `ALLOWED_EMAILS=you@example.com,\
teammate@example.com`, plus the Phase B job vars. Cloud Run's default SA is
   the Firebase Admin identity (grant it token-verify via project membership).
3. Frontend build env: `VITE_AUTH_ENABLED=true`, `VITE_API_BASE=/api`, and the
   `VITE_FIREBASE_*` values (apiKey/authDomain/projectId/appId) from the console.
4. `firebase.json`: hosting target `nebula`, rewrites `/api/**` → Cloud Run
   `nebula-api`, else → `/index.html`. `firebase target:apply hosting nebula …`;
   build Vite; `firebase deploy --only hosting:nebula`.
5. Add `nebula.emergentstrategies.tech` as a custom domain in Firebase Hosting; DNS.

### Phase D — MCP (local)
- Local `backend/.env` → Aura creds + `GEMINI_API_KEY`. Run `.mcp.json` as today; it
  now reads/writes prod data. Nothing exposed.

### Phase E — switchable LLM provider (#8 — done)
- LiteLLM for ADK agents + the `app/llm.py` wrapper for direct calls; `LLM_PROVIDER` /
  `LLM_MODEL` env (see "Switchable LLM" above). Default `gemini` keeps today's native
  path unchanged. Remaining ops task: wire each provider's API key into Secret Manager.

## Cost

Scale-to-zero Cloud Run + Aura Free + Firebase Hosting + Cloud Tasks all sit within
free tiers for personal use → **~$0/month idle**, plus Gemini API usage. (First
request after Aura auto-pause or a Cloud Run cold start has a few-second delay.)

## Settled

- **GCP project:** `emergent-strategies` (region assumed `europe-west2` — confirm).
- **Migration:** `neo4j-admin` **dump/load** (keeps existing research).
- **Allowlist:** `you@example.com`, `teammate@example.com`.
