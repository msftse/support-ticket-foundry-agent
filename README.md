# Support Ticket Workflow — Foundry Hosted Agent

A Microsoft Agent Framework **workflow** packaged as a Foundry **hosted agent**.
The workflow processes IT support tickets through parallel triage / knowledge /
context / docs / business branches, then runs a validator agent and a
deterministic retry controller that loops back targeted branches before closing
the case or escalating.

## Architecture

```
input → initialize → route → prepare ──┬→ triage     ─┐
                                       ├→ knowledge  ─┤
                                       ├→ context    ─┼→ aggregate → resolution
                                       ├→ docs       ─┤                 │
                                       └→ business   ─┘                 ▼
                                                                   validator
                                                                        │
                                                                        ▼
                                                              retry_controller
                                                                        │
                                                            ┌───────────┼───────────┐
                                                            ▼           ▼           ▼
                                                          close       retry       error
```

The agent is exposed over the Foundry **Responses** protocol at
`/agents/support-ticket-workflow-agent/endpoint/protocols/openai/responses`.

## Repository layout

```
agent/                         Hosted agent source (Python)
  agent.yaml                   ContainerAgent definition (kind, protocol, env vars)
  agent.manifest.yaml          Agent template + required model resources
  Dockerfile                   Container image (python:3.12-slim)
  main.py                      Entrypoint — wraps the workflow with ResponsesHostServer
  workflow.py                  WorkflowBuilder graph (executors, edges, fan-out/in)
  middleware.py                Per-executor middleware (telemetry, retry guard)
  models.py                    Pydantic models for ticket payloads, asset records, etc.
  tools.py                     @tool functions exposed to LLM agents
  tracing.py                   OpenTelemetry tracer + span attribute helpers
  requirements.txt             Pinned Python deps
infra/                         Bicep IaC (subscription-scope deployment)
  main.bicep                   Foundry project + ACR + App Insights + role assignments
  main.parameters.json         azd parameter substitution
  core/                        Reusable Bicep modules
scripts/
  grant-agent-rbac.sh          Post-deploy: grants the agent MI 'Azure AI User'
azure.yaml                     azd service + infra wiring
```

## Prerequisites

- Azure subscription with permission to create an AI Foundry account, ACR,
  Application Insights, and role assignments
- [Azure Developer CLI (azd)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) ≥ 1.11
- [Azure CLI (az)](https://learn.microsoft.com/cli/azure/install-azure-cli) ≥ 2.60
- [Docker](https://www.docker.com/) running locally (azd builds the agent image)
- The `azure.ai.agents` azd extension (azd installs it on first run)

## Quick start

```bash
azd auth login
az login

azd init                              # if you don't already have a .azure env
azd env new support-tickets           # pick any name

# Region must support the Azure OpenAI Responses API; swedencentral works well.
azd env set AZURE_LOCATION swedencentral

# Enable the hosted-agents capability host and pre-create the gpt-4o deployment.
azd env set ENABLE_HOSTED_AGENTS true
azd env set ENABLE_CAPABILITY_HOST true
azd env set AI_PROJECT_DEPLOYMENTS '[{"name":"gpt-4o","model":{"format":"OpenAI","name":"gpt-4o","version":"2024-11-20"},"sku":{"name":"GlobalStandard","capacity":50}}]'

azd up
```

`azd up` will:

1. Provision the Foundry project, ACR, App Insights, and gpt-4o model deployment.
2. Build the agent container, push it to ACR, and register the agent with Foundry.
3. Run `scripts/grant-agent-rbac.sh` to grant the agent's managed identity the
   `Azure AI User` role on the AI Services account (required for the inner
   Responses API calls).

> **Why the post-deploy script?** Foundry creates a per-agent managed identity
> only when the agent is registered (during `azd deploy`, not `azd provision`),
> so the role assignment cannot live in Bicep. The script is idempotent.

## Smoke test

```bash
ENDPOINT="$(azd env get-value AZURE_AI_PROJECT_ENDPOINT)"
TOKEN="$(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv)"

curl -sS -X POST \
  "$ENDPOINT/agents/support-ticket-workflow-agent/endpoint/protocols/openai/responses?api-version=2025-11-15-preview" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --max-time 240 \
  -d '{"input":"Laptop sign-in keeps failing after a security update.","store":false}'
```

Expected: HTTP 200, `status: completed`, an `output_text` summarising the
workflow result (ticket id, retry counts, validator concerns, resolution or
escalation reason).

If you receive an `internal server error` immediately after deploying, wait
1–3 minutes for Entra role propagation and retry. Inspect logs in App Insights
(`AppTraces` table, `AppRoleName has 'support-ticket'`).

## Environment variables (set in `agent/agent.yaml`)

| Name | Default | Purpose |
| --- | --- | --- |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | `gpt-4o` | Foundry model deployment used by every executor |
| `SUPPORT_WORKFLOW_MAX_RETRIES` | `3` | Cap on per-target retries from the controller |
| `MICROSOFT_LEARN_MCP_URL` | `https://learn.microsoft.com/api/mcp` | Optional MCP grounding source |

To change the model, update `agent/agent.yaml` and `agent/agent.manifest.yaml`,
update the `AI_PROJECT_DEPLOYMENTS` azd env var, then run `azd up`.

## Iterating

```bash
# Code-only change → rebuild image + re-register agent (~30s)
azd deploy support-ticket-agent

# Infra change
azd provision

# Tear down
azd down --purge --force
```

## Local container test

```bash
cd agent
docker build -t support-ticket-agent:dev .
docker run --rm -p 8088:8088 \
  -e AZURE_AI_MODEL_DEPLOYMENT_NAME=gpt-4o \
  -e FOUNDRY_PROJECT_ENDPOINT="$(azd env get-value AZURE_AI_PROJECT_ENDPOINT)" \
  support-ticket-agent:dev

# In another shell:
curl http://localhost:8088/readiness   # → {"status":"healthy"}
```

The local container will fail real LLM calls (no managed identity), but
readiness and workflow plumbing can be verified.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `HTTP 424 session_not_ready` on Responses call | Container crash-looping | Query App Insights, filter `AppRoleName has 'support-ticket'` |
| `401 Unauthorized` from `…/openai/v1/responses` in logs | Agent MI missing `Azure AI User` role | Re-run `./scripts/grant-agent-rbac.sh`, wait 1–3 minutes |
| `model=""` in agent definition | `${AZURE_AI_MODEL_DEPLOYMENT_NAME}` substitution unset | Hardcoded in `agent/agent.yaml` already; verify it wasn't templated out |
| `azd deploy` postdeploy fails on `AZURE_TENANT_ID` | Cosmetic — deploy already succeeded | `azd env set AZURE_TENANT_ID "$(az account show --query tenantId -o tsv)"` |

## Credits

Workflow and agent design adapted from
[`support-ticket-foundry-pocs`](https://github.com/mohammadzaid308/support-ticket-foundry-pocs)
by Mohammad Zaid, packaged here for direct `azd up` deployment to a customer
Foundry environment.
