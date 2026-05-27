#!/bin/bash
set -euo pipefail

DOMAIN="<your-vm-dns>.eastus.cloudapp.azure.com"
APP_DIR="/home/azureuser/vinnydemo/clasisi_agent"

echo "==> System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3-venv python3-pip nginx snapd curl

echo "==> Python venv"
python3 -m venv "$APP_DIR/venv"
source "$APP_DIR/venv/bin/activate"
pip install --upgrade pip -q
pip install -r "$APP_DIR/app/requirements.txt" -q

echo "==> systemd service"
sudo cp "$APP_DIR/config/kbtool.service" /etc/systemd/system/kbtool.service
sudo systemctl daemon-reload
sudo systemctl enable kbtool
sudo systemctl start kbtool
echo "   kbtool status: $(sudo systemctl is-active kbtool)"

echo "==> nginx"
sudo cp "$APP_DIR/config/nginx.conf" /etc/nginx/sites-available/kbtool
sudo ln -sf /etc/nginx/sites-available/kbtool /etc/nginx/sites-enabled/kbtool
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl reload nginx

echo "==> Smoke test (HTTP before cert)"
curl -s http://127.0.0.1:8000/healthz

echo "==> Let's Encrypt TLS"
sudo snap install --classic certbot 2>/dev/null || true
sudo ln -sf /snap/bin/certbot /usr/bin/certbot 2>/dev/null || true
sudo certbot --nginx \
    -d "$DOMAIN" \
    --non-interactive \
    --agree-tos \
    -m admin@yourdomain.com \
    --redirect

echo ""
echo "==> Done. Test HTTPS:"
echo "    curl -s https://$DOMAIN/healthz"