#!/usr/bin/env bash
# One-time setup for GitHub Actions → Cloud Run auto-deploy.
#
# Creates:
#   - Service account: github-deployer@${PROJECT}.iam.gserviceaccount.com
#   - Workload Identity Pool + GitHub OIDC provider
#   - IAM bindings (Cloud Build, Cloud Run, Artifact Registry, SA user)
#   - Allows the GitHub repo to impersonate the SA via WIF
#
# After it runs, it prints two values you paste into GitHub repo secrets:
#   GCP_WIF_PROVIDER  → projects/.../providers/github
#   GCP_DEPLOYER_SA   → github-deployer@<project>.iam.gserviceaccount.com
#
# Idempotent — safe to re-run.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-loupe-app-56235}"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
GITHUB_REPO="${GITHUB_REPO:-wiggapony0925/loupe-backend}"  # owner/repo
SA_NAME="github-deployer"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
POOL_ID="github-pool"
PROVIDER_ID="github"

echo "Project:        ${PROJECT_ID} (#${PROJECT_NUMBER})"
echo "GitHub repo:    ${GITHUB_REPO}"
echo "Service acct:   ${SA_EMAIL}"
echo

# --- enable required APIs ---
gcloud services enable \
  iamcredentials.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  --project "${PROJECT_ID}"

# --- create deployer service account ---
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="GitHub Actions deployer" \
    --project "${PROJECT_ID}"
fi

# --- grant roles needed to build + deploy ---
for role in \
  roles/run.admin \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.writer \
  roles/storage.admin \
  roles/iam.serviceAccountUser \
  roles/logging.viewer
do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet >/dev/null
done

# Cloud Run runtime SA (Compute default) must be usable by the deployer.
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud iam service-accounts add-iam-policy-binding "${COMPUTE_SA}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser" \
  --project "${PROJECT_ID}" \
  --quiet >/dev/null

# --- workload identity pool + provider ---
if ! gcloud iam workload-identity-pools describe "${POOL_ID}" \
    --location=global --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "${POOL_ID}" \
    --location=global \
    --display-name="GitHub Actions pool" \
    --project "${PROJECT_ID}"
fi

if ! gcloud iam workload-identity-pools providers describe "${PROVIDER_ID}" \
    --location=global --workload-identity-pool="${POOL_ID}" \
    --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_ID}" \
    --location=global \
    --workload-identity-pool="${POOL_ID}" \
    --display-name="GitHub OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository=='${GITHUB_REPO}'" \
    --project "${PROJECT_ID}"
fi

# --- allow the GitHub repo to impersonate the deployer SA ---
WIF_PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_REPO}"
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="${WIF_PRINCIPAL}" \
  --project "${PROJECT_ID}" \
  --quiet >/dev/null

PROVIDER_RESOURCE="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"

cat <<EOF

=========================================================================
Done. Add these as GitHub repo secrets (Settings → Secrets and variables
→ Actions → New repository secret):

  GCP_WIF_PROVIDER = ${PROVIDER_RESOURCE}
  GCP_DEPLOYER_SA  = ${SA_EMAIL}

Then push to main — .github/workflows/deploy.yml will build + deploy
loupe-api and loupe-worker automatically after CI passes.
=========================================================================
EOF
