#!/usr/bin/env bash
# Deploy this Function App code to an existing Azure Function App.
#
# Usage:
#   ./scripts/deploy.sh <function-app-name>
#   ./scripts/deploy.sh <function-app-name> -g <resource-group>
#
# Prerequisites:
#   - Azure CLI (`az`) logged in (`az login`)
#   - Function App already exists in your selected subscription
#   - Function App Public network access must allow your client
#
# Note: This only deploys code. App settings / env vars are not changed.
# Flex Consumption apps use config-zip (with remote build for Python), not
# the preview `az functionapp deploy --type zip` OneDeploy path (returns 415).

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/deploy.sh <function-app-name> [-g|--resource-group <rg>]

Examples:
  ./scripts/deploy.sh my-scaler-func
  ./scripts/deploy.sh my-scaler-func -g my-rg
EOF
}

FUNCTION_APP_NAME=""
RESOURCE_GROUP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -g|--resource-group)
      RESOURCE_GROUP="${2:-}"
      if [[ -z "$RESOURCE_GROUP" ]]; then
        echo "error: --resource-group requires a value" >&2
        exit 1
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ -n "$FUNCTION_APP_NAME" ]]; then
        echo "error: unexpected argument: $1" >&2
        usage >&2
        exit 1
      fi
      FUNCTION_APP_NAME="$1"
      shift
      ;;
  esac
done

if [[ -z "$FUNCTION_APP_NAME" ]]; then
  echo "error: function app name is required (from Azure Portal)" >&2
  usage >&2
  exit 1
fi

if ! command -v az >/dev/null 2>&1; then
  echo "error: Azure CLI (az) is not installed or not on PATH" >&2
  exit 1
fi

if ! command -v zip >/dev/null 2>&1; then
  echo "error: zip is not installed or not on PATH" >&2
  exit 1
fi

if ! az account show >/dev/null 2>&1; then
  echo "error: not logged in to Azure. Run: az login" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ZIP_PATH="${TMPDIR:-/tmp}/tdei-scalar-function-deploy.zip"

if [[ -z "$RESOURCE_GROUP" ]]; then
  echo "Looking up resource group for Function App '${FUNCTION_APP_NAME}'..."
  RESOURCE_GROUP="$(
    az functionapp list \
      --query "[?name=='${FUNCTION_APP_NAME}'].resourceGroup | [0]" \
      -o tsv
  )"
  if [[ -z "$RESOURCE_GROUP" || "$RESOURCE_GROUP" == "None" ]]; then
    echo "error: Function App '${FUNCTION_APP_NAME}' not found in the current subscription." >&2
    echo "  Current subscription: $(az account show --query name -o tsv)" >&2
    echo "  Pass -g <resource-group>, or switch subscription with: az account set --subscription <id>" >&2
    exit 1
  fi
fi

echo "Deploying to:"
echo "  Function App  : ${FUNCTION_APP_NAME}"
echo "  Resource Group: ${RESOURCE_GROUP}"
echo "  Subscription  : $(az account show --query name -o tsv)"

echo "Checking Function App network / publish settings..."
PUBLIC_ACCESS="$(
  az resource show \
    -g "$RESOURCE_GROUP" \
    -n "$FUNCTION_APP_NAME" \
    --resource-type Microsoft.Web/sites \
    --query "properties.publicNetworkAccess" \
    -o tsv 2>/dev/null || echo "Unknown"
)"

SCM_BASIC_AUTH="$(
  az resource show \
    -g "$RESOURCE_GROUP" \
    -n "${FUNCTION_APP_NAME}/basicPublishingCredentialsPolicies/scm" \
    --resource-type Microsoft.Web/sites/basicPublishingCredentialsPolicies \
    --query "properties.allow" \
    -o tsv 2>/dev/null || echo "Unknown"
)"

PLAN_ID="$(
  az functionapp show \
    -g "$RESOURCE_GROUP" \
    -n "$FUNCTION_APP_NAME" \
    --query "properties.serverFarmId" \
    -o tsv
)"
PLAN_SKU="$(
  az appservice plan show --ids "$PLAN_ID" --query "sku.tier" -o tsv 2>/dev/null || echo "Unknown"
)"
RUNTIME_NAME="$(
  az functionapp show \
    -g "$RESOURCE_GROUP" \
    -n "$FUNCTION_APP_NAME" \
    --query "properties.functionAppConfig.runtime.name" \
    -o tsv 2>/dev/null || echo ""
)"

echo "  Public network access : ${PUBLIC_ACCESS}"
echo "  SCM basic auth allow  : ${SCM_BASIC_AUTH}"
echo "  Hosting plan tier     : ${PLAN_SKU}"
echo "  Runtime               : ${RUNTIME_NAME:-unknown}"

if [[ "${PUBLIC_ACCESS}" == "Disabled" ]]; then
  cat >&2 <<EOF
error: Function App public network access is Disabled.
  Deploy calls the SCM endpoint and will return HTTP 403 from your machine.

Fix (temporary, then re-disable if needed):
  az resource update -g ${RESOURCE_GROUP} -n ${FUNCTION_APP_NAME} \\
    --resource-type Microsoft.Web/sites \\
    --set properties.publicNetworkAccess=Enabled

Then re-run:
  ./scripts/deploy.sh ${FUNCTION_APP_NAME} -g ${RESOURCE_GROUP}
EOF
  exit 1
fi

if [[ "${SCM_BASIC_AUTH}" == "false" ]]; then
  echo "note: SCM basic auth is disabled; deploy uses your az login (Azure AD) token."
fi

echo "Creating deployment zip..."
rm -f "$ZIP_PATH"
(
  cd "$REPO_ROOT"
  zip -r "$ZIP_PATH" . \
    -x ".git/*" \
    -x ".github/*" \
    -x ".venv/*" \
    -x "tests/*" \
    -x "simulation/*" \
    -x "scripts/*" \
    -x "__pycache__/*" \
    -x "*/__pycache__/*" \
    -x ".vscode/*" \
    -x ".env" \
    -x "local.settings.json" \
    -x "*.pyc" \
    -x ".DS_Store" \
    >/dev/null
)

# Flex Consumption + Python: Microsoft docs use config-zip --build-remote true.
# Preview `az functionapp deploy --type zip` returns HTTP 415 on Flex.
BUILD_REMOTE_ARGS=()
if [[ "${PLAN_SKU}" == "FlexConsumption" && "${RUNTIME_NAME}" == "python" ]]; then
  BUILD_REMOTE_ARGS=(--build-remote true)
  echo "Uploading zip (Flex Consumption + Python remote build)..."
else
  echo "Uploading zip..."
fi

set +e
az functionapp deployment source config-zip \
  --name "$FUNCTION_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --src "$ZIP_PATH" \
  "${BUILD_REMOTE_ARGS[@]}"
DEPLOY_EXIT=$?
set -e

rm -f "$ZIP_PATH"

if [[ "$DEPLOY_EXIT" -ne 0 ]]; then
  cat >&2 <<EOF
error: deployment failed (exit ${DEPLOY_EXIT}).

This app is on plan tier '${PLAN_SKU}' (runtime '${RUNTIME_NAME:-unknown}').

If you still see HTTP 403: public network access / SCM IP restrictions.
If you see HTTP 415: do not use 'az functionapp deploy --type zip' on Flex;
  this script uses config-zip instead.
If SCM basic auth is disabled and config-zip still fails, enable it temporarily:
  az resource update -g ${RESOURCE_GROUP} \\
    -n ${FUNCTION_APP_NAME}/basicPublishingCredentialsPolicies/scm \\
    --resource-type Microsoft.Web/sites/basicPublishingCredentialsPolicies \\
    --set properties.allow=true

Or publish with Azure Functions Core Tools:
  func azure functionapp publish ${FUNCTION_APP_NAME} --python
EOF
  exit "$DEPLOY_EXIT"
fi

echo "Deployment complete."
echo "Note: Function App settings were not modified."
