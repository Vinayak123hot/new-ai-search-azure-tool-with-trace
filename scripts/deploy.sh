#!/bin/bash
set -euo pipefail

APP_DIR="/home/azureuser/kb-tool"

echo "==> Installing/updating dependencies"
source "$APP_DIR/venv/bin/activate"
pip install -r "$APP_DIR/app/requirements.txt" -q

echo "==> Restarting service"
sudo systemctl restart kbtool
sleep 2
echo "   status: $(sudo systemctl is-active kbtool)"

echo "==> Health check"
curl -sf http://127.0.0.1:8000/healthz && echo " OK" || echo " FAILED"