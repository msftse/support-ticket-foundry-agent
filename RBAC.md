# RBAC — `support-ticket-foundry-agent`

Production guidance for assigning, auditing, and revoking the Azure role assignments required by this repo.

Three identity classes are in scope:

1. **Deployer** — the human (or CI/CD service principal) running `azd up`.
2. **Agent managed identities** — auto-created by Foundry when the agent is registered.
3. **Platform service identities** — system-assigned managed identities on the Foundry project, ACR, Search, etc.

---

## 1. Deployment-time roles (pre-assigned by an admin)

These are assigned to the deployer **before** `azd up`. They are not granted by the deployment itself.

| # | Role | Scope | Why required |
|---|---|---|---|
| 1 | **Contributor** | Subscription | `infra/main.bicep` is subscription-scoped: it creates the resource group (`Microsoft.Resources/resourceGroups/write`) and every workload resource. |
| 2 | **User Access Administrator** *(or **Role Based Access Control Administrator** with a constraint — see §6)* | Subscription | Bicep creates 5 role assignments and `scripts/grant-agent-rbac.sh` creates 2 more (`Microsoft.Authorization/roleAssignments/write`). Contributor alone cannot do this. |
| 3 | **Directory Reader** *(Entra ID)* | Tenant | Only if your tenant restricts `az ad sp list` for standard members. The post-deploy script enumerates the agent's managed identities via Microsoft Graph. |

> **`Owner`** at subscription scope is a strict superset of #1 + #2 and is acceptable, but it is **not the minimum**. Use Contributor + UAA (or RBAC Administrator) for least privilege.

### Why `Owner` is not the minimum

| Permission | Contributor | User Access Administrator | Owner |
|---|---|---|---|
| `Microsoft.Resources/*` (RG + resources) | ✅ | ❌ | ✅ |
| `Microsoft.Authorization/roleAssignments/write` | ❌ | ✅ | ✅ |
| Azure Policy / blueprints / billing | ❌ | ❌ | ✅ (unused) |

Combining the first two columns is sufficient and avoids granting unrelated Owner-only permissions.

---

## 2. Runtime roles (auto-assigned, no manual action)

Created by Bicep and the post-deploy hook during `azd up`. Listed here for audit and inventory.

### 2a. To the **deployer**

| Role | Scope | Source | Purpose |
|---|---|---|---|
| **Azure AI User** (`53ca6127-db72-4b80-b1b0-d745d6d5456d`) | Foundry project | `infra/core/ai/ai-project.bicep` | Call the agent endpoint from CLI / portal / SDK after deploy. |

### 2b. To the **agent's managed identities**

Foundry creates two system-assigned principals per hosted agent:

```
<ai-account>-<project>-<agent-name>-AgentIdentity
<ai-account>-<project>-<agent-name>-<short>-AgentIdentityBlueprint
```

| Role | Scope | Source | Purpose |
|---|---|---|---|
| **AcrPull** (`7f951dda-4ed3-4680-a7ca-43fe172d538d`) | Azure Container Registry | `infra/core/ai/acr-role-assignment.bicep` | Foundry runtime pulls the agent container image. |
| **Azure AI User** (`53ca6127-db72-4b80-b1b0-d745d6d5456d`) | AI Services account | `scripts/grant-agent-rbac.sh` (post-deploy hook) | Container's `DefaultAzureCredential` calls the inner Responses API. |

### 2c. To **platform service identities**

| Principal | Role | Scope | Source |
|---|---|---|---|
| ACR system MI | **ACR Repository Contributor** (`fb382eab-e894-4461-af04-94435c366c3f`) + **AcrPull** | ACR | `infra/core/host/acr.bicep` |
| App Insights system MI | **Monitoring Metrics Publisher** (`73c42c96-874c-492b-b04d-ab87d138a893`) | App Insights | `infra/core/monitor/applicationinsights.bicep` |

---

## 3. Optional roles when AI Search is enabled

Activated by deploying `infra/core/search/azure_ai_search.bicep`. All assignments are created by Bicep — the deployer needs no additional pre-assigned roles beyond §1.

| Principal | Role | Scope | Purpose |
|---|---|---|---|
| Deployer | **Search Index Data Contributor** (`8ebe5a00-799e-43f5-93ac-243d3dce84a7`) | Search service | Manage index documents from CLI / SDK. |
| Foundry project MI | **Search Service Contributor** (`7ca78c08-252a-4471-8644-bb5ff32d4ba0`) | Search service | Manage indexes, data sources, skillsets. |
| Foundry project MI | **Search Index Data Contributor** | Search service | Read/write index data on agents' behalf. |
| Search service MI | **Storage Blob Data Reader** (`2a2b9908-6ea1-4ae2-8e65-a410df84e7d1`) | Storage account | Indexer pulls source documents. |
| Search service MI | **Cognitive Services OpenAI User** (`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`) | AI Services account | Embeddings calls during indexing / query. |

If your agent's Python code calls Search **directly** (not via Foundry's built-in `azure_ai_search` tool), also grant the agent managed identities **Search Index Data Reader** (`1407120a-92aa-4202-b7e9-c0e197c71c8f`) on the Search service via `scripts/grant-agent-rbac.sh`.

---

## 4. Post-deployment hardening — revoking elevated roles

Once `azd up` has completed and the smoke test passes, the deployer's `Contributor + User Access Administrator` (or `Owner`) are **not required for normal operation**. They can — and in a production posture, **should** — be revoked.

### What stays, what goes

| Role on the deployer | After successful deploy | Reason |
|---|---|---|
| Contributor (subscription) | **Revoke** | Only needed for resource lifecycle changes. |
| User Access Administrator (subscription) | **Revoke** | Only needed for role-assignment lifecycle changes. |
| Owner (subscription) — if used | **Revoke** | Superset of the above. |
| Azure AI User (Foundry project) — auto-assigned | **Keep** | Required to call the agent endpoint. |
| Directory Reader (Entra) | **Optional** — keep if the deployer will re-run `grant-agent-rbac.sh` | Used only by the post-deploy script. |

### Recommended steady-state role set for the deployer

| Role | Scope | Purpose |
|---|---|---|
| **Reader** | Resource group | List resources, check provisioning state. |
| **Azure AI User** | Foundry project | Call the agent endpoint (already assigned — do **not** remove). |
| **Log Analytics Reader** *(optional)* | Log Analytics workspace | Run KQL queries against `AppTraces`, `AppRequests`, `AppDependencies`. |
| **Monitoring Reader** *(optional)* | Resource group | View App Insights dashboards, metrics, alerts. |

### When the elevated roles must be re-granted temporarily

Use Entra **Privileged Identity Management (PIM)** with a short activation window (4–8 h), approval workflow, and audit logging.

| Scenario | Re-grant Contributor + UAA? |
|---|---|
| Re-deploy agent code only (`azd deploy`) | Usually **no** — ACR push + agent registration use existing access. Re-grant only if Foundry rotates the agent's managed identity. |
| Re-run Bicep (`azd provision`) | **Yes** |
| Add resources (Search, models, storage, etc.) | **Yes** |
| Rotate model deployment / change SKU | **Yes** |
| Tear down (`azd down --purge --force`) | **Yes** |
| Call the agent / read logs / curl the endpoint | **No** |

### What you must NOT revoke

| Assignment | Consequence if removed |
|---|---|
| Agent MIs → AcrPull on ACR | Container pull fails → agent does not start. |
| Agent MIs → Azure AI User on AI Services account | Inner Responses API calls return `401` → workflow crashes. |
| Project MI → Storage Blob Data Contributor | Checkpoint persistence fails. |
| Project MI → Search roles (if Search enabled) | Foundry built-in Search tool returns `403`. |

---

## 5. Verification commands

Run after `azd up` to confirm the role landscape matches expectations.

```bash
SUB="$(az account show --query id -o tsv)"
RG="$(azd env get-value AZURE_RESOURCE_GROUP)"
AI_ACCOUNT="$(azd env get-value AZURE_AI_ACCOUNT_NAME)"
ACR_ENDPOINT="$(azd env get-value AZURE_CONTAINER_REGISTRY_ENDPOINT)"
ACR_NAME="${ACR_ENDPOINT%%.*}"
DEPLOYER_OID="$(az ad signed-in-user show --query id -o tsv)"

# All role assignments inside the resource group (audit)
az role assignment list --resource-group "$RG" --all \
  --query "[].{role:roleDefinitionName, principal:principalName, principalType:principalType, scope:scope}" -o table

# Deployer's current effective roles
az role assignment list --assignee "$DEPLOYER_OID" --all \
  --query "[].{role:roleDefinitionName, scope:scope}" -o table

# Agent MIs must show 'Azure AI User' on the AI Services account
AI_ACCOUNT_ID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$AI_ACCOUNT"
az role assignment list --scope "$AI_ACCOUNT_ID" \
  --query "[?contains(principalName, 'AgentIdentity')].{role:roleDefinitionName, principal:principalName}" -o table

# Agent MIs must show 'AcrPull' on the ACR
ACR_ID="/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.ContainerRegistry/registries/$ACR_NAME"
az role assignment list --scope "$ACR_ID" \
  --query "[?contains(principalName, 'AgentIdentity')].{role:roleDefinitionName, principal:principalName}" -o table
```

### Revoke the deployer's elevated roles after a successful deployment

```bash
DEPLOYER_OID="$(az ad signed-in-user show --query id -o tsv)"
SUB="$(az account show --query id -o tsv)"
RG="$(azd env get-value AZURE_RESOURCE_GROUP)"

# Remove subscription-scoped Owner / Contributor / User Access Administrator
for ROLE in "Owner" "Contributor" "User Access Administrator"; do
  az role assignment delete \
    --assignee "$DEPLOYER_OID" \
    --role "$ROLE" \
    --scope "/subscriptions/$SUB" 2>/dev/null || true
done

# Grant steady-state Reader on the resource group
az role assignment create \
  --assignee "$DEPLOYER_OID" \
  --role "Reader" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG"
```

> Confirm the README's smoke test still returns `HTTP 200` after revocation. If it fails with `401`, verify the deployer's auto-assigned **Azure AI User** on the Foundry project was not removed.

---

## 6. Tighter alternative — RBAC Administrator with a constraint

If governance does not permit `User Access Administrator` at subscription scope, substitute **Role Based Access Control Administrator** (`f58310d9-a9f6-439a-9e8d-f62e7b41a168`) with a condition limiting assignable role definitions to the exact set this repo uses:

```text
(
  (!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'}))
  OR
  (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {
    53ca6127-db72-4b80-b1b0-d745d6d5456d,  /* Azure AI User                  */
    7f951dda-4ed3-4680-a7ca-43fe172d538d,  /* AcrPull                        */
    fb382eab-e894-4461-af04-94435c366c3f,  /* ACR Repository Contributor     */
    73c42c96-874c-492b-b04d-ab87d138a893,  /* Monitoring Metrics Publisher   */
    ba92f5b4-2d11-453d-a403-e96b0029c9fe,  /* Storage Blob Data Contributor  */
    2a2b9908-6ea1-4ae2-8e65-a410df84e7d1,  /* Storage Blob Data Reader       */
    5e0bd9bd-7b93-4f28-af87-19fc36ad61bd,  /* Cognitive Services OpenAI User */
    7ca78c08-252a-4471-8644-bb5ff32d4ba0,  /* Search Service Contributor     */
    8ebe5a00-799e-43f5-93ac-243d3dce84a7   /* Search Index Data Contributor  */
  })
)
```

This restricts the deployer to creating only the role assignments this template needs, and nothing else.

---

## 7. Lifecycle summary

| Phase | Deployer roles | Notes |
|---|---|---|
| **Pre-deploy** | Contributor + User Access Administrator (subscription), Directory Reader (tenant) | Granted by an Azure admin, ideally via PIM. |
| **During `azd up`** | Same | All other assignments are created automatically. |
| **Steady-state** | Reader (RG) + Azure AI User (project, auto) | Revoke Contributor + UAA. |
| **Change windows** | Temporarily re-grant Contributor + UAA via PIM | Auto-expire after the change. |
