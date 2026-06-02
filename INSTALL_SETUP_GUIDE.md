# KB Candidates Tool — End-to-End VM Setup Guide

This guide takes you from a **bare Ubuntu Azure VM** to a **running, TLS-secured `kbtool` service** verifiable via `systemctl status kbtool` and a public HTTPS endpoint.

Use this whenever you spin up a fresh VM (e.g., after a subscription change). Every command is copy-paste ready.

---

## 0. Architecture Recap

```
Internet ──► nginx :443 (TLS) ──► uvicorn 127.0.0.1:8000 ──► Azure AI Search
                                          │
                                          └─► Azure Key Vault (secrets via Managed Identity)
```

What lives **outside the VM** (must exist in your Azure subscription):

| Resource | Purpose | Current value |
|---|---|---|
| Azure AI Search | KB index source | `vinny-outlook-ragsearch.search.windows.net`, index `multimodal-rag-1778857242111` |
| Azure Key Vault | Holds `search-api-key`, `tool-api-key` | `vinny-kb-tool-vault.vault.azure.net` |
| GitHub repo | Code | `https://github.com/Vinayak123hot/vinny-ai-search.git` |

> If the **old Azure subscription is being deleted**, confirm the Key Vault and AI Search are in a subscription that **survives**, or you must recreate them and re-upload the index + secrets first. The steps below assume both still exist and are reachable.

---

## 1. Pre-Migration Checklist

Before touching the new VM, confirm:

- [ ] Azure subscription with quota for a B2s (or larger) Linux VM in `eastus`.
- [ ] Key Vault `vinny-kb-tool-vault` is reachable from your account (`az keyvault secret list --vault-name vinny-kb-tool-vault`).
- [ ] Azure AI Search service and index are still alive.
- [ ] GitHub personal access token or `gh auth login` ready (for cloning a private repo, if applicable).
- [ ] You know the **DNS label** you'll use for the new VM (e.g., `vinny-kbtool-v2` → `vinny-kbtool-v2.eastus.cloudapp.azure.com`).

---

## 2. Create the Azure VM

Either use the portal or the CLI. CLI version:

```bash
# Run on your local machine (not the VM)
RG="kbtool-rg"
LOC="eastus"
VM="vinny-kbtool-v2"
DNS_LABEL="vinny-kbtool-v2"   # becomes <label>.eastus.cloudapp.azure.com

az group create -n $RG -l $LOC

az vm create \
  --resource-group $RG \
  --name $VM \
  --image Ubuntu2204 \
  --size Standard_B2s \
  --admin-username azureuser \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --assign-identity                                # ← system-assigned managed identity

# Give the VM a stable DNS name
az network public-ip update \
  --resource-group $RG \
  --name ${VM}PublicIP \
  --dns-name $DNS_LABEL

# Open HTTP + HTTPS for certbot + traffic
az vm open-port -g $RG -n $VM --port 80  --priority 1010
az vm open-port -g $RG -n $VM --port 443 --priority 1020
```

Capture the FQDN — you'll need it in step 6:

```bash
echo "Domain: ${DNS_LABEL}.${LOC}.cloudapp.azure.com"
az vm show -d -g $RG -n $VM --query publicIps -o tsv
```

---

## 3. Grant the VM Access to Key Vault

The app uses `DefaultAzureCredential`, which on an Azure VM picks up the **system-assigned managed identity** automatically. Grant it read access to the vault:

```bash
# Get the VM's managed identity principal ID
PRINCIPAL_ID=$(az vm show -g $RG -n $VM --query identity.principalId -o tsv)

# Grant Key Vault Secrets User on the vault
KV_ID=$(az keyvault show -n vinny-kb-tool-vault --query id -o tsv)
az role assignment create \
  --assignee $PRINCIPAL_ID \
  --role "Key Vault Secrets User" \
  --scope $KV_ID
```

> If your Key Vault uses **access policies** instead of RBAC, run:
> ```bash
> az keyvault set-policy -n vinny-kb-tool-vault --object-id $PRINCIPAL_ID --secret-permissions get list
> ```

---

## 4. SSH into the VM and Update the System

```bash
ssh azureuser@${DNS_LABEL}.${LOC}.cloudapp.azure.com

# From now on, all commands run ON the VM
sudo apt-get update
sudo apt-get -y upgrade
sudo apt-get install -y git curl python3-venv python3-pip nginx snapd
```

---

## 5. Clone the Repo

```bash
mkdir -p /home/azureuser/vinnydemo
cd /home/azureuser/vinnydemo
git clone https://github.com/Vinayak123hot/vinny-ai-search.git clasisi_agent
cd clasisi_agent
```

If the repo is private, use `gh auth login` first or use a token URL.

---

## 6. Patch the Two Files That Contain the Old Domain

The repo still references the old VM's DNS name in two places. Replace them in one go:

```bash
NEW_DOMAIN="${DNS_LABEL}.${LOC}.cloudapp.azure.com"   # e.g. vinny-kbtool-v2.eastus.cloudapp.azure.com
OLD_DOMAIN="vinnyclasifiervm.eastus.cloudapp.azure.com"

cd /home/azureuser/vinnydemo/clasisi_agent

# nginx.conf — server_name
sed -i "s|${OLD_DOMAIN}|${NEW_DOMAIN}|g" config/nginx.conf

# setup.sh — DOMAIN variable + admin email for certbot
sed -i "s|<your-vm-dns>.eastus.cloudapp.azure.com|${NEW_DOMAIN}|g" scripts/setup.sh
sed -i "s|admin@yourdomain.com|<your-real-email>@example.com|g" scripts/setup.sh   # ← edit this

# openapi/test.sh — public test URL
sed -i "s|${OLD_DOMAIN}|${NEW_DOMAIN}|g" openapi/test.sh
```

> Verify quickly: `grep -RIn cloudapp config/ scripts/ openapi/`

### Optional fix: deploy.sh path

`scripts/deploy.sh` still points to the old `/home/azureuser/kb-tool` path. Fix it once so future redeploys work:

```bash
sed -i 's|/home/azureuser/kb-tool|/home/azureuser/vinnydemo/clasisi_agent|g' scripts/deploy.sh
```

---

## 7. Verify Key Vault Access from the VM (sanity check)

Before running `setup.sh`, confirm the managed identity can actually read the vault. This catches the #1 cause of `kbtool` failing at startup.

```bash
# Install Azure CLI just for the check (optional but useful)
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

az login --identity
az keyvault secret show --vault-name vinny-kb-tool-vault --name tool-api-key --query value -o tsv | head -c 8 ; echo "..."
```

If you see the first 8 chars of the secret, you're good. If you see a 403, recheck step 3.

---

## 8. Run the One-Shot Setup Script

This handles: Python venv → pip install → systemd unit → nginx site → Let's Encrypt cert.

```bash
cd /home/azureuser/vinnydemo/clasisi_agent
bash scripts/setup.sh
```

The script will:
1. `apt-get install` Python, nginx, snapd, curl.
2. Create the venv at `venv/` and install pinned deps from `app/requirements.txt`.
3. Copy `config/kbtool.service` to `/etc/systemd/system/` and `enable + start` it.
4. Copy `config/nginx.conf` to `/etc/nginx/sites-available/kbtool`, symlink it into `sites-enabled`, and reload nginx.
5. Install certbot via snap and obtain a Let's Encrypt cert for the domain, auto-configuring nginx for HTTPS + redirect.

Expected ending lines:
```
==> Done. Test HTTPS:
    curl -s https://<your-new-domain>/healthz
```

---

## 9. Verify the Service

```bash
# Service running?
sudo systemctl status kbtool --no-pager

# Live logs (Ctrl-C to exit)
journalctl -u kbtool -f

# Local health (skips nginx)
curl -s http://127.0.0.1:8000/healthz
# → {"status":"ok","tool":"get_kb_candidates"}

# Public health (through nginx + TLS)
curl -s https://${NEW_DOMAIN}/healthz
# → {"status":"ok","tool":"get_kb_candidates"}
```

Look for `✅ API keys successfully loaded from Key Vault` in the journalctl output — that confirms the managed-identity path is working.

---

## 10. Smoke-Test the Full API

```bash
cd /home/azureuser/vinnydemo/clasisi_agent
bash openapi/test.sh
```

You should see:
- `=== 1. Health check ===` → `{"status":"ok",...}`
- `=== 2. Valid request ===` → JSON with `candidates`, `spread`, `top_score`
- `=== 3. Wrong API key ===` → `HTTP 401`
- `=== 4. Empty description ===` → `HTTP 400`

---

## 11. Update Anything That Talks to the New Endpoint

Once the new VM is verified, update the consumer(s):

- **Azure AI Foundry / Copilot agent tool**: change the OpenAPI `servers[].url` in `openapi/kb_candidates.json` to the new domain, and re-import.
- Any other service / dashboard pointing at the old URL.

---

## Routine Operations Cheat Sheet

| Action | Command |
|---|---|
| Check status | `sudo systemctl status kbtool` |
| Restart | `sudo systemctl restart kbtool` |
| Tail logs | `journalctl -u kbtool -f` |
| Reload nginx | `sudo systemctl reload nginx` |
| Pull + redeploy | `cd /home/azureuser/vinnydemo/clasisi_agent && git pull && bash scripts/deploy.sh` |
| Force renew cert | `sudo certbot renew --force-renewal` |

---

## Troubleshooting

**`kbtool` keeps restarting / `systemctl status` shows `failed`**
```bash
journalctl -u kbtool -n 80 --no-pager
```
- `Failed to load secrets from Key Vault` → step 3 (managed identity + role) or step 7 (vault reachable) is broken.
- `ModuleNotFoundError` → venv install failed; rerun `pip install -r app/requirements.txt` inside the activated venv.
- `Address already in use` → another process owns :8000; `sudo lsof -i :8000` and stop it.

**`certbot` fails with `Connection refused` / `Timeout`**
- Port 80 isn't open in the NSG (rerun the `az vm open-port` from step 2).
- DNS hasn't propagated yet — wait 2–5 min after setting the DNS label, then retry: `sudo certbot --nginx -d $NEW_DOMAIN`.

**nginx returns `502 Bad Gateway`**
- `kbtool` isn't running. `sudo systemctl status kbtool`.

**Agent gets `401` but key looks right**
- Vault returned a different value than expected. Verify on VM: `az keyvault secret show --vault-name vinny-kb-tool-vault --name tool-api-key --query value -o tsv`.

---

## Appendix: What to Persist Between VMs

Everything sensitive lives in **Azure Key Vault**, not on the VM. So a fresh VM needs nothing from the old disk — the only state to carry over is:

1. The **Key Vault** (`vinny-kb-tool-vault`) — keep it, or recreate and re-add the two secrets.
2. The **AI Search index** — keep it, or rebuild + re-ingest documents.
3. The **GitHub repo** — already backed up.

The VM itself is **disposable**: this guide rebuilds it deterministically.
