#!/usr/bin/env sh
# Grant the hosted agent's managed identities the "Azure AI User" role on the
# Azure AI Services account. This is required so the agent's container can call
# the Foundry Responses API (the agent itself uses DefaultAzureCredential and
# without this role assignment the inner LLM calls return 401 Unauthorized).
#
# Idempotent: re-running is safe (az role assignment create returns existing
# assignments without error when --only-show-errors is set).

set -e

if [ -z "${AZURE_RESOURCE_GROUP:-}" ] || [ -z "${AZURE_AI_ACCOUNT_NAME:-}" ]; then
    echo "[grant-agent-rbac] Skipping: required azd env vars not set." >&2
    exit 0
fi

if [ -z "${AZURE_SUBSCRIPTION_ID:-}" ]; then
    AZURE_SUBSCRIPTION_ID="$(az account show --query id -o tsv 2>/dev/null || true)"
fi

if [ -z "${AZURE_SUBSCRIPTION_ID:-}" ]; then
    echo "[grant-agent-rbac] Could not determine subscription id; skipping." >&2
    exit 0
fi

SCOPE="/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}/providers/Microsoft.CognitiveServices/accounts/${AZURE_AI_ACCOUNT_NAME}"

# The agent identity service principal is created by the Foundry control plane
# during `azd deploy`. Names follow the pattern
#   <ai-account>-<ai-project>-<agent-name>-AgentIdentity
#   <ai-account>-<ai-project>-<agent-name>-<short>-AgentIdentityBlueprint
PROJECT_NAME="${AZURE_AI_PROJECT_NAME:-}"
AGENT_NAME="support-ticket-workflow-agent"
PREFIX="${AZURE_AI_ACCOUNT_NAME}-${PROJECT_NAME}-${AGENT_NAME}"

echo "[grant-agent-rbac] Searching for agent identities matching: ${PREFIX}*"

# Retry loop because the SPs may not yet be visible in Microsoft Graph
# immediately after `azd deploy` finishes registering the agent.
ATTEMPT=0
MAX_ATTEMPTS=12
PRINCIPAL_IDS=""
while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    PRINCIPAL_IDS="$(az ad sp list --display-name "${PREFIX}" --query '[].id' -o tsv 2>/dev/null || true)"
    if [ -n "${PRINCIPAL_IDS}" ]; then
        break
    fi
    ATTEMPT=$((ATTEMPT + 1))
    echo "[grant-agent-rbac] Identities not yet available (attempt ${ATTEMPT}/${MAX_ATTEMPTS}); waiting 10s..."
    sleep 10
done

if [ -z "${PRINCIPAL_IDS}" ]; then
    echo "[grant-agent-rbac] WARNING: no agent identities found. You may need to grant the 'Azure AI User' role manually on ${SCOPE}." >&2
    exit 0
fi

for PID in ${PRINCIPAL_IDS}; do
    echo "[grant-agent-rbac] Granting 'Azure AI User' to ${PID} on ${SCOPE}"
    az role assignment create \
        --assignee-object-id "${PID}" \
        --assignee-principal-type ServicePrincipal \
        --role "Azure AI User" \
        --scope "${SCOPE}" \
        --only-show-errors >/dev/null || true
done

echo "[grant-agent-rbac] Done. Role propagation may take 1-3 minutes."
