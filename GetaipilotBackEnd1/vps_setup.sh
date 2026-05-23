#!/bin/bash
# =============================================================================
# GetaipilotBackEnd1 — VPS Setup Script
# Target: https://tg.getaipilot.in
# Run as root on Ubuntu 20.04 / 22.04
# =============================================================================

set -e  # Exit on any error

DEPLOY_DIR="/var/www/telesub"
DOMAIN="tg.getaipilot.in"
SERVICE="telesub"

echo "========================================"
echo " GetaipilotBackEnd1 VPS Installer"
echo " Domain: https://$DOMAIN"
echo "========================================"

# ── 1. System dependencies ────────────────────────────────────────────────────
echo ""
echo "[ 1/7 ] Installing system dependencies..."
apt update -y
apt install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx curl git

# ── 2. Upload / clone code ───────────────────────────────────────────────────
echo ""
echo "[ 2/7 ] Setting up app directory at $DEPLOY_DIR..."
mkdir -p "$DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR/sessions"
mkdir -p "$DEPLOY_DIR/logs"

# If you're using git clone, uncomment and edit the lines below:
# git clone https://github.com/YOUR_USERNAME/bot-dashboard.git /tmp/repo
# cp -r /tmp/repo/GAP_Telegram_Automation/GetaipilotBackEnd1/* "$DEPLOY_DIR/"

# If you're uploading via scp, code should already be in $DEPLOY_DIR.
# This script assumes you have already placed the code there.

echo "    ✅ Directory ready: $DEPLOY_DIR"

# ── 3. Python virtual environment ────────────────────────────────────────────
echo ""
echo "[ 3/7 ] Creating Python virtual environment..."
cd "$DEPLOY_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
echo "    ✅ Python venv created and dependencies installed"

# ── 4. .env file ─────────────────────────────────────────────────────────────
if [ ! -f "$DEPLOY_DIR/.env" ]; then
  echo ""
  echo "[ 4/7 ] Creating .env file..."
  cat > "$DEPLOY_DIR/.env" <<'EOF'
# ── Supabase ──────────────────────────────────────────
VITE_SUPABASE_URL=https://your-project.supabase.co
VITE_SUPABASE_ANON_KEY=your_supabase_anon_key

# ── Telegram API (from https://my.telegram.org) ───────
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash

# ── CORS ──────────────────────────────────────────────
CORS_ORIGINS=https://getaipilot.in,https://www.getaipilot.in,https://tg.getaipilot.in

# ── Server ────────────────────────────────────────────
HOST=127.0.0.1
PORT=8000
EOF
  echo "    ⚠️  .env file created — EDIT IT NOW before starting the service!"
  echo "    nano $DEPLOY_DIR/.env"
else
  echo "[ 4/7 ] .env already exists — skipping (check CORS_ORIGINS includes tg.getaipilot.in)"
fi

# ── 5. systemd service ───────────────────────────────────────────────────────
echo ""
echo "[ 5/7 ] Creating systemd service..."
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=GetaipilotBackEnd1 FastAPI — tg.getaipilot.in
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=$DEPLOY_DIR
EnvironmentFile=$DEPLOY_DIR/.env
ExecStart=$DEPLOY_DIR/venv/bin/uvicorn Telesub:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Fix permissions
chown -R www-data:www-data "$DEPLOY_DIR"
chmod 700 "$DEPLOY_DIR/sessions"

systemctl daemon-reload
systemctl enable "$SERVICE"
echo "    ✅ systemd service created: $SERVICE"

# ── 6. Nginx reverse proxy ───────────────────────────────────────────────────
echo ""
echo "[ 6/7 ] Configuring Nginx for $DOMAIN..."
cat > /etc/nginx/sites-available/$DOMAIN <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    # Health check — no auth required
    location /health {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
echo "    ✅ Nginx configured for $DOMAIN"

# ── 7. SSL certificate ───────────────────────────────────────────────────────
echo ""
echo "[ 7/7 ] Obtaining SSL certificate from Let's Encrypt..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email admin@getaipilot.in --redirect
echo "    ✅ SSL certificate issued and Nginx updated for HTTPS"

# ── Final summary ────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo " ✅ Setup complete!"
echo "========================================"
echo ""
echo " NEXT STEPS:"
echo " 1. Edit your .env file with real credentials:"
echo "    nano $DEPLOY_DIR/.env"
echo ""
echo " 2. Start the service:"
echo "    systemctl start $SERVICE"
echo "    systemctl status $SERVICE"
echo ""
echo " 3. Verify:"
echo "    curl -s https://$DOMAIN/health"
echo ""
echo " Useful commands:"
echo "   View logs:    journalctl -u $SERVICE -f"
echo "   Restart:      systemctl restart $SERVICE"
echo "   Stop:         systemctl stop $SERVICE"
echo "========================================"
