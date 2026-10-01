#!/usr/bin/env bash
# Deploys Audio Notes to Google Cloud Run. Stages (run in this order the first time):
#   setup    one-time: project APIs, service accounts, image registry, Cloud Tasks queue, secrets from .env
#   build    build the image with Cloud Build and push it to Artifact Registry
#   steps    deploy the PRIVATE step service Cloud Tasks calls
#   migrate  run `alembic upgrade head` against the database as a Cloud Run job
#   api      deploy the PUBLIC API
# usage: deploy/gcp.sh <stage> [stage...]      (from the repository root; needs gcloud logged in)
# The project and its billing account must exist already:
#   gcloud projects create $PROJECT && gcloud billing projects link $PROJECT --billing-account=<id>
# Secret values come from the git-ignored .env and are piped to Secret Manager, never printed.
set -euo pipefail

PROJECT=${PROJECT:-audio-notes-demo}
REGION=${REGION:-asia-south1}                        # next to the S3 bucket (ap-south-1)
TAG=${TAG:-v1}
CORS_ORIGINS=${CORS_ORIGINS:-http://localhost:3000}  # the frontend's origin(s), comma separated
MAX_UPLOADS_PER_DAY=${MAX_UPLOADS_PER_DAY:-5}        # per browser, per 24 hours
MAX_UPLOAD_BYTES=${MAX_UPLOAD_BYTES:-209715200}      # 200 MB for the public demo (the code allows 2 GiB)

IMAGE=$REGION-docker.pkg.dev/$PROJECT/audio-notes/app:$TAG
API_SA=api-sa@$PROJECT.iam.gserviceaccount.com
STEPS_SA=steps-sa@$PROJECT.iam.gserviceaccount.com
QUEUE=projects/$PROJECT/locations/$REGION/queues/job-steps
G="--project $PROJECT"

env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '"\r'; }
number() { gcloud projects describe "$PROJECT" --format='value(projectNumber)'; }
# Cloud Run's URL is predictable, so the services can be told each other's address before either exists.
steps_url() { echo "https://steps-$(number).$REGION.run.app"; }

common_env() {  # "^#^" makes # the separator, so a value may contain commas (and @, as an email does)
  echo "^#^QUEUE_BACKEND=cloudtasks#CLOUD_TASKS_QUEUE=$QUEUE#STEPS_URL=$(steps_url)#STEPS_INVOKER_EMAIL=$STEPS_SA#STORAGE_REGION=$(env_value STORAGE_REGION)#STORAGE_BUCKET=$(env_value STORAGE_BUCKET)#DB_CONNECT_TIMEOUT_SECONDS=10#APP_ENV=production"
}
DB_SECRETS=DATABASE_URL=database-url:latest,STORAGE_ACCESS_KEY_ID=s3-access-key-id:latest,STORAGE_SECRET_ACCESS_KEY=s3-secret-access-key:latest

secret() {  # secret <secret-name> <ENV_VAR in .env> <service account that may read it>...
  local name=$1 var=$2; shift 2
  if gcloud secrets describe "$name" $G >/dev/null 2>&1; then
    env_value "$var" | tr -d '\n' | gcloud secrets versions add "$name" --data-file=- $G >/dev/null
  else
    env_value "$var" | tr -d '\n' | gcloud secrets create "$name" --replication-policy=automatic --data-file=- $G >/dev/null
  fi
  for sa in "$@"; do
    gcloud secrets add-iam-policy-binding "$name" --member="serviceAccount:$sa" \
      --role=roles/secretmanager.secretAccessor $G --format='value(etag)' >/dev/null
  done
  echo "secret $name ready"
}

setup() {
  gcloud services enable run.googleapis.com cloudtasks.googleapis.com secretmanager.googleapis.com \
    artifactregistry.googleapis.com cloudbuild.googleapis.com $G
  gcloud iam service-accounts describe "$API_SA" $G >/dev/null 2>&1 || gcloud iam service-accounts create api-sa --display-name="Audio Notes API" $G
  gcloud iam service-accounts describe "$STEPS_SA" $G >/dev/null 2>&1 || gcloud iam service-accounts create steps-sa --display-name="Audio Notes step runner" $G
  gcloud artifacts repositories describe audio-notes --location="$REGION" $G >/dev/null 2>&1 \
    || gcloud artifacts repositories create audio-notes --repository-format=docker --location="$REGION" $G
  # Retries mirror what Celery had: about 10 attempts, 5 s to 120 s apart. Two steps at a time keeps Gnani and Groq calm.
  local queue_flags=(--max-concurrent-dispatches=2 --max-dispatches-per-second=5 --max-attempts=10 --min-backoff=5s --max-backoff=120s --max-doublings=4)
  gcloud tasks queues describe job-steps --location="$REGION" $G >/dev/null 2>&1 \
    && gcloud tasks queues update job-steps --location="$REGION" "${queue_flags[@]}" $G \
    || gcloud tasks queues create job-steps --location="$REGION" "${queue_flags[@]}" $G
  for sa in "$API_SA" "$STEPS_SA"; do  # both enqueue: the API starts a job, a step schedules its next step
    gcloud tasks queues add-iam-policy-binding job-steps --location="$REGION" --member="serviceAccount:$sa" \
      --role=roles/cloudtasks.enqueuer $G --format='value(etag)' >/dev/null
  done
  # Creating a task that signs as steps-sa requires permission to act as it.
  for sa in "$API_SA" "$STEPS_SA"; do
    gcloud iam service-accounts add-iam-policy-binding "$STEPS_SA" --member="serviceAccount:$sa" \
      --role=roles/iam.serviceAccountUser $G --format='value(etag)' >/dev/null
  done
  # The API holds the database and storage secrets only; the provider keys belong to the step service alone.
  secret database-url NEON_DATABASE_URL "$API_SA" "$STEPS_SA"
  secret s3-access-key-id STORAGE_ACCESS_KEY_ID "$API_SA" "$STEPS_SA"
  secret s3-secret-access-key STORAGE_SECRET_ACCESS_KEY "$API_SA" "$STEPS_SA"
  secret gnani-api-key GNANI_API_KEY "$STEPS_SA"
  secret llm-api-key LLM_API_KEY "$STEPS_SA"
}

build() {
  gcloud builds submit --tag "$IMAGE" $G .
}

steps() {
  # One instance: every step then shares the one process-wide pace that keeps Gnani under its call limit.
  gcloud run deploy steps --image "$IMAGE" --region "$REGION" $G --service-account "$STEPS_SA" \
    --no-allow-unauthenticated --max-instances 1 --timeout 300 --memory 512Mi \
    --set-env-vars "$(common_env)#SERVICE_ROLE=steps" \
    --set-secrets "$DB_SECRETS,GNANI_API_KEY=gnani-api-key:latest,LLM_API_KEY=llm-api-key:latest"
  # Only Cloud Tasks (signing as steps-sa) may call it. Access is per service, which is why it is separate from the API.
  gcloud run services add-iam-policy-binding steps --region "$REGION" $G \
    --member="serviceAccount:$STEPS_SA" --role=roles/run.invoker --format='value(etag)' >/dev/null
}

migrate() {
  gcloud run jobs deploy migrate --image "$IMAGE" --region "$REGION" $G --service-account "$API_SA" \
    --command alembic --args upgrade,head --max-retries 0 --task-timeout 300 \
    --set-env-vars "$(common_env)" --set-secrets "$DB_SECRETS"
  gcloud run jobs execute migrate --region "$REGION" $G --wait
}

api() {
  gcloud run deploy api --image "$IMAGE" --region "$REGION" $G --service-account "$API_SA" \
    --allow-unauthenticated --max-instances 3 --memory 512Mi \
    --set-env-vars "$(common_env)#SERVICE_ROLE=api#CORS_ORIGINS=$CORS_ORIGINS#MAX_UPLOADS_PER_DAY=$MAX_UPLOADS_PER_DAY#MAX_UPLOAD_BYTES=$MAX_UPLOAD_BYTES" \
    --set-secrets "$DB_SECRETS"
}

[ $# -gt 0 ] || { sed -n '2,10p' "$0"; exit 1; }
for stage in "$@"; do "$stage"; done
