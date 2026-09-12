#!/usr/bin/env bash
# Deploy the Auth Broker to Cloud Run as the sole holder of KMS signing rights.
#
# The broker is the only workload granted roles/cloudkms.signerVerifier. Agents
# get roles/cloudkms.publicKeyViewer, which lets them verify a delegation they
# could never mint. No private key is packaged anywhere; KMS holds it and will
# not export it.
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE="a2a-auth-broker"
SA_NAME="a2a-auth-broker"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
KEY_RING="a2a-demo-delegation"
KEY_ID="delegation-issuer"
KEY="projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KEY_RING}/cryptoKeys/${KEY_ID}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

step() { echo; echo "=== $* ==="; }
info() { echo "    $*"; }

step "Service account"
if gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  info "exists: $SA_EMAIL"
else
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \
    --display-name "A2A Auth Broker" \
    --description "Sole caller of KMS asymmetricSign for delegation tokens" >/dev/null
  info "created: $SA_EMAIL"
fi

step "Granting signing rights on the key only (not project-wide)"
gcloud kms keys add-iam-policy-binding "$KEY_ID" \
  --keyring "$KEY_RING" --location "$REGION" --project "$PROJECT_ID" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/cloudkms.signerVerifier >/dev/null
info "roles/cloudkms.signerVerifier on $KEY_ID -> $SA_EMAIL"

# Traces only. The broker needs nothing else at project level.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/cloudtrace.agent --condition=None >/dev/null 2>&1 || true
info "roles/cloudtrace.agent granted"

step "Artifact Registry repository"
if ! gcloud artifacts repositories describe a2a-demo \
     --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create a2a-demo --repository-format=docker \
    --location "$REGION" --project "$PROJECT_ID" \
    --description "A2A governance demo images" >/dev/null
  info "created repository a2a-demo"
else
  info "repository a2a-demo exists"
fi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/a2a-demo/${SERVICE}:$(date +%Y%m%d-%H%M%S)"

step "Building the image"
# This gcloud has no `run deploy --dockerfile`, so the image is built explicitly
# from cloud/auth_broker/Dockerfile with the repo root as build context.
gcloud builds submit "$REPO_ROOT" \
  --project "$PROJECT_ID" --region "$REGION" \
  --config "$REPO_ROOT/cloud/auth_broker/cloudbuild.yaml" \
  --substitutions "_IMAGE=${IMAGE}" --quiet
info "built $IMAGE"

step "Deploying (authenticated access only)"
gcloud run deploy "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$SA_EMAIL" \
  --no-allow-unauthenticated \
  --set-env-vars "DELEGATION_SIGNER=kms,TRACE_EXPORTER=gcp,KMS_SIGNING_KEY=${KEY}/cryptoKeyVersions/1,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},AUTH_BROKER_AUDIENCE=https://a2a-auth-broker-611427964532.${REGION}.run.app" \
  --min-instances 0 --max-instances 3 --timeout 60 \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
REVISION="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.latestReadyRevisionName)')"

step "Deployed"
info "service  : $SERVICE"
info "revision : $REVISION"
info "url      : $URL"
info "auth     : --no-allow-unauthenticated (callers need roles/run.invoker)"
echo
echo "AUTH_BROKER_URL=$URL"
echo "KMS_SIGNING_KEY=${KEY}/cryptoKeyVersions/1"
