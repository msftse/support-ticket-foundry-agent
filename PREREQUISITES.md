# Prerequisites

Install these on your laptop **before** running `azd up`.

## Required tools

1. **Git** — any recent version. Used to clone the repo.
2. **Azure CLI (`az`)** — version **≥ 2.60**
   <https://learn.microsoft.com/cli/azure/install-azure-cli>
3. **Azure Developer CLI (`azd`)** — version **≥ 1.11**
   <https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd>
4. **Docker Desktop** (or Rancher Desktop / OrbStack) — must be **running** during deployment.
   <https://www.docker.com/products/docker-desktop/>
5. **Bash shell**
   - macOS / Linux: built-in.
   - Windows: **WSL2** (Ubuntu) — the post-deploy RBAC script is bash.
6. **`curl`** and **`jq`** — for the smoke-test commands in the README.
   - macOS: `brew install jq` (curl is built-in).
   - Windows / WSL2: `sudo apt install -y curl jq`.

> **Bicep CLI** is required but installs automatically with `az` — no separate step. Verify with `az bicep version`.

## Azure-side requirements

- An Azure **subscription** you can deploy into.
- Rights to create: AI Foundry account/project, ACR, Log Analytics, App Insights, Storage.
- Rights to **assign roles** on the target resource group (Owner or User Access Administrator).
- A region where Foundry hosted agents + the chosen model are available.
  Default: `eastus2`. Alternatives: `swedencentral`, `westus3`.

## One-time install commands

### macOS

```bash
brew install git azure-cli azd jq
# Then install Docker Desktop from docker.com and launch it.
```

### Windows (PowerShell, then use WSL2 for deployment)

```powershell
winget install --id Git.Git
winget install --id Microsoft.AzureCLI
winget install --id Microsoft.Azd
winget install --id Docker.DockerDesktop
wsl --install -d Ubuntu
```

### Ubuntu / WSL2

```bash
sudo apt update && sudo apt install -y curl jq git
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
curl -fsSL https://aka.ms/install-azd.sh | bash
```

## Verify your environment

```bash
git --version
az version
azd version
docker info          # must show a running engine
az bicep version
jq --version
```

If all six commands print a version, you're ready to run `azd up`.
