#!/usr/bin/env bash
# Install / refresh the agent-admin systemd unit and Caddy block.
# Run as a user with sudo privileges. Idempotent.

set -euo pipefail

# Use askpass helper if provided (allows non-interactive sudo)
if [[ -n "${SUDO_ASKPASS:-}" ]]; then
  SUDO="sudo -A"
else
  SUDO="sudo"
fi

REPO_DIR="/home/bots/projects/agent-admin"
SERVICE_SRC="$REPO_DIR/deploy/agent-admin.service"
CADDY_SNIPPET="$REPO_DIR/deploy/caddy-snippet.txt"
CADDYFILE="/etc/caddy/Caddyfile"
ENV_FILE="$REPO_DIR/backend/.env"

echo "==> 1. Install systemd unit"
$SUDO install -m 0644 "$SERVICE_SRC" /etc/systemd/system/agent-admin.service
$SUDO systemctl daemon-reload

echo "==> 2. Ensure .env exists"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<EOF
ADMIN_SECRET_KEY=$(openssl rand -hex 32)
ADMIN_PORT=5191
ADMIN_SECURE_COOKIES=true
ADMIN_ALLOW_SIGNUP=true
EOF
  echo "    wrote $ENV_FILE"
else
  echo "    $ENV_FILE already exists, leaving alone"
fi

echo "==> 3. Build frontend"
( cd "$REPO_DIR/frontend" && npm install --silent && npm run build )

echo "==> 4. Caddy block"
if grep -q "bots\.netforce\.com" "$CADDYFILE"; then
  echo "    Caddyfile already references bots.netforce.com, not modifying"
else
  echo "    Appending bots.netforce.com block to $CADDYFILE"
  $SUDO tee -a "$CADDYFILE" > /dev/null < "$CADDY_SNIPPET"
fi
$SUDO caddy validate --config "$CADDYFILE" --adapter caddyfile
$SUDO systemctl reload caddy

echo "==> 5. Start agent-admin service"
$SUDO systemctl enable --now agent-admin.service
$SUDO systemctl restart agent-admin.service

sleep 2
echo "==> 6. Status"
systemctl status agent-admin.service --no-pager | head -15
echo
curl -sf http://127.0.0.1:5191/api/health && echo "  (local health OK)" || echo "  (local health FAILED)"

echo
echo "Done. Once DNS/TLS settles, visit: https://bots.netforce.com"
