# RBAC requirements

Two identities need permissions: **you** (the human running `azd up`) and **the agent's managed identities** (created automatically by Foundry).

## 1. You (the deployer)

Your Azure account must have these roles **before** you run `azd up`. They are granted to your user by an Azure admin (or yourself if you already own the subscription) — they are not granted by the deployment.

| Role | Scope | Why |
|---|---|---|
| **Owner** *(or **Contributor** + **User Access Administrator**)* | Subscription | Bicep is subscription-scoped: `azd up` creates the resource group, all resources inside it, and several role assignments. |
| **Directory Reader** *(Entra ID)* | Tenant | Only if your tenant restricts `az ad sp list`. The post-deploy script uses it to find the agent's managed identities. |

The resource group, all Azure resources, and every other role assignment listed below are created automatically by `azd up` (Bicep + `scripts/grant-agent-rbac.sh`).

## 2. Agent's managed identities

Created by Foundry when the agent is registered. No manual action required — listed here for reference.

| Role | Scope | Granted by |
|---|---|---|
| **AcrPull** | Azure Container Registry | Bicep (`infra/core/ai/acr-role-assignment.bicep`) |
| **Azure AI User** | AI Services account | Post-deploy (`scripts/grant-agent-rbac.sh`) |

## 3. Auto-assigned to you after deploy

| Role | Scope | Granted by |
|---|---|---|
| **Azure AI User** | Foundry project | Bicep (`infra/core/ai/ai-project.bicep`) |

## If you enable AI Search

All assigned automatically by Bicep — you do **not** need new permissions beyond Owner (or Contributor + UAA).

| Principal | Role | Scope |
|---|---|---|
| You | Search Index Data Contributor | Search service |
| Foundry project MI | Search Service Contributor + Search Index Data Contributor | Search service |
| Search service MI | Storage Blob Data Reader | Storage account |
| Search service MI | Cognitive Services OpenAI User | AI Services account |

## TL;DR

Give the deployer **Owner** on the subscription. `azd up` does the rest — including creating the resource group.
