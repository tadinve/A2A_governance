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

# Cloud Run serves this service at https://<service>-<project number>.<region>.run.app.
# Agents mint their ID token for exactly that audience, and the broker verifies
# against it, so the two must agree -- and the project number is not something
# this script may assume. It was hardcoded to one lab, which meant every agent
# in a rebuilt project authenticated against an audience that did not exist.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
BROKER_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

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
  --set-env-vars "DELEGATION_SIGNER=kms,TRACE_EXPORTER=gcp,KMS_SIGNING_KEY=${KEY}/cryptoKeyVersions/1,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},AUTH_BROKER_AUDIENCE=${BROKER_URL}" \
  --min-instances 0 --max-instances 3 --timeout 60 \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
# Report the project-number URL, not Cloud Run's hashed alias: agents put this
# value in AUTH_BROKER_URL and mint their ID token for it, and it has to be the
# string the broker checks AUTH_BROKER_AUDIENCE against.
REVISION="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.latestReadyRevisionName)')"

step "Granting each deployed agent roles/run.invoker on this service"
# --no-allow-unauthenticated means Cloud Run's own front door checks this
# before a request ever reaches the FastAPI app -- a missing grant here 403s
# with a bare Cloud Run HTML page, not our JSON, and looks identical to "the
# broker rejected this token" when it is really "Cloud Run never let it in".
# Reads the principals config/broker_clients.json currently has, so this only
# grants real agents once registry_principals.py has written their identities
# in (the second broker deploy in deploy_to_gcp.sh's two-pass bootstrap); on
# the first pass, before any agent exists, there is nothing here to grant yet.
GRANTED=0
while read -r member; do
  [[ -n "$member" ]] || continue
  gcloud run services add-iam-policy-binding "$SERVICE" \
    --project "$PROJECT_ID" --region "$REGION" \
    --member "$member" --role roles/run.invoker --quiet >/dev/null
  GRANTED=$((GRANTED + 1))
done < <(python3 -c '
import json, sys
try:
    clients = json.load(open(sys.argv[1]))
except FileNotFoundError:
    sys.exit(0)
for client in clients.values():
    for principal in client.get("allowed_principals", []):
        if principal.startswith("principal://"):
            print(principal)
            break
' "$REPO_ROOT/config/broker_clients.json" 2>/dev/null)
if [[ "$GRANTED" -eq 0 ]]; then
  info "no agent principals in config/broker_clients.json yet -- nothing to grant"
  info "(expected on the first of the two broker deploys; run again after the agents exist)"
else
  info "granted roles/run.invoker to $GRANTED agent principal(s)"
fi

step "Deployed"
info "service  : $SERVICE"
info "revision : $REVISION"
info "url      : $BROKER_URL"
info "alias    : $URL  (Cloud Run's hashed alias; not the token audience)"
info "auth     : --no-allow-unauthenticated (callers need roles/run.invoker)"
echo
echo "AUTH_BROKER_URL=$BROKER_URL"
echo "KMS_SIGNING_KEY=${KEY}/cryptoKeyVersions/1"
