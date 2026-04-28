#!/bin/bash
# deploy.sh — rebuild and redeploy the FIFO backend to Cloud Run
# Run from the Finance_Software root directory.
#
# Prerequisites: gcloud CLI installed + authenticated
#   brew install google-cloud-sdk
#   gcloud auth login
#   gcloud config set project <your-project-id>

set -e

PROJECT=$(gcloud config get-value project 2>/dev/null)
SERVICE="finance-demo"
REGION="us-central1"
IMAGE="us-central1-docker.pkg.dev/${PROJECT}/${SERVICE}/${SERVICE}:latest"

echo "→ Configuring Docker for Artifact Registry"
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet

echo "→ Building Docker image locally: ${IMAGE}"
docker build -t "${IMAGE}" .

echo "→ Pushing image to Artifact Registry"
docker push "${IMAGE}"

echo "→ Deploying to Cloud Run (min-instances=1 to keep DB alive between requests)"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --min-instances 1 \
  --max-instances 3 \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --port 8000

echo "✓ Deployed. Service URL:"
gcloud run services describe "${SERVICE}" \
  --region "${REGION}" \
  --format "value(status.url)"
