# ════════════════════════════════════════════════════════════════════
# deploy_trace_service.ps1
# Creates a SEPARATE always-on App Service running the traced app
# (app.main_traced:app) next to the existing teva-kb-candidate service,
# cloning its plan, app settings, and identity permissions.
#
# Usage:   az login            (browser auth, once)
#          powershell -File tools\deploy_trace_service.ps1
#
# After it finishes, point ONLY Test-agent's OpenAPI tool at the new
# hostname (the script writes openapi/kb_candidates_trace.json for that).
# ════════════════════════════════════════════════════════════════════
param(
    [string]$SourceAppName = "teva-kb-candidate",
    [string]$NewAppName    = "teva-kb-trace"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Step($msg) { Write-Host "`n══ $msg" -ForegroundColor Cyan }

# ── 1. Locate the source app ──────────────────────────────────────
Step "Locating source app '$SourceAppName'"
$src = az webapp list --query "[?name=='$SourceAppName']" -o json | ConvertFrom-Json
if (-not $src) { throw "App '$SourceAppName' not found in the current subscription. Run 'az account set -s <sub>' first." }
$src = $src[0]
$rg   = $src.resourceGroup
$plan = $src.appServicePlanId
Write-Host "  resource group : $rg"
Write-Host "  app plan       : $(Split-Path $plan -Leaf)"
Write-Host "  location       : $($src.location)"

# ── 2. Read source app settings ───────────────────────────────────
Step "Reading app settings from $SourceAppName"
$settings = az webapp config appsettings list -g $rg -n $SourceAppName -o json | ConvertFrom-Json
$exclude  = @("WEBSITE_RUN_FROM_PACKAGE")
$pairs = @()
foreach ($s in $settings) {
    if ($exclude -notcontains $s.name) { $pairs += "$($s.name)=$($s.value)" }
}
$pairs += "TRACE_LOG_DIR=/home/LogFiles/kbtrace"
$pairs += "SCM_DO_BUILD_DURING_DEPLOYMENT=true"
Write-Host "  $($pairs.Count) settings will be applied (TRACE_LOG_DIR added)"

# ── 3. Create the new web app on the SAME plan ────────────────────
Step "Creating web app '$NewAppName' (same plan, Python 3.11)"
$existing = az webapp list --query "[?name=='$NewAppName']" -o json | ConvertFrom-Json
if ($existing) {
    Write-Host "  already exists — reusing it" -ForegroundColor Yellow
    $newApp = $existing[0]
} else {
    $newApp = az webapp create -g $rg -p $plan -n $NewAppName --runtime "PYTHON:3.11" -o json | ConvertFrom-Json
}
$hostName = $newApp.defaultHostName
if (-not $hostName) { $hostName = (az webapp show -g $rg -n $NewAppName --query defaultHostName -o tsv) }
Write-Host "  hostname: https://$hostName"

# ── 4. Apply settings, startup command, always-on ─────────────────
Step "Applying app settings"
az webapp config appsettings set -g $rg -n $NewAppName --settings @pairs -o none

Step "Setting startup command + Always On"
az webapp config set -g $rg -n $NewAppName `
    --startup-file "python -m uvicorn app.main_traced:app --host 0.0.0.0 --port 8000" `
    --always-on true -o none

# ── 5. Managed identity + role grants ─────────────────────────────
Step "Enabling system-assigned managed identity"
$identity = az webapp identity assign -g $rg -n $NewAppName -o json | ConvertFrom-Json
$pid = $identity.principalId
Write-Host "  principalId: $pid"

# Key Vault (vault name parsed from the AZURE_KEY_VAULT_URL setting)
$kvUrl = ($settings | Where-Object { $_.name -eq "AZURE_KEY_VAULT_URL" }).value
if ($kvUrl) {
    $kvName = ([uri]$kvUrl).Host.Split(".")[0]
    Step "Granting Key Vault access on '$kvName'"
    $kv = az keyvault show -n $kvName -o json | ConvertFrom-Json
    try {
        if ($kv.properties.enableRbacAuthorization) {
            az role assignment create --role "Key Vault Secrets User" --assignee-object-id $pid `
                --assignee-principal-type ServicePrincipal --scope $kv.id -o none
        } else {
            az keyvault set-policy -n $kvName --object-id $pid --secret-permissions get list -o none
        }
        Write-Host "  done"
    } catch {
        Write-Host "  ⚠ FAILED (need Owner/User Access Administrator). Grant manually:" -ForegroundColor Yellow
        Write-Host "    az role assignment create --role `"Key Vault Secrets User`" --assignee-object-id $pid --scope $($kv.id)"
    }
}

# Storage (account name parsed from the BLOB_ACCOUNT_URL setting)
$blobUrl = ($settings | Where-Object { $_.name -eq "BLOB_ACCOUNT_URL" }).value
if ($blobUrl) {
    $acct = ([uri]$blobUrl).Host.Split(".")[0]
    Step "Granting Storage roles on '$acct' (Blob Data Reader + Blob Delegator for SAS)"
    $stg = az resource list --resource-type "Microsoft.Storage/storageAccounts" --name $acct -o json | ConvertFrom-Json
    if ($stg) {
        foreach ($role in @("Storage Blob Data Reader", "Storage Blob Delegator")) {
            try {
                az role assignment create --role $role --assignee-object-id $pid `
                    --assignee-principal-type ServicePrincipal --scope $stg[0].id -o none
                Write-Host "  $role : done"
            } catch {
                Write-Host "  ⚠ $role FAILED. Grant manually:" -ForegroundColor Yellow
                Write-Host "    az role assignment create --role `"$role`" --assignee-object-id $pid --scope $($stg[0].id)"
            }
        }
    } else {
        Write-Host "  ⚠ storage account '$acct' not visible in this subscription — grant roles manually" -ForegroundColor Yellow
    }
}

# ── 6. Zip-deploy the code (Oryx builds requirements.txt) ─────────
Step "Zip-deploying the repo (app/, requirements.txt, runtime.txt)"
$zip = Join-Path $env:TEMP "kb-trace-deploy.zip"
if (Test-Path $zip) { Remove-Item $zip -Force -Confirm:$false }
Compress-Archive -Path (Join-Path $RepoRoot "app"),
                       (Join-Path $RepoRoot "requirements.txt"),
                       (Join-Path $RepoRoot "runtime.txt") -DestinationPath $zip
az webapp deploy -g $rg -n $NewAppName --src-path $zip --type zip --timeout 600 -o none
Write-Host "  deployed"

# ── 7. Wait for the service to come up ────────────────────────────
Step "Waiting for https://$hostName/healthz (Oryx build + cold start can take a few minutes)"
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "https://$hostName/healthz" -TimeoutSec 15
        if ($r.status -eq "ok") { $ok = $true; break }
    } catch { }
    Start-Sleep -Seconds 20
}
if ($ok) { Write-Host "  ✓ service is UP" -ForegroundColor Green }
else     { Write-Host "  ⚠ not responding yet — check 'az webapp log tail -g $rg -n $NewAppName'" -ForegroundColor Yellow }

# ── 8. Write the agent-side OpenAPI spec for the trace service ────
Step "Writing openapi/kb_candidates_trace.json (point ONLY Test-agent at this)"
$specPath = Join-Path $RepoRoot "openapi\kb_candidates.json"
$spec = Get-Content $specPath -Raw | ConvertFrom-Json
$spec.servers[0].url = "https://$hostName"
$outSpec = Join-Path $RepoRoot "openapi\kb_candidates_trace.json"
$spec | ConvertTo-Json -Depth 32 | Out-File $outSpec -Encoding utf8
Write-Host "  written: $outSpec"

# ── 9. Summary ────────────────────────────────────────────────────
Step "DONE — summary"
Write-Host @"
  Service URL      : https://$hostName
  Health check     : https://$hostName/healthz
  Log viewer       : https://$hostName/trace?key=<tool-api-key>
  Raw log download : https://$hostName/trace/raw?key=<tool-api-key>
  Trace files      : /home/LogFiles/kbtrace (also visible via Kudu)

  NEXT STEP (manual, in the Foundry portal):
    Test-agent → Tools → its OpenAPI tool → replace the spec with
    openapi/kb_candidates_trace.json (same x-api-key auth, only the
    server URL differs). Other agents keep using $SourceAppName untouched.
"@
