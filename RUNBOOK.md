# Deploy runbook

Ordered commands to deploy Nebula. Architecture/rationale is in `DEPLOYMENT.md`.
Settled: project `emergent-strategies`, region `europe-west2`, dump/load migration,
allowlist `yrnclndymn@gmail.com`, `andy@emergentstrategies.tech`.

Placeholders `<LIKE_THIS>` come from a console step; fill them as you go.

## 0. Prereqs (once)
```bash
gcloud auth login
gcloud config set project emergent-strategies
gcloud services enable run.googleapis.com cloudtasks.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com identitytoolkit.googleapis.com
npm i -g firebase-tools && firebase login
```

## 1. Neo4j Aura + data (dump/load)
1. Neo4j Aura console → create a **Free** instance. Note `NEO4J_URI` (neo4j+s://…),
   user (`neo4j`), password.
2. Dump local → upload to Aura:
```bash
docker exec nebula-neo4j neo4j-admin database dump neo4j --to-path=/tmp
docker cp nebula-neo4j:/tmp/neo4j.dump ./neo4j.dump
# Aura console → your instance → "Import database" → upload neo4j.dump
# (or: neo4j-admin database upload neo4j --from-path=. --to-uri=<NEO4J_URI>)
```

## 2. Secrets
```bash
for S in GEMINI_API_KEY NEO4J_URI NEO4J_USER NEO4J_PASSWORD; do
  printf '%s' "<value-for-$S>" | gcloud secrets create "$S" --data-file=- 2>/dev/null \
    || printf '%s' "<value>" | gcloud secrets versions add "$S" --data-file=-
done
```

## 3. Service accounts + IAM
```bash
PROJECT=emergent-strategies
gcloud iam service-accounts create nebula-run  --display-name="Nebula Cloud Run"
gcloud iam service-accounts create nebula-tasks --display-name="Nebula Cloud Tasks invoker"
RUN_SA=nebula-run@$PROJECT.iam.gserviceaccount.com
TASK_SA=nebula-tasks@$PROJECT.iam.gserviceaccount.com
# Cloud Run SA: read secrets, enqueue tasks, verify Firebase tokens (project member)
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$RUN_SA --role=roles/secretmanager.secretAccessor
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$RUN_SA --role=roles/cloudtasks.enqueuer
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$RUN_SA --role=roles/firebaseauth.viewer
```

## 4. Cloud Tasks queue
```bash
gcloud tasks queues create nebula-jobs --location=europe-west2
```

## 5. Deploy backend (Cloud Run, scale-to-zero)
```bash
gcloud run deploy nebula-api --source backend --region europe-west2 \
  --service-account=$RUN_SA --min-instances=0 --max-instances=2 --allow-unauthenticated \
  --set-secrets=GEMINI_API_KEY=GEMINI_API_KEY:latest,NEO4J_URI=NEO4J_URI:latest,NEO4J_USER=NEO4J_USER:latest,NEO4J_PASSWORD=NEO4J_PASSWORD:latest \
  --set-env-vars="^##^REQUIRE_AUTH=true##API_PREFIX=/api##JOB_MODE=cloudtasks##GCP_PROJECT=emergent-strategies##CLOUD_TASKS_LOCATION=europe-west2##TASKS_SERVICE_ACCOUNT=$TASK_SA##GEMINI_MODEL=gemini-3.1-flash-lite##AGENT_MODEL=gemini-3.1-flash-lite##ALLOWED_EMAILS=yrnclndymn@gmail.com,andy@emergentstrategies.tech##FRONTEND_ORIGIN=https://nebula.emergentstrategies.tech"
# ^##^ sets '##' as the pair delimiter so the comma in ALLOWED_EMAILS is literal.
# Note the service URL, then set SERVICE_URL + let the tasks SA invoke it:
SERVICE_URL=$(gcloud run services describe nebula-api --region europe-west2 --format='value(status.url)')
gcloud run services update nebula-api --region europe-west2 --update-env-vars=SERVICE_URL=$SERVICE_URL
gcloud run services add-iam-policy-binding nebula-api --region europe-west2 \
  --member=serviceAccount:$TASK_SA --role=roles/run.invoker
curl -s $SERVICE_URL/health   # expect {"status":"ok"}
```

## 6. Firebase Auth + Hosting (nebula subdomain)
1. Firebase console → Authentication → enable **Google** provider.
2. Create the hosting site + wire the target:
```bash
firebase hosting:sites:create nebula-emergentstrategies   # or your chosen id
firebase target:apply hosting nebula nebula-emergentstrategies
```
3. Build the SPA with prod config (VITE_FIREBASE_* from console → Project settings):
```bash
cd frontend
VITE_AUTH_ENABLED=true VITE_API_BASE=/api \
VITE_FIREBASE_API_KEY=<...> VITE_FIREBASE_AUTH_DOMAIN=<...> \
VITE_FIREBASE_PROJECT_ID=emergent-strategies VITE_FIREBASE_APP_ID=<...> \
  npm run build
cd ..
firebase deploy --only hosting:nebula
```
4. Firebase console → Hosting → add custom domain `nebula.emergentstrategies.tech`;
   add the DNS records it shows. Also add that domain to Authentication → Settings →
   Authorized domains.

## 7. Smoke test
- Open `https://nebula.emergentstrategies.tech` → Google sign-in → table loads.
- Assistant → "research a company" → proposal appears (Cloud Task ran it) → commit.
- MCP: local `backend/.env` → Aura creds + `GEMINI_API_KEY`; use as before.

## Redeploys
- Backend: `gcloud run deploy nebula-api --source backend --region europe-west2`.
- Frontend: rebuild with the VITE_* env, `firebase deploy --only hosting:nebula`.
- Refresh prod data: re-dump local and re-import to Aura (step 1.2).
