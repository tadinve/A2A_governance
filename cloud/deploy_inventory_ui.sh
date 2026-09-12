#!/usr/bin/env bash
# Deploy the Inventory & Purchasing UI to Cloud Run.
#
# The UI is where a human reviews and approves a draft, so its state is the
# evidence that approval happened. Two consequences shape this deployment:
#
#   * it is pinned to exactly one instance. Approval records and submission
#     idempotency claims live in SQLite on the instance filesystem, so a second
#     instance would hold a second, divergent copy: an approval granted on one
#     would be invisible to the other, and the submission guard that stops a
#     draft being ordered twice would stop guarding. One instance makes that
#     impossible rather than unlikely;
#   * that state does NOT survive a new revision. Redeploying starts an empty
#     database. Acceptable for a demonstration, not for anything real -- see
#     SECURITY_NOTES.md. Moving to Cloud SQL is the fix, and store.py is the
#     only module that would change.
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE="a2a-inventory-ui"
SA_NAME="a2a-inventory-ui"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

step() { echo; echo "=== $* ==="; }
info() { echo "    $*"; }

step "Service account"
if gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  info "exists: $SA_EMAIL"
else
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \
    --display-name "A2A Inventory UI" \
    --description "Runs the purchasing UI; holds no signing key" >/dev/null
  info "created: $SA_EMAIL"
fi

step "Granting only what the UI actually needs"
# The Zoho MCP endpoint URLs. The UI never sees a Zoho credential itself.
for secret in zoho-invread-mcp-url zoho-procurewrite-mcp-url; do
  if gcloud secrets describe "$secret" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud secrets add-iam-policy-binding "$secret" --project "$PROJECT_ID" \
      --member "serviceAccount:${SA_EMAIL}" \
      --role roles/secretmanager.secretAccessor >/dev/null
    info "secretAccessor on $secret"
  else
    info "WARNING: secret $secret not found; run cloud/setup_zoho_secrets.py"
  fi
done
# Reads IAM policy to render the live security plane, and exports traces.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/cloudtrace.agent --condition=None >/dev/null 2>&1 || true
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/iam.securityReviewer --condition=None >/dev/null 2>&1 || true
info "cloudtrace.agent, iam.securityReviewer"

step "Session secret"
# Cookies are signed with this. Generated in the container by default, which on
# Cloud Run means it changes whenever the instance restarts and logs everyone
# out; holding it in Secret Manager keeps sessions valid across restarts.
if ! gcloud secrets describe a2a-ui-session-secret --project "$PROJECT_ID" >/dev/null 2>&1; then
  python3 -c "import secrets; print(secrets.token_hex(32))" \
    | gcloud secrets create a2a-ui-session-secret --project "$PROJECT_ID" \
        --data-file=- --replication-policy=automatic >/dev/null
  info "created a2a-ui-session-secret"
else
  info "a2a-ui-session-secret exists"
fi
gcloud secrets add-iam-policy-binding a2a-ui-session-secret --project "$PROJECT_ID" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/secretmanager.secretAccessor >/dev/null

step "Artifact Registry repository"
if ! gcloud artifacts repositories describe a2a-demo \
     --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create a2a-demo --repository-format=docker \
    --location "$REGION" --project "$PROJECT_ID" \
    --description "A2A governance demo images" >/dev/null
fi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/a2a-demo/${SERVICE}:$(date +%Y%m%d-%H%M%S)"

step "Building the image"
gcloud builds submit "$REPO_ROOT" \
  --project "$PROJECT_ID" --region "$REGION" \
  --config "$REPO_ROOT/cloud/inventory_ui/cloudbuild.yaml" \
  --substitutions "_IMAGE=${IMAGE}" --quiet
info "built $IMAGE"

ZOHO_ORGANIZATION_ID="${ZOHO_ORGANIZATION_ID:-}"
DEMO_SKU="${DEMO_SKU:-DEMO-WIDGET-A}"
ZOHO_REORDER_POLICY="${ZOHO_REORDER_POLICY:-{\"DEMO-WIDGET-A\":{\"target_stock\":100,\"min_order_quantity\":1}}}"

step "Deploying (single instance, authenticated access only)"
gcloud run deploy "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$SA_EMAIL" \
  --no-allow-unauthenticated \
  --min-instances 1 --max-instances 1 --timeout 300 \
  --set-secrets "SESSION_SECRET=a2a-ui-session-secret:latest" \
  --set-env-vars "^@^GOOGLE_CLOUD_PROJECT=${PROJECT_ID}@GOOGLE_CLOUD_LOCATION=${REGION}@TRACE_EXPORTER=gcp@ZOHO_ORGANIZATION_ID=${ZOHO_ORGANIZATION_ID}@DEMO_SKU=${DEMO_SKU}@ZOHO_REORDER_POLICY=${ZOHO_REORDER_POLICY}" \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
step "Deployed"
info "url  : $URL"
info "auth : --no-allow-unauthenticated; grant a human roles/run.invoker to open it:"
info "       gcloud run services add-iam-policy-binding $SERVICE --region $REGION \\"
info "         --member user:SOMEONE@example.com --role roles/run.invoker"
echo
echo "INVENTORY_UI_URL=$URL"
