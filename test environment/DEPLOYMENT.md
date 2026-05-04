# Web Content FW Rule Creator — Deployment Guide

## Overview

This solution automatically creates Azure Firewall Policy application rules whenever a new VNet is registered in IPAM. It consists of:

- **Azure Function** (`it-d-web-content-fw-rule-automation-fn`) — creates the firewall rule collection and rule in the target firewall policy
- **Azure Logic App** (`web-content-fw-rule-creator`) — runs every 15 minutes, queries IPAM via Log Analytics, and invokes the Function when a new VNet is detected in a monitored subscription
- **Blob Storage config file** (`subscription_mapping.json`) — defines which subscriptions/resource groups are in scope and maps Azure regions to firewall policies

---

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) installed
- Contributor access on subscription **Platform Connectivity Dev** (`52ce279b-e5da-4cad-87f7-e00d125ee4ba`)
- Log Analytics Reader on workspace `it-p-secops-global-log`
- PowerShell or Windows Terminal

> **Note:** All commands below use PowerShell syntax. Line continuations use the backtick character (`` ` ``), not backslash (`\`).

---

## Files

| File | Description |
|---|---|
| `function_app.py` | Azure Function source code |
| `host.json` | Azure Functions host configuration |
| `requirements.txt` | Python dependencies |
| `function_app_deploy.json` | ARM template — Function App infrastructure |
| `logic_app.json` | ARM template — Logic App |
| `subscription_mapping.json` | Config file uploaded to blob storage |

---

## Deployment Steps

### Phase 1 — Login and set subscription

```powershell
az login
az account set --subscription "52ce279b-e5da-4cad-87f7-e00d125ee4ba"
```

---

### Phase 2 — Create the resource group

```powershell
az group create `
  --name "it-d-web-content-fw-rule-automation-rg" `
  --location "eastus"
```

---

### Phase 3 — Deploy Function App infrastructure

Run from the folder containing the project files.

```powershell
az deployment group create `
  --resource-group "it-d-web-content-fw-rule-automation-rg" `
  --template-file "function_app_deploy.json"
```

When complete, retrieve the outputs — you will need them in later steps:

```powershell
az deployment group show `
  --resource-group "it-d-web-content-fw-rule-automation-rg" `
  --name "function_app_deploy" `
  --query "properties.outputs" `
  --output table
```

Note down:
- `functionAppUrl` — e.g. `https://it-d-web-content-fw-rule-automation-fn.azurewebsites.net`
- `functionAppPrincipalId` — the managed identity object ID
- `configStorageAccountName` — `itdwcfwrulecfgst`

---

### Phase 4 — Deploy the Function code

Run from the folder containing the project files.

```powershell
Compress-Archive -Path function_app.py, host.json, requirements.txt -DestinationPath function_code.zip -Force
```

```powershell
az functionapp deployment source config-zip `
  --resource-group "it-d-web-content-fw-rule-automation-rg" `
  --name "it-d-web-content-fw-rule-automation-fn" `
  --src "function_code.zip"
```

---

### Phase 5 — Get the Function host key

```powershell
az functionapp keys list `
  --resource-group "it-d-web-content-fw-rule-automation-rg" `
  --name "it-d-web-content-fw-rule-automation-fn" `
  --query "functionKeys.default" `
  --output tsv
```

Copy the key — it is required for the Logic App deployment.

---

### Phase 6 — Upload subscription_mapping.json to blob storage

```powershell
az storage blob upload `
  --account-name "itdwcfwrulecfgst" `
  --container-name "fw-rule-config" `
  --name "subscription_mapping.json" `
  --file "subscription_mapping.json" `
  --auth-mode login `
  --overwrite
```

> **Note:** To add or update monitored subscriptions in the future, edit `subscription_mapping.json` and re-run this command. No redeployment of the Logic App or Function is needed.

---

### Phase 7 — Deploy the Logic App

Replace `<functionAppUrl>` and `<functionKey>` with the values from previous steps.

```powershell
az deployment group create `
  --resource-group "it-d-web-content-fw-rule-automation-rg" `
  --template-file "logic_app.json" `
  --parameters `
      functionAppUrl="<functionAppUrl>" `
      functionKey="<functionKey>" `
      configStorageAccountName="itdwcfwrulecfgst"
```

Retrieve the Logic App principal ID:

```powershell
az deployment group show `
  --resource-group "it-d-web-content-fw-rule-automation-rg" `
  --name "logic_app" `
  --query "properties.outputs.logicAppPrincipalId.value" `
  --output tsv
```

---

### Phase 8 — Assign RBAC roles

Replace `<functionAppPrincipalId>` and `<logicAppPrincipalId>` with the values captured in previous steps.

**Function App managed identity** — Network Contributor on the firewall policy resource group:

```powershell
az role assignment create `
  --assignee "<functionAppPrincipalId>" `
  --role "Network Contributor" `
  --scope "/subscriptions/52ce279b-e5da-4cad-87f7-e00d125ee4ba/resourceGroups/it-test-vwan-rg"
```

**Function App managed identity** — Reader on the subscription (required to query VNet address spaces):

```powershell
az role assignment create `
  --assignee "<functionAppPrincipalId>" `
  --role "Reader" `
  --scope "/subscriptions/52ce279b-e5da-4cad-87f7-e00d125ee4ba"
```

**Logic App managed identity** — Storage Blob Data Reader on the config storage account:

```powershell
az role assignment create `
  --assignee "<logicAppPrincipalId>" `
  --role "Storage Blob Data Reader" `
  --scope "/subscriptions/52ce279b-e5da-4cad-87f7-e00d125ee4ba/resourceGroups/it-d-web-content-fw-rule-automation-rg/providers/Microsoft.Storage/storageAccounts/itdwcfwrulecfgst"
```

**Logic App managed identity** — Reader on the subscription (required for Azure Resource Graph VNet region lookup):

```powershell
az role assignment create `
  --assignee "<logicAppPrincipalId>" `
  --role "Reader" `
  --scope "/subscriptions/52ce279b-e5da-4cad-87f7-e00d125ee4ba"
```

---

### Phase 9 — Authorize the API connections (Azure Portal)

These two connections use OAuth and must be authorized manually.

1. Go to **Azure Portal → Resource Group `it-d-web-content-fw-rule-automation-rg`**
2. Open the **`office365`** API connection → **Edit API connection** → **Authorize** → sign in → **Save**
3. Open the **`azuremonitorlogs`** API connection → **Edit API connection** → **Authorize** → sign in → **Save**

> The account used to authorize `azuremonitorlogs` must have **Log Analytics Reader** on workspace `it-p-secops-global-log` (subscription `67cbb141-a2e1-40de-bbd3-601e8dbb6879`, resource group `it-p-secops-workspaces-rg`).

---

### Phase 10 — Enable and test the Logic App

1. In the Azure Portal, open Logic App **`web-content-fw-rule-creator`**
2. If the status shows **Disabled**, click **Enable**
3. Click **Run Trigger → Recurrence** to fire it immediately without waiting 15 minutes
4. Open **Run History** and inspect each step to confirm the workflow is executing correctly

---

## Updating the subscription mapping

To add a new subscription or resource group to the monitoring scope, edit `subscription_mapping.json` and re-upload it (Phase 6). No redeployment needed.

**Scoped entry** — specific resource group only:
```json
{
  "subscriptionId": "<subscription-id>",
  "resourceGroup": "<resource-group-name>",
  "rcgName": "<rule-collection-group-name>"
}
```

**Subscription-wide entry** — any resource group in the subscription:
```json
{
  "subscriptionId": "<subscription-id>",
  "resourceGroup": null,
  "rcgName": "<rule-collection-group-name>"
}
```

To add a new Azure region, add an entry under the `regions` object mapping the region name to its firewall policy name:
```json
"regions": {
  "eastus": "it-test-eus-fp",
  "westus": "it-p-connect-prod-wus-fwp"
}
```

---

## Deployed resources summary

| Resource | Name | Resource Group |
|---|---|---|
| Function App | it-d-web-content-fw-rule-automation-fn | it-d-web-content-fw-rule-automation-rg |
| App Service Plan | it-d-web-content-fw-rule-automation-fn-plan | it-d-web-content-fw-rule-automation-rg |
| Function Storage Account | st`<uniqueString>` | it-d-web-content-fw-rule-automation-rg |
| Config Storage Account | itdwcfwrulecfgst | it-d-web-content-fw-rule-automation-rg |
| Logic App | web-content-fw-rule-creator | it-d-web-content-fw-rule-automation-rg |
| API Connection (email) | office365 | it-d-web-content-fw-rule-automation-rg |
| API Connection (logs) | azuremonitorlogs | it-d-web-content-fw-rule-automation-rg |
