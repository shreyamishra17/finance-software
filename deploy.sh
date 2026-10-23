#!/bin/bash
# deploy.sh — rebuild and redeploy the FIFO backend to Cloud Run
# Run from the Finance_Software root directory.
#
# Prerequisites: gcloud CLI installed + authenticated
#   brew install google-cloud-sdk
#   gcloud auth login
#   gcloud config set project <your-project-id>

set -e

# Canonical project (override with GCP_PROJECT_ID if ever needed).
PROJECT="${GCP_PROJECT_ID:-financesoftware-487822}"
SERVICE="finance-demo"
REGION="us-central1"
IMAGE="us-central1-docker.pkg.dev/${PROJECT}/${SERVICE}/${SERVICE}:latest"

# Runtime configuration baked into the Cloud Run service.
FIREBASE_STORAGE_BUCKET="financesoftware-487822.firebasestorage.app"
FIRESTORE_DATABASE="finance-database"

echo "→ Configuring Docker for Artifact Registry"
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

echo "→ Building Docker image locally: ${IMAGE}"
docker build -t "${IMAGE}" .

echo "→ Pushing image to Artifact Registry"
docker push "${IMAGE}"

echo "→ Deploying to Cloud Run (project: ${PROJECT})"
gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 3 \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --port 8000 \
  --set-env-vars "PERSIST_MODE=firebase_storage,FIREBASE_STORAGE_BUCKET=${FIREBASE_STORAGE_BUCKET},FIRESTORE_DATABASE=${FIRESTORE_DATABASE}"

echo "✓ Deployed. Service URL:"
gcloud run services describe "${SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --format "value(status.url)"
