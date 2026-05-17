# RBAC

Two identities need roles: **you** (the deployer) and the **agent's managed identities** (created by Foundry). Everything except the deployer's pre-deploy roles is assigned automatically by `azd up`.

## Before `azd up` — grant to the deployer

| Role | Scope | Why |
|---|---|---|
| **Contributor** + **User Access Administrator** *(or **Owner**)* | Subscription | Bicep creates the resource group, all resources, and several role assignments. |
| **Directory Reader** *(Entra)* | Tenant | Only if your tenant restricts `az ad sp list`; used by the post-deploy script. |

## Assigned automatically by `azd up`

| Principal | Role | Scope |
|---|---|---|
| Deployer | Azure AI User | Foundry project |
| Agent managed identities | AcrPull | Container Registry |
| Agent managed identities | Azure AI User | AI Services account |
| ACR system MI | ACR Repository Contributor, AcrPull | ACR |
| App Insights system MI | Monitoring Metrics Publisher | App Insights |

If AI Search is enabled, Bicep also grants Search/Storage/OpenAI roles to the project and Search service identities — no extra deployer permissions needed.

## After a successful deploy — revoke elevated roles

Once the smoke test passes, the deployer no longer needs Contributor or User Access Administrator. Revoke them and keep only:

| Role | Scope | Purpose |
|---|---|---|
| **Azure AI User** *(auto-assigned, keep)* | Foundry project | Call the agent endpoint. |
| **Reader** | Resource group | View resources. |

Re-grant Contributor + UAA temporarily (ideally via PIM) only when you need to run `azd provision`, `azd down`, or change infrastructure. Day-to-day use (`curl` the endpoint, read logs) does not require them.

```bash
DEPLOYER_OID="$(az ad signed-in-user show --query id -o tsv)"
SUB="$(az account show --query id -o tsv)"
RG="$(azd env get-value AZURE_RESOURCE_GROUP)"

for ROLE in "Owner" "Contributor" "User Access Administrator"; do
  az role assignment delete --assignee "$DEPLOYER_OID" --role "$ROLE" \
    --scope "/subscriptions/$SUB" 2>/dev/null || true
done

az role assignment create --assignee "$DEPLOYER_OID" --role "Reader" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG"
```

**Do not revoke** the auto-assigned roles on the agent's managed identities or platform services — the agent will stop working.

